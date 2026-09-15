#!/usr/bin/env bash
# aads-record-release — 배포 사실을 중앙 원장이 읽을 수 있는 형태로 남긴다.
#
# 왜 필요한가
#   GO100(contabo14) 은 자체 릴리스 컨트롤러가 release-queue.tsv 를 쓰기 때문에
#   `scripts/sync_external_deploy_ledger.py` 가 읽을 것이 있었다. NTV2/SF(cafe24_114)
#   는 그런 파일이 없어서 배포를 해도 채팅 아티팩트 '배포' 탭에 아무것도 남지
#   않았다(2026-09-15 CEO 지적). 이 스크립트가 그 빈 자리를 채운다.
#
# 설치
#   scp scripts/record_release.sh root@<host>:/usr/local/bin/aads-record-release
#   chmod +x /usr/local/bin/aads-record-release
#
# 사용
#   aads-record-release <PROJECT> <deployed|failed|blocked> [SHA] [TASK_ID] [NOTE]
#   예) aads-record-release NTV2 deployed "$(git -C /srv/newtalk-v2 rev-parse --short HEAD)" \
#         NTV2-FRONTEND-DEPLOY "frontend rolling 교체 성공"
#
# 원칙
#   - 이 스크립트가 남기는 것은 "호출한 쪽이 사실로 확인한 것"뿐이다.
#     성공을 확인하지 않았으면 deployed 로 부르지 마라(R-CRITICAL).
#   - 실패해도 배포 스크립트를 죽이지 않는다. 기록은 배포보다 덜 중요하다.
set -uo pipefail

PROJECT="${1:-}"
STATUS="${2:-}"
SHA="${3:-}"
TASK_ID="${4:-}"
NOTE="${5:-}"

if [ -z "$PROJECT" ] || [ -z "$STATUS" ]; then
    echo "usage: aads-record-release <PROJECT> <deployed|failed|blocked> [SHA] [TASK_ID] [NOTE]" >&2
    exit 2
fi

case "$STATUS" in
    deployed|failed|blocked) ;;
    *) echo "[aads-record-release] 알 수 없는 status=$STATUS (deployed|failed|blocked)" >&2; exit 2 ;;
esac

PROJECT="$(printf '%s' "$PROJECT" | tr '[:lower:]' '[:upper:]')"
PROJECT_LC="$(printf '%s' "$PROJECT" | tr '[:upper:]' '[:lower:]')"

QUEUE_DIR="/var/lib/aads-release-control/${PROJECT}"
QUEUE_FILE="${QUEUE_DIR}/release-queue.tsv"
STATE_DIR="/etc/aads-release"
STATE_FILE="${STATE_DIR}/${PROJECT_LC}-release-state"

NOW="$(date -Is)"
EVENT_ID="${PROJECT}-$(date +%s)-$$"
[ -n "$SHA" ] || SHA="$EVENT_ID"

# 탭/개행은 TSV 를 깨뜨린다. 한 줄 = 한 사건이라는 규칙을 지킨다.
sanitize() { printf '%s' "${1:-}" | tr '\t\n\r' '   ' | cut -c1-240; }
SHA="$(sanitize "$SHA")"
TASK_ID="$(sanitize "${TASK_ID:-$PROJECT release}")"
NOTE="$(sanitize "$NOTE")"
OWNER="$(sanitize "${AADS_RELEASE_OWNER:-$(id -un)@$(hostname -s)}")"
SESSION="$(sanitize "${AADS_RELEASE_SESSION:-}")"

mkdir -p "$QUEUE_DIR" "$STATE_DIR" 2>/dev/null || {
    echo "[aads-record-release] 기록 디렉터리 생성 실패 — 배포는 계속한다" >&2
    exit 0
}

printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
    "$EVENT_ID" "$SHA" "$STATUS" "$OWNER" "$SESSION" "$TASK_ID" "$NOW" "$NOTE" \
    >> "$QUEUE_FILE" 2>/dev/null || {
    echo "[aads-record-release] 큐 기록 실패 — 배포는 계속한다" >&2
    exit 0
}

# 큐가 무한히 자라지 않게 한다. 동기화는 최근 며칠만 읽으므로 2000줄이면 충분하다.
if [ "$(wc -l < "$QUEUE_FILE" 2>/dev/null || echo 0)" -gt 2000 ]; then
    tail -n 1000 "$QUEUE_FILE" > "${QUEUE_FILE}.trim" 2>/dev/null \
        && mv "${QUEUE_FILE}.trim" "$QUEUE_FILE"
fi

if [ "$STATUS" = "deployed" ]; then
    PHASE="deployed"
else
    PHASE="$STATUS"
fi

{
    echo "phase=${PHASE}"
    echo "release_sha=${SHA}"
    echo "updated_at=${NOW}"
} > "$STATE_FILE" 2>/dev/null || true

echo "[aads-record-release] ${PROJECT} ${STATUS} sha=${SHA} id=${EVENT_ID}"
exit 0
