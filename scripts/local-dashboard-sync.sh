#!/bin/bash
# 서버68 대시보드 변경 자동감지 → BG 배포 트리거
# 5분마다 cron 실행. 소스 해시 비교 → 변경 시 deploy.sh 자동 호출.
# Contabo sync와 동일 패턴이지만 서버68 로컬용.

set -euo pipefail

DASHBOARD_DIR="/root/aads/aads-dashboard"
HASH_FILE="/tmp/local-dashboard-hash"
LOCKFILE="/tmp/local-dashboard-sync.lock"
LOG="/tmp/local-dashboard-sync.log"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" >> "$LOG"; }

# 동시 실행 방지
if [ -f "$LOCKFILE" ]; then
    LOCK_PID=$(cat "$LOCKFILE" 2>/dev/null || echo "")
    if [ -n "$LOCK_PID" ] && kill -0 "$LOCK_PID" 2>/dev/null; then
        log "skip (locked by PID $LOCK_PID)"
        exit 0
    fi
    rm -f "$LOCKFILE"
fi
echo $$ > "$LOCKFILE"
trap "rm -f $LOCKFILE" EXIT

compute_source_hash() {
    cd "$DASHBOARD_DIR"
    find . \
        \( -path './.git' -o -path './.git/*' \
        -o -path './node_modules' -o -path './node_modules/*' \
        -o -path './.next' -o -path './.next/*' \
        -o -path './docs' -o -path './docs/*' \
        -o -path './reports' -o -path './reports/*' \) -prune \
        -o -type f \
        ! -name 'tsconfig.tsbuildinfo' \
        ! -name '.active_port' \
        ! -name '.active_container' \
        ! -name 'build.log' \
        ! -name 'HANDOVER.md' \
        ! -name '*.bak_aads*' \
        ! -name '*.bak_*' \
        -print0 \
        | sort -z | xargs -0 md5sum 2>/dev/null | md5sum | cut -d' ' -f1
}

# 현재 소스 해시 (배포 상태/문서/빌드 산출물 제외)
CURRENT_HASH=$(compute_source_hash)

OLD_HASH=$(cat "$HASH_FILE" 2>/dev/null || echo "none")

if [ "$CURRENT_HASH" = "$OLD_HASH" ]; then
    exit 0
fi

log "dashboard source changed (${OLD_HASH:0:8} → ${CURRENT_HASH:0:8}), deploying..."

# 대시보드 BG 배포 실행
if bash "$DASHBOARD_DIR/deploy.sh" >> "$LOG" 2>&1; then
    echo "$CURRENT_HASH" > "$HASH_FILE"
    log "dashboard blue-green deploy done"
else
    log "dashboard deploy FAILED — kept current dashboard"
fi
