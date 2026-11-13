#!/bin/bash
# Build a filtered source tarball of the Mac services for the GitHub monorepo.
# Runs ON the Mac. Creates /tmp/projects-source.tar.gz containing the service
# source dirs (relative paths, e.g. mac-compute/, sub-convert/, ...).
# Excludes: venvs, caches, DBs, data/downloads, secrets, tokens, cookies,
# credentials, and any file > 10 MB. Prints a short summary; never prints secrets.
set -e
STAGE=/tmp/projects-sync
TARBALL=/tmp/projects-source.tar.gz
rm -rf "$STAGE" "$TARBALL"
mkdir -p "$STAGE"

EXCLUDES=(
  --exclude='venv/'
  --exclude='.venv/'
  --exclude='__pycache__/'
  --exclude='*.db'
  --exclude='*.db-wal'
  --exclude='*.db-shm'
  --exclude='*.sqlite'
  --exclude='*.sqlite3'
  --exclude='data/'
  --exclude='db/'
  --exclude='downloads/'
  --exclude='.env'
  --exclude='*.env'
  --exclude='auth.json'
  --exclude='*.secret'
  --exclude='*.pem'
  --exclude='*.key'
  --exclude='*.tar.gz'
  --exclude='node_modules/'
  --exclude='.DS_Store'
  --exclude='*token*'
  --exclude='*credential*'
  --exclude='*password*'
  --exclude='*cookie*'
  --exclude='*session_secret*'
  --exclude='session_secret'
  --exclude='*.pyc'
  --exclude='*.log'
  --exclude='*.bak*'
  --exclude='*.bak'
  --exclude='certs/'
  --exclude='*.auth'
  --exclude='aliases.json'
  --exclude='runs/'
  --exclude='run/'
  --exclude='client_status.json'
  --exclude='video_paths.json'
  --exclude='data.json'
)

for svc in mac-compute localdy-server sub-node sub-convert proxy-test ping-test dy-filter nav router-status; do
  src="/Users/zin/$svc"
  if [ -d "$src" ]; then
    rsync -a --max-size=10M "${EXCLUDES[@]}" "$src/" "$STAGE/$svc/"
  else
    echo "WARN: missing $src" >&2
  fi
done

# doh-relay-probe: just probe.py
mkdir -p "$STAGE/doh-relay-probe"
cp /opt/dohrelay-probe/probe.py "$STAGE/doh-relay-probe/" 2>/dev/null || echo "WARN: /opt/dohrelay-probe/probe.py missing" >&2

# homelab: nginx config + reload wrapper (no secrets in these)
mkdir -p "$STAGE/homelab/nginx"
cp /opt/homebrew/etc/nginx/status-443.conf "$STAGE/homelab/nginx/" 2>/dev/null || echo "WARN: status-443.conf missing" >&2
cp /usr/local/bin/nginx-reload-status-443.sh "$STAGE/homelab/nginx/" 2>/dev/null || echo "WARN: nginx-reload wrapper missing" >&2

# Remove specific files that carry secrets even though their names look innocent:
# sub-node's generated mihomo config (controller secret + subscription token URL),
# the downloaded subscription file (node passwords), the subscription URL list
# (token URLs), the published status files (token filenames + node passwords),
# and runtime test-result JSONs (not source).
rm -f "$STAGE/sub-node/sub.yaml" "$STAGE/sub-node/subscription.urls" \
  "$STAGE/sub-node/config.yaml" "$STAGE/sub-node/experience.json" \
  "$STAGE/sub-node/experience-v1.json" "$STAGE/sub-node/experience-udp.json" \
  "$STAGE/sub-node/speed.json"
rm -rf "$STAGE/sub-node/status" "$STAGE/sub-node/logs"
find "$STAGE" -name '*.env' -delete
echo "secret-file cleanup done"

tar -czf "$TARBALL" -C "$STAGE" .
echo "tarball: $TARBALL"
ls -lh "$TARBALL"
echo "staged tree:"
du -sh "$STAGE"/*
echo "file count: $(find "$STAGE" -type f | wc -l)"
