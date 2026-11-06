#!/usr/bin/env bash
# 매시 정본 문서 자동승인 순환 점검(호스트 cron). 대표님 지시 2026-11-06: "승인문서는 검토완료후 자동승인처리".
# 검토가 빠진 문서는 첫 검토, 조건을 채운 문서는 자동승인한다. 판정 기준은 app/services/doc_auto_approval.py 그대로.
# 운영 컨테이너 중 새 분류(would_review)를 가진 이미지를 먼저 쓰고, 없으면 실행 중인 아무 슬롯을 쓴다.
set -euo pipefail
exec 9>/tmp/doc_auto_approval_sweep.lock
flock -n 9 || { echo "$(date '+%F %T') skip: previous sweep still running"; exit 0; }
pick=""
for name in aads-server aads-server-green; do
    docker ps --format '{{.Names}}' | grep -qx "$name" || continue
    [[ -z "$pick" ]] && pick="$name"
    if docker exec "$name" grep -q "would_review" /app/scripts/doc_auto_approval_sweep.py 2>/dev/null; then
        pick="$name"; break
    fi
done
[[ -n "$pick" ]] || { echo "$(date '+%F %T') skip: no aads-server container"; exit 0; }
echo "$(date '+%F %T') sweep container=$pick"
timeout 900 docker exec "$pick" timeout 840 python3 scripts/doc_auto_approval_sweep.py --apply --limit "${SWEEP_LIMIT:-5}" --timeout 820
