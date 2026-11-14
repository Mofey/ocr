"""Build-only compatibility for subprocess-free Paddle startup on Linux.

Paddle 3.3.1 probes libc and AVX with shell commands during import. The OCR
parser forbids process execution. Read the same operating-system facts directly
instead; neither the parser syscall policy nor model inference is changed.
"""
from __future__ import annotations

import ast
import hashlib
import importlib.metadata
import importlib.util
import sys
from pathlib import Path


# Exact core.py from the pinned PyPI wheel. Dependency changes must be reviewed
# rather than silently applying this compatibility change to different code.
_CORE_SHA256 = "a6d2f2b81a2427d56667b1b6e19552a88b8103288ea0ce8d992b502e9519fb32"
_REPLACEMENTS = {
    "get_libc_ver": '''def get_libc_ver():
    value = os.confstr("CS_GNU_LIBC_VERSION")
    if not value or not value.startswith("glibc "):
        raise RuntimeError("Prepared OCR image requires glibc")
    return ("glibc", value.split(" ", 1)[1])
''',
    "avx_supported": '''def avx_supported():
    with open("/proc/cpuinfo", encoding="ascii") as source:
        return any(
            line.partition(":")[0].strip() == "flags"
            and "avx" in line.partition(":")[2].split()
            for line in source
        )
''',
}


def prepare_paddle_runtime() -> None:
    if sys.platform != "linux" or importlib.metadata.version("paddlepaddle") != "3.3.1":
        raise RuntimeError("Paddle startup compatibility requires the pinned Linux dependency")
    spec = importlib.util.find_spec("paddle")
    if spec is None or spec.origin is None:
        raise RuntimeError("Pinned Paddle package is unavailable")
    core = Path(spec.origin).parent / "base" / "core.py"
    original = core.read_bytes()
    if hashlib.sha256(original).hexdigest() != _CORE_SHA256:
        raise RuntimeError("Pinned Paddle startup source changed; review compatibility before preparing")
    text = original.decode("utf-8")
    tree = ast.parse(text)
    targets = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in _REPLACEMENTS]
    if len(targets) != len(_REPLACEMENTS) or any(node.args.args or node.decorator_list for node in targets):
        raise RuntimeError("Pinned Paddle startup functions changed")
    lines = text.splitlines(keepends=True)
    for node in sorted(targets, key=lambda item: item.lineno, reverse=True):
        lines[node.lineno - 1:node.end_lineno] = [_REPLACEMENTS[node.name]]
    updated = "".join(lines)
    ast.parse(updated)
    core.write_text(updated, encoding="utf-8", newline="\n")
    print("Applied subprocess-free discovery to the pinned Paddle startup source.")


if __name__ == "__main__":
    prepare_paddle_runtime()
