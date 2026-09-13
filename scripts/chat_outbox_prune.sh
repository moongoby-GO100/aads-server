#!/usr/bin/env bash
# chat_outbox 보존 스윕 (migration 178)
#
# 배경: WP04 outbox는 생산자만 있고 소비자가 없어(v2 게이트 OFF) 무한 증식했다.
#       2026-09-13 실측 — 13.5시간 만에 942,611행 / 698MB.
# read model(chat_read_model.py)은 클라이언트 커서가 보존 구간보다 오래되면
# snapshot_required로 자동 폴백하므로 오래된 이벤트 삭제는 정합성을 깨지 않는다.
#
# cron: */5 * * * * /root/aads/aads-server/scripts/chat_outbox_prune.sh
set -uo pipefail

RETENTION="${CHAT_OUTBOX_RETENTION:-2 hours}"
BATCH="${CHAT_OUTBOX_PRUNE_BATCH:-20000}"
MAX_BATCHES="${CHAT_OUTBOX_PRUNE_MAX_BATCHES:-50}"
CONTAINER="${AADS_PG_CONTAINER:-aads-postgres}"

if ! docker ps --format '{{.Names}}' | grep -qx "${CONTAINER}"; then
    echo "$(date '+%F %T%z') skip reason=postgres_container_not_running container=${CONTAINER}"
    exit 0
fi

deleted=$(docker exec "${CONTAINER}" psql -U aads -d aads -At -c \
    "SELECT public.aads_chat_outbox_prune(INTERVAL '${RETENTION}', ${BATCH}, ${MAX_BATCHES})" 2>&1)
rc=$?

if [ "${rc}" -ne 0 ]; then
    echo "$(date '+%F %T%z') error rc=${rc} detail=${deleted}"
    exit "${rc}"
fi

remaining=$(docker exec "${CONTAINER}" psql -U aads -d aads -At -c \
    "SELECT count(*) FROM chat_outbox" 2>/dev/null)

echo "$(date '+%F %T%z') retention='${RETENTION}' deleted=${deleted} remaining=${remaining}"
