#!/bin/bash
set -euo pipefail
echo "[$(date)] Dashboard blue-green deploy started" > /tmp/dashboard-build.log
/root/aads/aads-dashboard/deploy.sh >> /tmp/dashboard-build.log 2>&1
echo "[$(date)] Dashboard blue-green deploy done" >> /tmp/dashboard-build.log
