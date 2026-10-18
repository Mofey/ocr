#!/bin/sh
# Trusted bootstrap. Called as root inside a NEW, nonpersistent microVM.
set -eu
exec 3>&2
exec >/dev/null 2>&1
stage=configure_cgroups
trap 'result=$?; if [ "$result" -ne 0 ]; then printf "OCR_SANDBOX_BOOT_FAILED stage=%s exit=%s\n" "$stage" "$result" >&3; fi' EXIT
umask 077
# Fresh managed VMs enable threaded controllers in their populated cgroup
# namespace root. Docker needs a domain subtree to enforce memory.max too.
# Keep supervisor processes in a leaf before enabling domain controllers;
# the VM's enclosing platform limits remain in force throughout this setup.
cgroup_root=/sys/fs/cgroup
if [ "$(cat "$cgroup_root/cgroup.type")" = 'domain threaded' ]; then
  disabled_controllers=
  for controller in $(cat "$cgroup_root/cgroup.subtree_control"); do
    disabled_controllers="$disabled_controllers -$controller"
  done
  printf '%s\n' "$disabled_controllers" > "$cgroup_root/cgroup.subtree_control"
fi
[ "$(cat "$cgroup_root/cgroup.type")" = domain ]
mkdir -p "$cgroup_root/ledgerical-supervisor"
while IFS= read -r process_id; do
  printf '%s\n' "$process_id" > "$cgroup_root/ledgerical-supervisor/cgroup.procs" || {
    # A process that exited during enumeration needs no migration.
    if kill -0 "$process_id" 2>/dev/null; then exit 1; fi
  }
done < "$cgroup_root/cgroup.procs"
printf '%s\n' '+cpu +memory +pids' > "$cgroup_root/cgroup.subtree_control"
stage=start_docker
# No daemon log contains document output; no TCP Docker listener or bridge.
rm -f /var/run/docker.pid /var/run/docker.sock
dockerd --host=unix:///var/run/docker.sock --bridge=none \
  --iptables=false --ip6tables=false --ip-forward=false \
  --exec-opt native.cgroupdriver=cgroupfs 3>&- >/dev/null 2>&1 &
attempt=0
until docker info >/dev/null 2>&1; do
  attempt=$((attempt + 1))
  [ "$attempt" -lt 80 ] || exit 1
  sleep 0.1
done
# Swap must not move document-bearing tmpfs pages onto a VM disk.
stage=disable_swap
swapoff -a
[ "$(wc -l </proc/swaps)" -eq 1 ]
stage=mount_job_tmpfs
mkdir -p /run/ledgerical-ocr
mount -t tmpfs -o size=16m,nosuid,nodev,noexec,mode=0755 tmpfs /run/ledgerical-ocr
mkdir /run/ledgerical-ocr/input /run/ledgerical-ocr/output
chmod 0777 /run/ledgerical-ocr/input /run/ledgerical-ocr/output
stage=check_parser_image
docker image inspect ledgerical-ocr:2026-10-17 >/dev/null
# Fail closed before any upload if the platform cannot enforce these controls.
stage=restricted_container_probe
docker run --rm --pull=never --network=none --read-only --user=10001:10001 \
  --cap-drop=ALL --security-opt=no-new-privileges=true \
  --cpus=1 --memory=1g --memory-swap=1g --pids-limit=128 \
  --ulimit core=0:0 --ulimit nofile=128:128 --log-driver=none \
  --tmpfs=/tmp:rw,noexec,nosuid,nodev,size=64m,uid=10001,gid=10001,mode=0700 \
  ledgerical-ocr:2026-10-17 --probe
