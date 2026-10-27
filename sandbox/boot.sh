#!/bin/sh
# Trusted bootstrap. Called as root inside a NEW, nonpersistent microVM.
set -eu
exec 3>&2
exec >/dev/null 2>&1
stage=configure_cgroups
cgroup_mode=unknown
trap 'result=$?; if [ "$result" -ne 0 ]; then printf "OCR_SANDBOX_BOOT_FAILED stage=%s exit=%s cgroup_mode=%s\n" "$stage" "$result" "$cgroup_mode" >&3; fi' EXIT
umask 077
cgroup_check_only=false
case "$#" in
  0) ;;
  1) [ "$1" = --check-cgroups ]; cgroup_check_only=true ;;
  *) exit 1 ;;
esac
# cgroup.type exists only on non-root cgroups. The actual v2 hierarchy root
# has no type file and is exempt from the no-internal-process restriction.
# Delegated domains still need their supervisor processes moved to a leaf.
cgroup_root=/sys/fs/cgroup
stage=check_cgroup_hierarchy
[ "$(stat -f -c %T "$cgroup_root")" = cgroup2fs ]
[ -r "$cgroup_root/cgroup.controllers" ]
[ -w "$cgroup_root/cgroup.subtree_control" ]
available_controllers=" $(cat "$cgroup_root/cgroup.controllers") "
for controller in cpu memory pids; do
  case "$available_controllers" in *" $controller "*) ;; *) exit 1 ;; esac
done
read_cgroup_mode() {
  if [ ! -e "$cgroup_root/cgroup.type" ]; then
    cgroup_mode=root
  else
    case "$(cat "$cgroup_root/cgroup.type")" in
      domain) cgroup_mode=domain ;;
      'domain threaded') cgroup_mode=domain_threaded ;;
      threaded) cgroup_mode=threaded ;;
      'domain invalid'|'domain (invalid)') cgroup_mode=invalid ;;
      *) cgroup_mode=unknown ;;
    esac
  fi
}
stage=check_cgroup_type
read_cgroup_mode
if [ "$cgroup_mode" = domain_threaded ]; then
  stage=disable_cgroup_threading
  disabled_controllers=
  for controller in $(cat "$cgroup_root/cgroup.subtree_control"); do
    disabled_controllers="$disabled_controllers -$controller"
  done
  if [ -n "$disabled_controllers" ]; then
    printf '%s\n' "$disabled_controllers" > "$cgroup_root/cgroup.subtree_control"
  fi
  read_cgroup_mode
fi
stage=check_cgroup_type
case "$cgroup_mode" in
  root) ;;
  domain) mkdir -p "$cgroup_root/ledgerical-supervisor" ;;
  *) exit 1 ;;
esac
# cgroup.procs is a live listing. Repeat migration in delegated domains until
# the kernel accepts all required controllers. Never move hierarchy-root
# processes or remove provider-owned cgroups to force a configuration.
attempt=0
while :; do
  if [ "$cgroup_mode" = domain ]; then
    stage=migrate_cgroup_processes
    while IFS= read -r process_id; do
      printf '%s\n' "$process_id" > "$cgroup_root/ledgerical-supervisor/cgroup.procs" 2>/dev/null || :
    done < "$cgroup_root/cgroup.procs"
  fi
  stage=enable_cgroup_controllers
  if printf '%s\n' '+cpu +memory +pids' > "$cgroup_root/cgroup.subtree_control" 2>/dev/null; then
    break
  fi
  attempt=$((attempt + 1))
  [ "$attempt" -lt 20 ] || exit 1
  sleep 0.1
done
stage=verify_cgroup_controllers
enabled_controllers=" $(cat "$cgroup_root/cgroup.subtree_control") "
for controller in cpu memory pids; do
  case "$enabled_controllers" in *" $controller "*) ;; *) exit 1 ;; esac
done
if [ "$cgroup_check_only" = true ]; then
  exit 0
fi
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
