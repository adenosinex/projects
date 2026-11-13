#!/bin/bash
# Deploy router-status on the Mac mini. Run ON the Mac after the files in
# this folder have been uploaded to ~/router-status/ (see README.md).
#   - picks a free port (18933 preferred, else 18934-18940) and sets the
#     server plist to it
#   - if the real cert is missing, mints a temporary self-signed cert so the
#     server can be smoke-tested over HTTPS; the real cert must replace it
#   - loads both LaunchAgents and verifies /health + data.json
set -u
cd "$HOME/router-status" || exit 1

# Stop any running server FIRST so the port check below is accurate.
# (Otherwise a running server on 18933 makes deploy pick a different port.)
UID_NOW="$(id -u)"
launchctl bootout "gui/$UID_NOW/org.zin.router-status-server" 2>/dev/null || true
sleep 2

PORT=18933
if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "port 18933 is taken, searching 18934-18940..."
  PORT=""
  for p in 18934 18935 18936 18937 18938 18939 18940; do
    if ! lsof -nP -iTCP:"$p" -sTCP:LISTEN >/dev/null 2>&1; then PORT="$p"; break; fi
  done
  if [ -z "$PORT" ]; then echo "FATAL: no free port 18933-18940"; exit 1; fi
  echo "using port $PORT instead"
fi
# Always set the server plist to the chosen port (so a previous fallback
# does not stick across redeploys).
/opt/homebrew/bin/python3 - "$PORT" <<'PYEOF'
import plistlib, sys
p = "org.zin.router-status-server.plist"  # uploaded copy in ~/router-status; copied to LaunchAgents below
with open(p, "rb") as f:
    d = plistlib.load(f)
d["ProgramArguments"] = ["/opt/homebrew/bin/python3",
                         "/Users/zin/router-status/server.py", sys.argv[1]]
with open(p, "wb") as f:
    plistlib.dump(d, f)
print("plist port set to", sys.argv[1])
PYEOF
echo "PORT=$PORT" > port.txt

CERT="$HOME/mac-compute/certs/status.mac.zin7.de.fullchain.crt"
KEY="$HOME/mac-compute/certs/status.mac.zin7.de.key"
if [ ! -f "$CERT" ] || [ ! -f "$KEY" ]; then
  echo "WARNING: real cert not present yet ($CERT) -- minting TEMPORARY self-signed cert"
  openssl req -x509 -newkey rsa:2048 -keyout selfsigned.key -out selfsigned.crt \
    -days 30 -nodes -subj "/CN=status.mac.zin7.de" 2>/dev/null
  chmod 600 selfsigned.key
  echo "TEMPORARY self-signed cert in use; replace with the real cert when it arrives"
else
  echo "real cert present"
fi

mkdir -p "$HOME/Library/LaunchAgents"
cp -f org.zin.router-status-collector.plist org.zin.router-status-server.plist \
  "$HOME/Library/LaunchAgents/"

for label in org.zin.router-status-collector org.zin.router-status-server; do
  launchctl bootout "gui/$UID_NOW/$label" 2>/dev/null || true
  launchctl bootstrap "gui/$UID_NOW" "$HOME/Library/LaunchAgents/$label.plist" \
    || launchctl load "$HOME/Library/LaunchAgents/$label.plist"
done
sleep 4
echo "--- launchctl ---"
launchctl list | grep router-status || echo "NOT LISTED (check logs)"
echo "--- data.json (head) ---"
head -c 1500 data.json 2>/dev/null || echo "data.json not written yet"
echo
echo "--- health ---"
if [ -f "$CERT" ] && [ -f "$KEY" ]; then
  curl -sk "https://127.0.0.1:$PORT/health" || echo "health check FAILED"
else
  curl -sk "https://127.0.0.1:$PORT/health" || curl -s "http://127.0.0.1:$PORT/health" || echo "health check FAILED"
fi
echo
echo "deploy done. Port: $PORT"
