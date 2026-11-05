#!/usr/bin/env bash
# 116 → contabo42 따뜻한 보관 계층으로 매일 옮긴다 (PRD: docs/design/20261105_AADS_116 디스크 확보와 42 보관 계층 PRD.md).
#
# 2026-11-05 116 디스크 98%. 일일 DB 덤프(8.6GB)를 카페24 검증 후에도 로컬에 1개 상시 보존했고,
# Codex 세션 기록·지난 배포본이 계속 쌓였다. 42(같은 데이터센터, 여유 750GB+)로 옮긴다.
#   1. DB 덤프: 카페24 전송 로그에 sha256 이 기록된(=검증된) 덤프만 42 로 옮기고, 42 에서 sha256 재대조.
#      일치하면 116 로컬본 삭제(rsync --remove-source-files). 42 에는 14일 보존.
#   2. Codex 세션 기록: 7일 넘은 .codex/sessions/** 만 (상태 DB·인증·설정은 건드리지 않는다).
#   3. acct-app 배포본: 최근 3개만 116 에 남긴다.
# 42 접속 실패·대조 불일치는 116 원본을 남긴다(fail-safe). 전송 대역 50MB/s.
set -uo pipefail

REMOTE_HOST="${AADS_OFFLOAD42_HOST:-mesh-42}"
REMOTE_ROOT="${AADS_OFFLOAD42_ROOT:-/srv/aads-offload/116}"
DUMPS="${AADS_OFFLOAD42_DUMPS:-/root/aads/backups/disk_offload}"
CAFE24_LOG="${AADS_OFFLOAD42_CAFE24_LOG:-/root/aads/logs/disk_offload_cafe24.log}"
CODEX_ROOT="${AADS_OFFLOAD42_CODEX_ROOT:-/root/.codex-relay}"
RELEASES="${AADS_OFFLOAD42_RELEASES:-/root/aads/state/component-releases}"
KEEP_RELEASES="${AADS_OFFLOAD42_KEEP_RELEASES:-3}"
SESSION_AGE_DAYS="${AADS_OFFLOAD42_SESSION_AGE_DAYS:-7}"
REMOTE_DUMP_DAYS="${AADS_OFFLOAD42_REMOTE_DUMP_DAYS:-14}"
RSYNC_COPY=(-a --partial --bwlimit="${AADS_OFFLOAD42_BWLIMIT:-51200}")
RSYNC_OPTS=("${RSYNC_COPY[@]}" --remove-source-files)

log() { printf '%s %s\n' "$(TZ=Asia/Seoul date '+%F %T KST')" "$*"; }
rsh() { timeout "${2:-60}" ssh -o BatchMode=yes -o ConnectTimeout=15 "$REMOTE_HOST" "$1"; }

if ! rsh "mkdir -p '$REMOTE_ROOT'/{db-dumps,codex-relay,component-releases}"; then
    log "ERROR 42 접속 실패 — 아무것도 옮기지 않음"
    exit 1
fi

# 1. DB 덤프
shopt -s nullglob
for dump in "$DUMPS"/aads_????????.dump; do
    name="$(basename "$dump")"
    verified="$(grep -oE "${name} checksum=[0-9a-f]{64}" "$CAFE24_LOG" 2>/dev/null | tail -1 | cut -d= -f2)"
    if [[ -z "$verified" ]]; then
        log "SKIP $name — 카페24 검증 기록 없음(로컬 보존)"
        continue
    fi
    local_sum="$(sha256sum "$dump" | awk '{print $1}')"
    if [[ "$local_sum" != "$verified" ]]; then
        log "SKIP $name — 로컬 sha256 이 카페24 기록과 다름(로컬 보존)"
        continue
    fi
    if ! rsync "${RSYNC_COPY[@]}" "$dump" "$REMOTE_HOST:$REMOTE_ROOT/db-dumps/"; then
        log "ERROR $name 전송 실패(로컬 보존)"
        continue
    fi
    remote_sum="$(rsh "sha256sum '$REMOTE_ROOT/db-dumps/$name'" 1800 | awk '{print $1}')"
    if [[ "$remote_sum" == "$local_sum" ]]; then
        rm -f -- "$dump" && log "MOVED $name → 42 sha256=${local_sum:0:12} (카페24·42 일치)"
    else
        log "ERROR $name 42 sha256 불일치(로컬 보존)"
    fi
done
rsh "find '$REMOTE_ROOT/db-dumps' -name 'aads_*.dump' -mtime +$REMOTE_DUMP_DAYS -delete" || true

# 2. Codex 세션 기록
list="$(mktemp)"; trap 'rm -f "$list"' EXIT
( cd "$CODEX_ROOT" 2>/dev/null && find . -path '*/.codex/sessions/*' -type f -mtime +"$SESSION_AGE_DAYS" | sed 's#^\./##' ) > "$list"
if [[ -s "$list" ]]; then
    if rsync "${RSYNC_OPTS[@]}" --files-from="$list" "$CODEX_ROOT/" "$REMOTE_HOST:$REMOTE_ROOT/codex-relay/"; then
        find "$CODEX_ROOT" -path '*/.codex/sessions/*' -type d -empty -delete 2>/dev/null || true
        log "MOVED codex sessions files=$(wc -l < "$list") (>${SESSION_AGE_DAYS}d)"
    else
        log "ERROR codex sessions 전송 실패"
    fi
fi

# 3. 지난 배포본
if [[ -d "$RELEASES" ]]; then
    mapfile -t old < <(ls -1t "$RELEASES" | tail -n +$((KEEP_RELEASES + 1)))
    for d in "${old[@]}"; do
        [[ -n "$d" && -d "$RELEASES/$d" ]] || continue
        if rsync "${RSYNC_OPTS[@]}" "$RELEASES/$d" "$REMOTE_HOST:$REMOTE_ROOT/component-releases/"; then
            find "$RELEASES/$d" -type d -empty -delete 2>/dev/null || true
            log "MOVED release $d"
        fi
    done
fi

log "DONE disk=$(df -h / | awk 'NR==2{print $5" free="$4}')"
