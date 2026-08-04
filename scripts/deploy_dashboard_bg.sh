#!/bin/bash
# Dashboard deploy in background — returns immediately
LOG="/tmp/dashboard-deploy-$(date +%Y%m%d_%H%M%S).log"
echo "Starting dashboard deploy in background. Log: $LOG"
(
  cd /root/aads/aads-dashboard
  bash deploy.sh >> "$LOG" 2>&1
  echo "DEPLOY_EXIT_CODE=$?" >> "$LOG"
  echo "DEPLOY_DONE at $(date '+%Y-%m-%d %H:%M:%S KST')" >> "$LOG"
) &
disown
echo "PID: $!"
echo "LOG: $LOG"
