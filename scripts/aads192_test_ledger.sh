#!/bin/bash
# AADS-192 ledger helper 단위 검증 (실제 deploy.sh 블록을 그대로 source)
set -uo pipefail
SRC=/root/aads/aads-dashboard/deploy.sh
BLOCK=/tmp/aads192_ledger_block.sh
sed -n '/^# --- deploy_runs 중앙 원장 기록 헬퍼/,/^# --- end deploy_runs 헬퍼/p' "$SRC" > "$BLOCK"

DEPLOY_LOG_FILE=/tmp/aads192_test_deploy.log
: > "$DEPLOY_LOG_FILE"
AADS_RELEASE_SHA="testsha192"
log() { printf '%s\n' "$*" | tee -a "$DEPLOY_LOG_FILE"; }

# shellcheck disable=SC1090
source "$BLOCK"

echo "--- TEST 1: record_deploy_start ---"
record_deploy_start
rc=$?
echo "exit=$rc DEPLOY_RUN_ID=[$DEPLOY_RUN_ID]"
if [ -z "$DEPLOY_RUN_ID" ]; then echo "FAIL: INSERT 실패"; exit 1; fi
TEST_ID="$DEPLOY_RUN_ID"

docker exec aads-postgres psql -U aads -d aads -tAc \
  "SELECT id||'|'||component||'|'||status||'|'||release_sha FROM deploy_runs WHERE id=${TEST_ID}"

echo "--- TEST 2: record_deploy_end (failed) ---"
echo "simulated failure line for ledger test" >> "$DEPLOY_LOG_FILE"
DEPLOY_RESULT="failed"
record_deploy_end
echo "exit=$?"
docker exec aads-postgres psql -U aads -d aads -tAc \
  "SELECT status||'|'||coalesce(error_summary,'NULL')||'|'||(phase_completed_at IS NOT NULL)::text FROM deploy_runs WHERE id=${TEST_ID}"

echo "--- TEST 3: record_deploy_end 재호출 (idempotent) ---"
record_deploy_end
echo "exit=$? (0 이어야 함)"

echo "--- TEST 4: INSERT 실패해도 set -e 중단 없음 ---"
(
  set -euo pipefail
  source "$BLOCK"
  _db_exec() { return 1; }
  record_deploy_start
  echo "PASS: set -e 환경에서 중단 없이 통과 (DEPLOY_RUN_ID=[$DEPLOY_RUN_ID])"
)
echo "subshell exit=$?"

echo "--- CLEANUP: 테스트 행 삭제 ---"
docker exec aads-postgres psql -U aads -d aads -tAc \
  "DELETE FROM deploy_runs WHERE id=${TEST_ID} AND release_sha='testsha192' RETURNING id"
echo "DONE"
