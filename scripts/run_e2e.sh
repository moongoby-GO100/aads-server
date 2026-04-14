#!/bin/bash
# E2E 테스트 실행 스크립트
set -euo pipefail

E2E_CONTAINER="${E2E_CONTAINER:-aads-server}"
E2E_BASE_URL="http://localhost:8080/api/v1"

echo "[E2E] 테스트 시작..."
if ! docker exec -e E2E_BASE_URL="$E2E_BASE_URL" "$E2E_CONTAINER" \
    python3 tests/test_e2e_api.py; then
    echo "[E2E] FAILED"
    exit 1
fi
echo "[E2E] PASSED"
