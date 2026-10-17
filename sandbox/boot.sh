#!/bin/sh
# Trusted bootstrap. Called as root inside a NEW, nonpersistent microVM.
set -eu
exec >/dev/null 2>&1
umask 077
# No daemon log contains document output; no TCP Docker listener or bridge.
rm -f /var/run/docker.pid /var/run/docker.sock
dockerd --host=unix:///var/run/docker.sock --bridge=none \
  --iptables=false --ip6tables=false --ip-forward=false \
  --exec-opt native.cgroupdriver=cgroupfs >/dev/null 2>&1 &
attempt=0
until docker info >/dev/null 2>&1; do
  attempt=$((attempt + 1))
  [ "$attempt" -lt 40 ] || exit 1
  sleep 0.1
done
# Swap must not move document-bearing tmpfs pages onto a VM disk.
swapoff -a
[ "$(wc -l </proc/swaps)" -eq 1 ]
mkdir -p /run/ledgerical-ocr
mount -t tmpfs -o size=16m,nosuid,nodev,noexec,mode=0755 tmpfs /run/ledgerical-ocr
mkdir /run/ledgerical-ocr/input /run/ledgerical-ocr/output
chmod 0777 /run/ledgerical-ocr/input /run/ledgerical-ocr/output
docker image inspect ledgerical-ocr:2026-10-17 >/dev/null
# Fail closed before any upload if the platform cannot enforce these controls.
docker run --rm --pull=never --network=none --read-only --user=10001:10001 \
  --cap-drop=ALL --security-opt=no-new-privileges=true \
  --cpus=1 --memory=1g --memory-swap=1g --pids-limit=128 \
  --ulimit core=0:0 --ulimit nofile=128:128 --log-driver=none \
  --tmpfs=/tmp:rw,noexec,nosuid,nodev,size=64m,uid=10001,gid=10001,mode=0700 \
  ledgerical-ocr:2026-10-17 --probe
