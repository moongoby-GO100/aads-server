#!/bin/bash
cd /root/aads/aads-dashboard
docker compose build aads-dashboard >> /tmp/dashboard-rebuild.log 2>&1
docker compose up -d aads-dashboard >> /tmp/dashboard-rebuild.log 2>&1
echo "DONE: $(date)" >> /tmp/dashboard-rebuild.log
