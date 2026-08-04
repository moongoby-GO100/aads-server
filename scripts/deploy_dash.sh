#!/bin/bash
LOG="/tmp/dash-deploy-$(date +%s).log"
echo "LOG=$LOG"
cd /root/aads/aads-dashboard
bash deploy.sh > "$LOG" 2>&1 &
disown
echo "PID=$!"
