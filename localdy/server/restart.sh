#!/bin/bash
# Localdy split server restart (Mac mini).
#   HTTP  :18930 on 0.0.0.0  (LAN + Tailscale, plain HTTP)
#   HTTPS :18931 on ::       (IPv6 only, apple.su7.dpdns.org + Let's Encrypt fullchain)
# Both share the same DBs (WAL mode). Run from this directory.
# .env (mode 600) holds LOCALDY_CODE=<access code>; LOCALDY_CLIENT_TOKEN
# is intentionally NOT set -- the client token is paired via the web page.
cd "$(dirname "$0")" || exit 1
export LOCALDY_SERVER_MODE=1
[ -f .env ] && set -a && . ./.env && set +a

PY=.venv/bin/python
[ -x "$PY" ] || { echo "no .venv; run: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2; exit 1; }

kill_port() {
  local port=$1 pids
  pids=$(lsof -ti tcp:$port 2>/dev/null)
  if [ -n "$pids" ]; then
    echo "$pids" | xargs kill 2>/dev/null
    for _ in $(seq 1 30); do
      sleep 1
      lsof -ti tcp:$port >/dev/null 2>&1 || break
    done
    lsof -ti tcp:$port 2>/dev/null | xargs kill -9 2>/dev/null
    sleep 1
  fi
}

kill_port 18930
kill_port 18931

CERT="$HOME/mac-compute/certs/apple.su7.dpdns.org.fullchain.crt"
KEY="$HOME/mac-compute/certs/apple.su7.dpdns.org.key"
# 顺序启动：先 HTTP，等它健康后再起 HTTPS，避免双进程同时抢库
# （core.py 的 _enable_wal 已有 busy_timeout + 重试，这里再加一道保险）。
nohup "$PY" app.py --host 0.0.0.0 --port 18930 > logs-http.log 2>&1 &
ok1=""
for _ in $(seq 1 30); do
  sleep 1
  ok1=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:18930/health 2>/dev/null)
  [ "$ok1" = "200" ] && break
done
[ "$ok1" = "200" ] || { echo "HTTP instance failed to come up" >&2; tail -20 logs-http.log; exit 1; }
if [ -f "$CERT" ] && [ -f "$KEY" ]; then
  nohup "$PY" app.py --host :: --port 18931 --cert "$CERT" --key "$KEY" > logs-https.log 2>&1 &
else
  echo "[warn] apple cert not found, HTTPS instance not started" >&2
fi

ok2=""
for _ in $(seq 1 30); do
  sleep 1
  ok2=$(curl -sk -o /dev/null -w "%{http_code}" https://[::1]:18931/health 2>/dev/null)
  [ "$ok2" = "200" ] && { echo "localdy up: http :18930 + https :18931"; exit 0; }
done
echo "FAILED to come up (http=$ok1 https=$ok2)" >&2
echo "--- logs-http.log ---"; tail -20 logs-http.log
echo "--- logs-https.log ---"; tail -20 logs-https.log
exit 1
