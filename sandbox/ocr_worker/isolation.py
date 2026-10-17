"""Fail-closed Linux container and parser-process restrictions.

These controls supplement the deployment's network policy and container sandbox.
They do not replace a VM/kernel boundary or establish that an input is malware-free.
"""
from __future__ import annotations

import ctypes
import errno
import os
import socket
import sys
from pathlib import Path


def _prctl(option: int, value: int) -> None:
    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    libc.prctl.restype = ctypes.c_int
    if libc.prctl(option, value, 0, 0, 0) != 0:
        raise RuntimeError("OCR process protection could not be applied")


def enforce_worker_runtime() -> None:
    """Never accept documents in a development server or an unrestricted dyno."""
    if sys.platform != "linux" or os.geteuid() == 0:
        raise RuntimeError("OCR requires a dedicated, non-root Linux container")
    if not os.statvfs("/").f_flag & os.ST_RDONLY:
        raise RuntimeError("OCR requires a read-only container root filesystem")
    status = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines() if ":" in line)
    capability_fields = ("CapEff", "CapPrm", "CapInh", "CapBnd", "CapAmb")
    if any(int(status.get(name, "1").strip(), 16) != 0 for name in capability_fields) or status.get("NoNewPrivs", "0").strip() != "1":
        raise RuntimeError("OCR requires all capabilities dropped and no-new-privileges")
    if status.get("Seccomp", "0").strip() != "2":
        raise RuntimeError("OCR requires the container runtime's seccomp profile")
    for filename, maximum in (("memory.max", 1024 * 1024 * 1024), ("pids.max", 128)):
        value = Path("/sys/fs/cgroup", filename).read_text().strip()
        if not value.isdecimal() or not 0 < int(value) <= maximum:
            raise RuntimeError("OCR requires bounded cgroup v2 memory and process limits")
    cpu = Path("/sys/fs/cgroup/cpu.max").read_text().split()
    if len(cpu) != 2 or not all(value.isdecimal() for value in cpu) or not 0 < int(cpu[0]) <= int(cpu[1]):
        raise RuntimeError("OCR requires a CPU quota of at most one core")
    tmp_mount = [row.split() for row in Path("/proc/self/mountinfo").read_text().splitlines() if row.split()[4] == "/tmp"]
    if len(tmp_mount) != 1 or tmp_mount[0][tmp_mount[0].index("-") + 1] != "tmpfs":
        raise RuntimeError("OCR scratch space must be a dedicated memory-only /tmp mount")
    scratch = os.statvfs("/tmp")
    if scratch.f_blocks * scratch.f_frsize > 64 * 1024 * 1024:
        raise RuntimeError("OCR scratch space must be capped at 64 MiB")
    forbidden = {"DATABASE_URL", "PGPASSWORD", "PGHOST", "MONGODB_URL", "MAIN_SERVICE_TOKEN", "CMD_SERVICE_TOKEN", "MAIN_ADMIN_SERVICE_TOKEN", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "SECRET_KEY", "GOOGLE_APPLICATION_CREDENTIALS"}
    if any(value and (name in forbidden or name.startswith(("R2_", "AWS_"))) for name, value in os.environ.items()):
        raise RuntimeError("OCR worker must not receive application or storage credentials")
    # The parser has the same UID, but must not inspect its parent's grant key
    # through /proc/<pid>/environ, /proc/<pid>/mem, or process_vm_readv.
    _prctl(4, 0)  # PR_SET_DUMPABLE=0
    ctypes.CDLL("libseccomp.so.2")  # Required at startup, never an optional fallback.


class _ArgComparison(ctypes.Structure):
    _fields_ = [("arg", ctypes.c_uint), ("op", ctypes.c_int), ("datum_a", ctypes.c_uint64), ("datum_b", ctypes.c_uint64)]


def prepare_parser_sandbox() -> None:
    """Apply before importing any PDF/image/OCR parser in the spawned child."""
    if sys.platform != "linux":
        raise RuntimeError("OCR parser sandbox requires Linux")
    import resource

    _prctl(4, 0)  # no core dumps or same-UID process inspection
    _prctl(38, 1)  # PR_SET_NO_NEW_PRIVS
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_CPU, (30, 31))
    resource.setrlimit(resource.RLIMIT_FSIZE, (64 * 1024 * 1024, 64 * 1024 * 1024))
    _, hard_fds = resource.getrlimit(resource.RLIMIT_NOFILE)
    descriptor_limit = min(128, hard_fds) if hard_fds != resource.RLIM_INFINITY else 128
    resource.setrlimit(resource.RLIMIT_NOFILE, (descriptor_limit, descriptor_limit))
    lib = ctypes.CDLL("libseccomp.so.2", use_errno=True)
    lib.seccomp_init.argtypes, lib.seccomp_init.restype = [ctypes.c_uint32], ctypes.c_void_p
    lib.seccomp_release.argtypes, lib.seccomp_release.restype = [ctypes.c_void_p], None
    lib.seccomp_syscall_resolve_name.argtypes, lib.seccomp_syscall_resolve_name.restype = [ctypes.c_char_p], ctypes.c_int
    lib.seccomp_rule_add_array.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint, ctypes.POINTER(_ArgComparison)]
    lib.seccomp_rule_add_array.restype = ctypes.c_int
    lib.seccomp_load.argtypes, lib.seccomp_load.restype = [ctypes.c_void_p], ctypes.c_int
    context = lib.seccomp_init(0x7FFF0000)  # SCMP_ACT_ALLOW; narrow further than the container's profile.
    if not context:
        raise RuntimeError("OCR syscall filter could not be initialized")

    def deny(name: str, comparison: _ArgComparison | None = None, error_number: int = errno.EPERM) -> None:
        syscall = lib.seccomp_syscall_resolve_name(name.encode("ascii"))
        if syscall == -1:
            raise RuntimeError("OCR syscall filter is missing a required rule")
        args = ctypes.pointer(comparison) if comparison is not None else None
        if lib.seccomp_rule_add_array(context, 0x00050000 | error_number, syscall, int(comparison is not None), args) != 0:
            raise RuntimeError("OCR syscall filter rule could not be installed")

    try:
        # AF_UNIX socketpair is needed by asyncio's local wakeup pipe. No
        # network family, new connection, listener, program, or process attack.
        deny("socket", _ArgComparison(0, 1, socket.AF_UNIX, 0))  # SCMP_CMP_NE
        deny("socketpair", _ArgComparison(0, 1, socket.AF_UNIX, 0))
        for name in ("connect", "bind", "listen", "accept", "accept4", "execve", "execveat", "fork", "vfork", "ptrace", "process_vm_readv", "process_vm_writev", "pidfd_getfd", "pidfd_send_signal", "kill", "tkill", "tgkill", "rt_sigqueueinfo", "rt_tgsigqueueinfo", "mount", "umount2", "pivot_root", "unshare", "setns", "bpf", "perf_event_open", "keyctl", "io_uring_setup", "io_uring_enter", "io_uring_register"):
            deny(name)
        # Native OCR may create threads, but no descendant processes can outlive
        # the parent's hard timeout. clone3 has opaque arguments; ENOSYS lets
        # libc fall back to clone for threads, whose flags can be filtered.
        deny("clone3", error_number=errno.ENOSYS)
        deny("clone", _ArgComparison(0, 7, 0x00010000, 0))  # CLONE_THREAD absent
        deny("prctl", _ArgComparison(0, 4, 4, 0))  # cannot re-enable dumpability
        if lib.seccomp_load(context) != 0:
            raise RuntimeError("OCR syscall filter could not be activated")
    finally:
        lib.seccomp_release(context)
