#!/bin/bash
# github-auto-push.sh — Auto-push Mac services source to GitHub.
# Repo: adenosinex/projects (public monorepo, branch main).
# Runs every 3 days via cron (0 9 */3 * *). Pushes ONLY when there are changes.
#
# Flow:
#   1. Build a filtered source tarball on the Mac (staging script).
#   2. Download it, verify it, then refresh the service dirs in the repo.
#   3. Scan for abnormal files (secrets, DBs, >10 MB, venvs, caches).
#      Abnormal files are removed from the index, logged to sync-excluded.log,
#      and NEVER block the push.
#   4. If `git status --porcelain` is empty, exit 0 ("no changes").
#   5. Otherwise commit (dated message) and push.
#
# The GitHub token is read from the Mac's ~/mac-compute/data/github.json
# at runtime (via SSH) and used transiently in memory only. It is NEVER
# written to disk, never echoed, and never appears in any log.
# Logs: ~/workspace/github-auto-push.log (runs), ~/workspace/sync-excluded.log (excluded files).

set -e
REPO="$HOME/workspace/github-projects"
LOG="$HOME/workspace/github-auto-push.log"
EXCLUDED_LOG="$HOME/workspace/sync-excluded.log"
MAC_HOST="100.87.118.70"
MAC_USER="zin"
SSH_PY="$HOME/workspace/ssh-venv/bin/python $HOME/workspace/ssh-paramiko-key.py"
SFTP_PY="$HOME/workspace/ssh-venv/bin/python $HOME/workspace/ssh-sftp-key.py"
STAGE_SCRIPT="$REPO/scripts/make-projects-tarball.sh"
TARBALL_MAC="/tmp/projects-source.tar.gz"
TARBALL_LOCAL="/tmp/projects-source.tar.gz"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG"; }

# --- 1. Build the tarball on the Mac ---
log "start: building source tarball on Mac"
$SFTP_PY put "$STAGE_SCRIPT" /Users/zin/make-projects-tarball.sh >> "$LOG" 2>&1
$SSH_PY "$MAC_HOST" "$MAC_USER" "bash /Users/zin/make-projects-tarball.sh" >> "$LOG" 2>&1
$SFTP_PY get "$TARBALL_MAC" "$TARBALL_LOCAL" >> "$LOG" 2>&1
if [ ! -s "$TARBALL_LOCAL" ]; then log "ERROR: tarball missing/empty"; exit 1; fi
tar -tzf "$TARBALL_LOCAL" > /dev/null || { log "ERROR: tarball corrupt"; exit 1; }

# --- 2. Refresh the service dirs in the repo ---
cd "$REPO"
log "refreshing service dirs"
rm -rf mac-compute localdy-server sub-node sub-convert proxy-test ping-test \
  dy-filter nav router-status doh-relay-probe homelab/nginx
tar --no-same-owner -xzf "$TARBALL_LOCAL"

# --- 3. Abnormal-file scan (staged tree) ---
log "scanning for abnormal files"
git add -A
: > "$EXCLUDED_LOG"
echo "# excluded on $(date '+%Y-%m-%d %H:%M:%S') (abnormal files — never committed)" >> "$EXCLUDED_LOG"
excluded_count=0

# (a) files > 10 MB in the index
while IFS= read -r f; do
  sz=$(stat -f%z "$f" 2>/dev/null || stat -c%s "$f" 2>/dev/null || echo 0)
  if [ "$sz" -gt 10485760 ]; then
    echo "$f (size ${sz} bytes)" >> "$EXCLUDED_LOG"
    git rm --cached -q -- "$f"
    rm -f -- "$f"
    excluded_count=$((excluded_count + 1))
  fi
done <<EOF
$(git ls-files)
EOF

# (b) forbidden names / patterns in the index
while IFS= read -r f; do
  case "$f" in
    *.db|*.db-wal|*.db-shm|*.sqlite|*.sqlite3|*.env|.env|auth.json|*.secret|*.pem|*.key|*.crt|*.auth|*.tar.gz|*.log|*.bak*|sub.yaml|subscription.urls|client_status.json|data.json|aliases.json|video_paths.json|*.pyc)
      echo "$f (forbidden name/pattern)" >> "$EXCLUDED_LOG"
      git rm --cached -q -- "$f"; rm -f -- "$f"; excluded_count=$((excluded_count + 1));;
    *token*|*secret*|*password*|*credential*|*cookie*|*session_secret*)
      echo "$f (suspicious name)" >> "$EXCLUDED_LOG"
      git rm --cached -q -- "$f"; rm -f -- "$f"; excluded_count=$((excluded_count + 1));;
    */venv/*|*/.venv/*|*/__pycache__/*|*/node_modules/*|*/data/*|*/db/*|*/downloads/*|*/runs/*|*/run/*|sub-node/status/*)
      echo "$f (excluded path)" >> "$EXCLUDED_LOG"
      git rm --cached -q -- "$f"; rm -f -- "$f"; excluded_count=$((excluded_count + 1));;
  esac
done <<EOF
$(git ls-files)
EOF

# (c) 32-hex filenames (subscription token URLs) in the index
while IFS= read -r f; do
  base=$(basename "$f")
  if echo "$base" | grep -qE '^[0-9a-f]{32}\.'; then
    echo "$f (32-hex token filename)" >> "$EXCLUDED_LOG"
    git rm --cached -q -- "$f"; rm -f -- "$f"; excluded_count=$((excluded_count + 1))
  fi
done <<EOF
$(git ls-files)
EOF

log "excluded $excluded_count abnormal file(s) (see sync-excluded.log)"

# --- 4. Push only if there are changes ---
if [ -z "$(git status --porcelain)" ]; then
  log "no changes — nothing to push"
  exit 0
fi
changed=$(git status --porcelain | wc -l | tr -d ' ')
log "changes detected: $changed file(s)"

# --- 5. Read the token from the Mac (transient, in memory only) ---
TOKEN=$($SSH_PY "$MAC_HOST" "$MAC_USER" "python3 -c \"import json; print(json.load(open('/Users/zin/mac-compute/data/github.json'))['token'])\"" 2>/dev/null | grep -oE 'ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}' | head -1)
if [ -z "$TOKEN" ]; then log "ERROR: could not read GitHub token from Mac github.json"; exit 1; fi

git -c user.name="zin" -c user.email="zin@zin7.de" commit -q -m "Auto-sync $(date '+%Y-%m-%d')"
if git -c http.extraHeader="Authorization: Bearer $TOKEN" push -q origin main >> "$LOG" 2>&1; then
  log "pushed OK ($changed file(s) changed)"
else
  log "ERROR: push failed"
  unset TOKEN
  exit 1
fi
unset TOKEN
log "done"
