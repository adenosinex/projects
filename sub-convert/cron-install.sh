#!/bin/bash
# Install the hourly cron job for sub-convert (user crontab, no sudo).
# Runs hourly.py at minute 0 of every hour; skips if a test is running.
DIR="/Users/zin/sub-convert"
LINE="0 * * * * $DIR/venv/bin/python $DIR/hourly.py >> $DIR/cron.log 2>&1"
EXISTING=$(crontab -l 2>/dev/null | grep -v "sub-convert/hourly.py" || true)
{
  echo "$EXISTING"
  echo "$LINE"
} | grep -v '^$' | crontab -
echo "installed:"
crontab -l | grep "sub-convert/hourly.py"
