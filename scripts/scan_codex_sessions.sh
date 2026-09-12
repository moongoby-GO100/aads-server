#!/usr/bin/env bash
# Codex 터미널 대화를 AADS 채팅 자원으로 적재한다.
#
# Codex CLI 에는 Claude Code 의 SessionEnd 훅에 해당하는 것이 없다. 그래서
# 훅 대신 주기 스캔으로 rollout-*.jsonl 을 훑는다. 적재기는 external_chat_sessions
# 로 멱등성을 보장하므로 같은 파일을 몇 번 넘겨도 중복 적재되지 않는다.
#
# 대상은 /root/.codex/sessions 만이다. /root/.codex-relay 는 같은 대화를 릴레이가
# 다시 기록한 사본이라 적재하면 전부 중복이 된다(2026-09-12 기준 4,358파일/16GB).
set -uo pipefail

SESSION_DIR="${CODEX_SESSION_DIR:-/root/.codex/sessions}"
IMPORTER="/root/aads/aads-server/scripts/import_claude_code_session.py"
# 마지막 수정이 이 분(minute) 이내인 파일은 아직 대화가 진행 중일 수 있어 건너뛴다.
SETTLE_MINUTES="${CODEX_IMPORT_SETTLE_MINUTES:-15}"

[ -d "$SESSION_DIR" ] || exit 0
[ -x "$IMPORTER" ] || { echo "$(date '+%F %T') importer missing: $IMPORTER"; exit 0; }

imported=0
while IFS= read -r f; do
    sid="$(basename "$f" .jsonl)"
    out="$(echo '{}' | python3 "$IMPORTER" "$f" "$sid" 2>&1)"
    case "$out" in
        imported*) imported=$((imported + 1)); echo "$(date '+%F %T') $sid $out" ;;
        already*)  : ;;
        *)         echo "$(date '+%F %T') $sid FAILED: $out" ;;
    esac
done < <(find "$SESSION_DIR" -name 'rollout-*.jsonl' -type f -mmin "+${SETTLE_MINUTES}" | sort)

[ "$imported" -gt 0 ] && echo "$(date '+%F %T') imported=${imported}"
exit 0
