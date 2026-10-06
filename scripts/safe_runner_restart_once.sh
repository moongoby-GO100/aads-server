#!/bin/bash
# AADS 러너 안전 재시작 — AADS 활성 작업이 0 으로 안정될 때만 systemd 재시작.
#
# 2026-09-29 교정: 이전 버전은 호스트 psql 로 조회했으나 contabo116 호스트에는
# psql 이 없다(command -v psql → 없음). 그 결과 ACTIVE 가 영구히 "err" 가 되어
# STABLE 이 절대 3 에 도달하지 못하고, 2026-09-23 에는 3시간 루프만 돌고
# "deadline reached without idle window" 로 끝났다. DB 조회를 컨테이너 경유로
# 바꾸고, 조회 실패가 조용히 묻히지 않게 사유를 journal 에 남긴다.
set -euo pipefail

PG_CONTAINER="${PG_CONTAINER:-aads-postgres}"
PG_USER="${PG_USER:-aads}"
PG_DB="${PG_DB:-aads}"
DEADLINE=$(( $(date +%s) + 10800 ))
STABLE=0
ACTIVE_SQL="SELECT COUNT(*) FROM pipeline_jobs WHERE project='AADS' AND status IN ('running','claimed','deploying')"

while [ "$(date +%s)" -lt "$DEADLINE" ]; do
  ACTIVE=$(docker exec "$PG_CONTAINER" psql -U "$PG_USER" -d "$PG_DB" -tAc "$ACTIVE_SQL" 2>/dev/null | tr -d '[:space:]')
  if ! [[ "$ACTIVE" =~ ^[0-9]+$ ]]; then
    ACTIVE="err"
  fi
  if [ "$ACTIVE" = "0" ]; then
    STABLE=$((STABLE+1))
  else
    STABLE=0
  fi
  logger -t aads-runner-safe-restart "active=${ACTIVE} stable=${STABLE} src=docker:${PG_CONTAINER}"
  if [ "$STABLE" -ge 3 ]; then
    logger -t aads-runner-safe-restart "AADS active jobs=0 stable — restarting aads-pipeline-runner.service (stale-code recycle)"
    # Stable DB observations do not serialize new claims. Reuse the exact
    # capability/drain/lease transaction used by every normal restart path.
    if bash "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/restart_local_runner.sh"; then
      exit 0
    else
      rc=$?
      [[ "$rc" == 3 ]] || exit "$rc"
      STABLE=0
    fi
  fi
  sleep 20
done
logger -t aads-runner-safe-restart "deadline reached without idle window — no restart performed (last active=${ACTIVE})"
exit 1
