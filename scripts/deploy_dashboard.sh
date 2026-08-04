#!/bin/bash
# Dashboard blue-green deploy — 대시보드의 자체 deploy.sh 위임
# 사용법: bash scripts/deploy_dashboard.sh
# 백그라운드 실행: bash scripts/deploy_dashboard_bg.sh
set -euo pipefail
DASHBOARD_DIR="/root/aads/aads-dashboard"
if [[ ! -f "${DASHBOARD_DIR}/deploy.sh" ]]; then
    echo "[deploy_dashboard.sh] ❌ ${DASHBOARD_DIR}/deploy.sh 없음"
    exit 1
fi
cd "$DASHBOARD_DIR"
exec bash deploy.sh "$@"
