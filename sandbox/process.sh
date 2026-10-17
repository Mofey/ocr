#!/bin/sh
# The only document-bearing files are in bounded RAM mounts. No content in
# command arguments, Docker logs or Vercel Sandbox command logs.
set -eu
exec >/dev/null 2>&1
exec timeout --signal=TERM --kill-after=1s 28s \
  docker run --rm --pull=never --network=none --read-only --user=10001:10001 \
    --cap-drop=ALL --security-opt=no-new-privileges=true \
    --cpus=1 --memory=1g --memory-swap=1g --pids-limit=128 \
    --ulimit core=0:0 --ulimit nofile=128:128 --log-driver=none \
    --tmpfs=/tmp:rw,noexec,nosuid,nodev,size=64m,uid=10001,gid=10001,mode=0700 \
    --mount=type=bind,src=/run/ledgerical-ocr/input,dst=/input,readonly \
    --mount=type=bind,src=/run/ledgerical-ocr/output,dst=/output \
    ledgerical-ocr:2026-10-17
