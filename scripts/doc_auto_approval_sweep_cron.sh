#!/usr/bin/env bash
# 매시 정본 문서 자동승인 순환 점검(호스트 cron). 대표님 지시 2026-11-06: "승인문서는 검토완료후 자동승인처리".
# 검토가 빠진 문서는 첫 검토, 조건을 채운 문서는 자동승인한다. 판정 기준은 app/services/doc_auto_approval.py 그대로.
# 첫 검토(would_review)와 후속 검토 저장 수정(stored_role, 929fcc94)이 모두 들어간 운영 슬롯에서만 돈다.
# 수정 전 이미지에서 돌면 검토 LLM 호출은 하고 저장은 CHECK 위반으로 실패해 비용만 쓴다 — 그때는 건너뛴다.
set -euo pipefail
export TZ=Asia/Seoul
exec 9>/tmp/doc_auto_approval_sweep.lock
flock -n 9 || { echo "$(date '+%F %T') skip: previous sweep still running"; exit 0; }
pick=""
for name in aads-server aads-server-green; do
    docker ps --format '{{.Names}}' | grep -qx "$name" || continue
    if docker exec "$name" grep -q "would_review" /app/scripts/doc_auto_approval_sweep.py 2>/dev/null \
        && docker exec "$name" grep -q "def stored_role" /app/app/services/doc_cross_review.py 2>/dev/null; then
        pick="$name"; break
    fi
done
[[ -n "$pick" ]] || { echo "$(date '+%F %T') skip: 수정 코드(929fcc94 이상)가 배포된 aads-server 슬롯 없음"; exit 0; }
echo "$(date '+%F %T') sweep container=$pick"
timeout 900 docker exec "$pick" timeout 840 python3 scripts/doc_auto_approval_sweep.py --apply --limit "${SWEEP_LIMIT:-5}" --timeout 820
