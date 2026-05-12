#!/bin/bash
# 대시보드 Blue-Green 재배포 래퍼. 직접 compose 교체는 SSE/세션 끊김을 만들 수 있어 금지.
set -euo pipefail
exec /root/aads/aads-dashboard/deploy.sh
