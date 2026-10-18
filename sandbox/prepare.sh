#!/bin/sh
# Runs once in an EMPTY trusted sandbox, with outbound downloads allowed.
# Never run this in a sandbox that has received a user's document.
set -eu
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends docker.io docker-buildx ca-certificates
mkdir -p /opt/ledgerical /vercel/sandbox/build/build-certs
cp /vercel/sandbox/build/boot.sh /vercel/sandbox/build/process.sh /opt/ledgerical/
chmod 0755 /opt/ledgerical/boot.sh /opt/ledgerical/process.sh
# These are public certificates, not registry tokens or other credentials.
cp /etc/ssl/certs/ca-certificates.crt /vercel/sandbox/build/build-certs/sandbox-trust.crt
dockerd --host=unix:///var/run/docker.sock --bridge=none \
  --iptables=false --ip6tables=false --ip-forward=false \
  --exec-opt native.cgroupdriver=cgroupfs >/dev/null 2>&1 &
dockerd_pid=$!
attempt=0
until docker info >/dev/null 2>&1; do
  attempt=$((attempt + 1))
  [ "$attempt" -lt 100 ] || exit 1
  sleep 0.1
done
cd /vercel/sandbox/build
# Only the VM's standard proxy settings are build arguments. No app secrets.
DOCKER_BUILDKIT=1 docker build --network=host \
  --build-arg HTTP_PROXY --build-arg HTTPS_PROXY --build-arg NO_PROXY \
  --tag ledgerical-ocr:2026-10-17 .
docker image inspect --format '{{.Id}}' ledgerical-ocr:2026-10-17 > /opt/ledgerical/image-id
docker builder prune --all --force >/dev/null
kill "$dockerd_pid"
# Losing the socket does not mean the daemon has finished shutting down.
# Reap it before boot validation can start another daemon on the same state.
wait "$dockerd_pid"
attempt=0
while docker info >/dev/null 2>&1; do
  attempt=$((attempt + 1))
  [ "$attempt" -lt 100 ] || exit 1
  sleep 0.1
done
rm -f /var/run/docker.pid /var/run/docker.sock
