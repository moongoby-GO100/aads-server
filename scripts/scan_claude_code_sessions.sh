#!/usr/bin/env bash
# Claude Code 터미널 대화를 주기적으로 AADS 채팅 자원에 적재한다.
#
# SessionEnd 훅만으로는 부족하다. 훅은 세션이 정상 종료될 때 한 번 돌기 때문에,
# 세션이 비정상 종료되거나(크래시·SSH 끊김·서버 재부팅) 며칠씩 이어지면 그동안의
# 대화가 DB 에 하나도 없다. 2026-09-13 기준 진행 중이던 세션 하나가 32MB /
# 14,460행이었고 DB 적재분은 0건이었다. 원본 파일도 30일 뒤
# prune_claude_sessions.sh 가 지우므로, 그 사이 적재가 없으면 영구 소실이다.
#
# 적재기는 external_chat_sessions.metadata.entries 로 어디까지 넣었는지 기억하고
# 늘어난 만큼만 이어붙인다. 그래서 진행 중인 파일을 몇 번이고 넘겨도 안전하고,
# SessionEnd 훅은 마지막 남은 분량을 넣는 한 번의 호출이 될 뿐이다.
set -uo pipefail

SESSION_DIR="${CLAUDE_SESSION_DIR:-/root/.claude/projects}"
IMPORTER="/root/aads/aads-server/scripts/import_claude_code_session.py"
# 마지막 쓰기 직후는 마지막 줄이 잘려 있을 수 있어 잠깐 둔다. 파서가 깨진 줄을
# 건너뛰므로 치명적이진 않지만, 그 줄은 다음 회차에 들어오는 편이 낫다.
SETTLE_MINUTES="${CLAUDE_IMPORT_SETTLE_MINUTES:-2}"

[ -d "$SESSION_DIR" ] || exit 0
[ -x "$IMPORTER" ] || { echo "$(date '+%F %T') importer missing: $IMPORTER"; exit 0; }

# 지난 회차 이후 건드려진 파일만 본다.
#
# 전량 스캔은 회차마다 386개 파일(32MB짜리 포함)을 다시 파싱하고 파일당 psql 을
# 한 번씩 띄운다. 첫 회차는 6분이 걸렸다. 이미 적재가 끝난 옛 파일은 다시 읽을
# 이유가 없다 — 트랜스크립트는 append-only 이므로 mtime 이 그대로면 내용도 그대로다.
STAMP="${CLAUDE_IMPORT_STAMP:-/var/lib/aads/claude-session-scan.stamp}"
mkdir -p "$(dirname "$STAMP")" 2>/dev/null
NEWER=()
if [ -f "$STAMP" ]; then
    NEWER=(-newer "$STAMP")
fi
# 스탬프는 스캔 *시작* 시각으로 찍는다. 스캔 도중 append 된 파일이 다음 회차에
# 빠지지 않게 하려면 종료 시각이 아니라 시작 시각이어야 한다.
STAMP_NEW="${STAMP}.new"
: > "$STAMP_NEW"

touched=0
while IFS= read -r f; do
    sid="$(basename "$f" .jsonl)"
    out="$(echo '{}' | python3 "$IMPORTER" "$f" "$sid" 2>&1)"
    case "$out" in
        imported*|appended*) touched=$((touched + 1)); echo "$(date '+%F %T') $sid $out" ;;
        already*|"no conversational rows") : ;;
        *)                   echo "$(date '+%F %T') $sid FAILED: $out" ;;
    esac
done < <(find "$SESSION_DIR" -name '*.jsonl' -type f -mmin "+${SETTLE_MINUTES}" "${NEWER[@]}" | sort)

mv -f "$STAMP_NEW" "$STAMP" 2>/dev/null
[ "$touched" -gt 0 ] && echo "$(date '+%F %T') touched=${touched}"
exit 0
