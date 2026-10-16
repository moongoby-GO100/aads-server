#!/usr/bin/env bash
# 외부 AI 수집기 cron 을 멱등 설치/제거한다.
#
# usage: install_external_ai_collector_cron.sh [--remove] [--print] [--disabled]
#   (없음)     마커 줄을 지우고 정확히 1줄을 다시 넣는다. 다른 crontab 줄은 그대로 둔다.
#   --remove   마커 줄만 지운다.
#   --print    crontab 을 바꾸지 않고 넣을 줄만 출력한다.
#   --disabled 줄 앞에 '# ' 를 붙여 비활성 상태로 설치한다(마커 유지, 멱등). 확인 안 된 화면을 실저장하지 않기 위함.
#
# 매시 17분. R-BG: timeout 900 으로 시간 상한을 걸고 flock -n 으로 중첩 실행을 막는다.
set -uo pipefail

MARKER="# AADS_EXTERNAL_AI_COLLECTOR"
SCRIPT_DIR="${AADS_EXTERNAL_AI_SCRIPT_DIR:-/root/aads/aads-server/scripts}"
LOG_FILE="${AADS_EXTERNAL_AI_LOG:-/var/log/aads-external-ai-collector.log}"
LOCK_FILE="${AADS_EXTERNAL_AI_LOCK:-/var/lock/aads-external-ai-collector.lock}"

ENTRY="17 * * * * flock -n ${LOCK_FILE} timeout 900 /usr/bin/python3 ${SCRIPT_DIR}/external_ai_collector.py >> ${LOG_FILE} 2>&1 ${MARKER}"

mode=install
disabled=0
for arg in "$@"; do
    case "$arg" in
        --remove) mode=remove ;;
        --print) mode=print ;;
        --disabled) disabled=1 ;;
        *) echo "unknown option: $arg" >&2; exit 64 ;;
    esac
done

if [ "$disabled" = 1 ]; then
    ENTRY="# ${ENTRY}"
fi

if [ "$mode" = print ]; then
    printf '%s\n' "$ENTRY"
    exit 0
fi

# crontab -l 의 실패를 "빈 crontab" 으로 취급하면, 읽기만 실패하고 쓰기는 성공하는 상황에서
# 기존 작업 전체가 수집기 한 줄로 교체된다. "no crontab for <user>" 만 빈 crontab 으로 본다.
list_err="$(mktemp)" || { echo "임시 파일 생성 실패" >&2; exit 1; }
trap 'rm -f "$list_err"' EXIT
if current="$(LC_ALL=C crontab -l 2>"$list_err")"; then
    :
elif grep -qi '^no crontab for' "$list_err"; then
    current=""
else
    echo "crontab 조회 실패 — 기존 작업을 보호하려고 변경하지 않는다: $(head -n 1 "$list_err")" >&2
    exit 1
fi
kept="$(printf '%s\n' "$current" | grep -vF "$MARKER" || true)"

if [ "$mode" = remove ]; then
    new="$kept"
else
    new="$(printf '%s\n%s\n' "$kept" "$ENTRY")"
fi

# 빈 줄이 늘어나지 않게 끝의 공백 줄을 하나로 맞춘다.
printf '%s\n' "$new" | sed -e :a -e '/^\n*$/{$d;N;ba' -e '}' | crontab - || { echo "crontab 갱신 실패" >&2; exit 1; }

count="$(crontab -l 2>/dev/null | grep -cF "$MARKER" || true)"
active="$(crontab -l 2>/dev/null | grep -F "$MARKER" | grep -vc '^[[:space:]]*#' || true)"
echo "marker lines: ${count} (active: ${active})"
