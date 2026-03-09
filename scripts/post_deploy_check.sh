#!/bin/bash
# AADS 배포 후 자동 검증 스크립트
# 사용법: bash scripts/post_deploy_check.sh
# docker compose up 후 자동 실행하거나, 수동 실행

set -e

echo "⏳ aads-server 기동 대기 (15초)..."
sleep 15

echo "=== AADS Post-Deploy Deep Health Check ==="

RESULT=$(curl -sf http://localhost:8100/api/v1/health/deep 2>&1)
if [ $? -ne 0 ]; then
    echo "❌ Deep health check 엔드포인트 접근 실패"
    echo "   기본 health: $(curl -sf http://localhost:8100/api/v1/health)"
    exit 1
fi

echo "$RESULT" | python3 -m json.tool 2>/dev/null || echo "$RESULT"

# 실패 항목 추출
FAILED=$(echo "$RESULT" | python3 -c "
import sys, json
d = json.load(sys.stdin)
failed = d.get('failed', [])
if failed:
    print('❌ 실패 항목: ' + ', '.join(failed))
    sys.exit(1)
else:
    print('✅ 전체 통과')
" 2>&1)

echo "$FAILED"
