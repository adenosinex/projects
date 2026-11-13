#!/bin/bash
PID=$(cat /opt/homebrew/var/run/nginx-status-443.pid)
/bin/kill -HUP "$PID"
