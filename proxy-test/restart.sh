#!/bin/bash
# proxy-test restart: kill by port 18935, start uvicorn from the venv.
# Run on the Mac: bash /Users/zin/proxy-test/restart.sh
cd /Users/zin/proxy-test || exit 1
PID=$(lsof -ti :18935 2>/dev/null)
if [ -n "$PID" ]; then
  kill $PID 2>/dev/null
  for i in 1 2 3 4 5; do
    sleep 1
    lsof -ti :18935 >/dev/null 2>&1 || break
  done
  PID=$(lsof -ti :18935 2>/dev/null)
  [ -n "$PID" ] && kill -9 $PID 2>/dev/null
fi
# never touch the test mihomo ports here; the app manages its own runs
nohup /Users/zin/proxy-test/venv/bin/uvicorn app:app \
  --host 127.0.0.1 --port 18935 \
  --app-dir /Users/zin/proxy-test \
  >> /Users/zin/proxy-test/app.log 2>&1 &
sleep 2
curl -s --max-time 5 http://127.0.0.1:18935/api/health && echo " <- proxy-test up"
