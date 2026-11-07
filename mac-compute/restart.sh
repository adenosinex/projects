#!/bin/bash
# Restart mac-compute on port 5850. Kills only the process listening
# on 5850 (via lsof), not other app.py processes on this Mac.
# Also renews the Tailscale TLS cert if it has <30 days left.
#
# CAUTION (2026-11-07): the old script slept a fixed 2s after kill and
# then started the new process. The old process sometimes took longer
# to release the IPv4 socket, so the new process died with
# "[Errno 48] address already in use" on 0.0.0.0:5850 — while the
# health check still passed against the dying old process. This
# version waits until the port is actually free before starting, and
# fails loudly if the new process does not answer health.
set -euo pipefail
cd "$(dirname "$0")"
if [ -d certs ]; then
  (cd certs && tailscale cert --min-validity 720h zindemac-mini.tail35e420.ts.net || echo "cert renewal failed, keeping existing cert") || true
fi
PID=$(lsof -ti tcp:5850 || true)
if [ -n "$PID" ]; then
  echo "killing $PID"
  kill "$PID" || true
  for _ in $(seq 1 30); do
    sleep 1
    if [ -z "$(lsof -ti tcp:5850 || true)" ]; then break; fi
  done
  if [ -n "$(lsof -ti tcp:5850 || true)" ]; then
    echo "port still busy after 30s, killing -9 $PID"
    kill -9 "$PID" || true
    sleep 2
  fi
fi
nohup .venv/bin/python app.py > mac-compute.log 2>&1 &
for _ in $(seq 1 30); do
  sleep 1
  if [ "$(curl -sk -o /dev/null -w "%{http_code}" https://127.0.0.1:5850/health || true)" = "200" ]; then
    curl -sk https://127.0.0.1:5850/health; echo
    echo "mac-compute is up"
    exit 0
  fi
done
echo "ERROR: mac-compute did not answer health within 30s" >&2
tail -20 mac-compute.log >&2 || true
exit 1
