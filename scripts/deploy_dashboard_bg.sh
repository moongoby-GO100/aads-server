#!/bin/bash
# Background dashboard build+deploy
LOG=/tmp/dashboard-deploy-$(date +%Y%m%d%H%M%S).log
echo "START $(date)" > $LOG
cd /root/aads/aads-dashboard
bash deploy.sh bluegreen >> $LOG 2>&1
echo "END $(date) exit=$?" >> $LOG
echo $LOG
