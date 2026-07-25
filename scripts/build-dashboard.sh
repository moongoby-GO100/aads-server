#!/bin/bash
cd /root/aads/aads-dashboard
docker compose build aads-dashboard >> /tmp/dash-opus5-build.log 2>&1
docker compose up -d aads-dashboard >> /tmp/dash-opus5-build.log 2>&1
echo "DONE $(date)" >> /tmp/dash-opus5-build.log
