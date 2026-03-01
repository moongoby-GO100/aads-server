#!/bin/bash
# AADS E2E 수동 검증 스크립트
set -e

BASE_URL="${AADS_URL:-http://localhost:8080/api/v1}"
echo "=== AADS E2E 검증 (${BASE_URL}) ==="

# 1. 헬스 체크
echo -e "\n[1] Health check..."
curl -sf "${BASE_URL}/health" | python3 -m json.tool

# 2. 프로젝트 생성
echo -e "\n[2] 프로젝트 생성..."
PROJECT=$(curl -sf -X POST "${BASE_URL}/projects" \
  -H "Content-Type: application/json" \
  -d '{"description":"투두 앱 만들어줘"}')
echo "$PROJECT" | python3 -m json.tool
PID=$(echo "$PROJECT" | python3 -c "import sys,json; print(json.load(sys.stdin)['project_id'])")
echo "Project ID: $PID"

# 3. 체크포인트 승인
echo -e "\n[3] 요구사항 승인..."
curl -sf -X POST "${BASE_URL}/projects/${PID}/checkpoint" \
  -H "Content-Type: application/json" \
  -d '{"action":"approve"}' | python3 -m json.tool

# 4. 상태 조회
echo -e "\n[4] 최종 상태 조회..."
curl -sf "${BASE_URL}/projects/${PID}" | python3 -m json.tool

echo -e "\n=== 검증 완료 ==="
