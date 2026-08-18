#!/bin/bash
# Trigger dashboard deploy in background
nohup bash /root/aads/aads-dashboard/deploy.sh > /tmp/dashboard-deploy-20260807.log 2>&1 &
echo "PID=$!"
echo "LOG=/tmp/dashboard-deploy-20260807.log"
