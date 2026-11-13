#!/bin/bash
# github-sync.sh — 把工作目录同步到公开 monorepo 并在有改动时推送。
# 仓库本身 (~/workspace/github-projects) 只是一个发布镜像：
# 密钥、数据库和使用数据永远不进仓库（rsync 排除 + 根目录 .gitignore 双重保护）。
# 定时任务每小时跑一次；手动跑也安全。
set -u
REPO="$HOME/workspace/github-projects"
LOG="$HOME/workspace/github-sync.log"
{
echo "=== $(date '+%F %T') ==="
mkdir -p "$REPO/localdy/client" "$REPO/localdy/server" \
  "$REPO/mac-compute/apps/mlx-flask-transcriber" \
  "$REPO/media-tools/mrbeast-audio" \
  "$REPO/homelab/speedtest-node" "$REPO/homelab/ssh-helpers"

# localdy：服务端 + 单文件客户端
rsync -a --delete --exclude='__pycache__/' --exclude='*.pyc' \
  "$HOME/workspace/localdy-split/client/" "$REPO/localdy/client/"
rsync -a --delete --exclude='__pycache__/' --exclude='*.pyc' \
  --exclude='*.db' --exclude='*.sqlite3' --exclude='.env' \
  --exclude='video_paths.json' --exclude='client_token.json' \
  --exclude='localdy_client.json' --exclude='localdy_sessions.json' \
  --exclude='localdy_client_state.json' --exclude='localdy_client_pending.json' \
  --exclude='*.log' --exclude='logs/' \
  "$HOME/workspace/localdy-split/server/" "$REPO/localdy/server/"

# mac-compute 平台（apps/ 另外同步，见下）
rsync -a --delete --exclude='__pycache__/' --exclude='*.pyc' \
  --exclude='config.py' --exclude='data/' --exclude='certs/' \
  --exclude='*.db' --exclude='.venv/' --exclude='*.tar.gz' \
  --exclude='apps/' \
  "$HOME/workspace/mac-compute/" "$REPO/mac-compute/"

# Qwen ASR 独立服务（工作副本在 ~/workspace/mlx-flask-transcriber/，从 Mac 拉取）
rsync -a --delete --exclude='__pycache__/' --exclude='*.pyc' \
  --exclude='data/' --exclude='.venv/' --exclude='*.bak*' \
  "$HOME/workspace/mlx-flask-transcriber/" "$REPO/mac-compute/apps/mlx-flask-transcriber/"

# media-tools
cp "$HOME/workspace/mini_parse.py" "$REPO/media-tools/"
rsync -a --delete --exclude='audio_bilibili/' --exclude='*.m4a' --exclude='*.mp3' \
  --exclude='.buvid3' \
  "$HOME/workspace/mrbeast-audio-download/" "$REPO/media-tools/mrbeast-audio/"

# homelab
rsync -a --delete "$HOME/workspace/speedtest-node/" "$REPO/homelab/speedtest-node/"
cp "$HOME/workspace/ssh-paramiko.py" "$HOME/workspace/ssh-sftp.py" \
   "$HOME/workspace/tailscale-ssh-proxy.py" "$REPO/homelab/ssh-helpers/"

cd "$REPO" || exit 1
git add -A
if git diff --cached --quiet; then
  echo "no changes"
else
  git commit -q -m "auto: sync $(date '+%F %T')" && echo "committed"
fi
export GIT_SSH_COMMAND="ssh -o StrictHostKeyChecking=accept-new -o BatchMode=yes"
git push -q origin main 2>&1 | tail -3
echo "push exit: ${PIPESTATUS[0]}"
} >> "$LOG" 2>&1
