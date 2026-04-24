#!/bin/bash
echo "START: $(date)" > /tmp/dashboard-rebuild.log
cd /root/aads/aads-dashboard
docker compose build --no-cache aads-dashboard >> /tmp/dashboard-rebuild.log 2>&1
BUILD_EXIT=$?
echo "BUILD_EXIT=$BUILD_EXIT" >> /tmp/dashboard-rebuild.log
if [ $BUILD_EXIT -eq 0 ]; then
  docker compose up -d aads-dashboard >> /tmp/dashboard-rebuild.log 2>&1
  echo "UP_EXIT=$?" >> /tmp/dashboard-rebuild.log
fi
echo "DONE: $(date)" >> /tmp/dashboard-rebuild.log
