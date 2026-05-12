#!/bin/bash
set -euo pipefail
echo "START: $(date)" > /tmp/dashboard-rebuild.log
/root/aads/aads-dashboard/deploy.sh >> /tmp/dashboard-rebuild.log 2>&1
echo "DONE: $(date)" >> /tmp/dashboard-rebuild.log
