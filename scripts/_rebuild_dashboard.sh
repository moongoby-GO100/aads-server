#!/bin/bash
# 대시보드 빌드+배포 (백그라운드)
cd /root/aads/aads-dashboard
docker compose build aads-dashboard 2>&1 | tail -5
docker compose up -d aads-dashboard 2>&1
echo "DONE: $(date)"
