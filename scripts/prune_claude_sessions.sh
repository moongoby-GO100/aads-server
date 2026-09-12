#!/usr/bin/env bash
# CLI 세션 파일 보존 정리. 컨테이너 밖으로 옮기면서 정리 주체가 사라졌으므로 함께 둔다.
# 세션은 대화 맥락(도구 호출·결과)을 담아 --resume 에 쓰인다. 오래된 것은 어차피
# 릴레이 세션맵에서도 끊겨 재개에 쓰이지 않는다.
set -euo pipefail
DIR="${CLAUDE_SESSION_DIR:-/root/aads/data/claude-sessions}"
DAYS="${CLAUDE_SESSION_RETENTION_DAYS:-30}"
[ -d "$DIR" ] || exit 0
before=$(find "$DIR" -name '*.jsonl' | wc -l)
find "$DIR" -name '*.jsonl' -mtime +"$DAYS" -delete 2>/dev/null || true
find "$DIR" -type d -empty -not -path "$DIR" -delete 2>/dev/null || true
after=$(find "$DIR" -name '*.jsonl' | wc -l)
logger -t aads-claude-session-prune "retention=${DAYS}d files ${before} -> ${after} ($(du -sh "$DIR" 2>/dev/null | cut -f1))"
