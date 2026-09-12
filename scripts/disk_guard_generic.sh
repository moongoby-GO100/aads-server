#!/usr/bin/env bash
# 서버 공용 디스크 감시 — 임계치를 넘으면 안전한 항목만 정리한다.
#
# AADS 전용 disk_cleanup_v2.sh 는 Docker 이미지 위주라, Docker 사용이 적고
# 로그가 큰 서버에서는 아무것도 회수하지 못한다. 2026-09-12 contabo14 실측:
# 디스크 95%, Docker 111M, /var/log 16G, /root 31G — Docker 정리는 무의미했다.
#
# 여기서는 "지워도 운영에 영향이 없는 것"만 다룬다.
#   - journald 아카이브
#   - 로테이션된 과거 로그(.gz, .1 ~ .9)
#   - 활성 로그 파일은 truncate 만 한다. rm 하면 프로세스가 열어둔 핸들 때문에
#     공간이 돌아오지 않고, 로그를 쓰던 프로세스가 깨질 수 있다.
#   - Docker: dangling 이미지, 중지된 컨테이너, 오래된 빌드 캐시
# 사용자 데이터/프로젝트 디렉터리는 절대 건드리지 않는다.
set -uo pipefail

THRESHOLD="${DISK_GUARD_THRESHOLD:-85}"
TRUNCATE_OVER_MB="${DISK_GUARD_TRUNCATE_OVER_MB:-512}"
JOURNAL_KEEP="${DISK_GUARD_JOURNAL_KEEP:-500M}"
BUILD_CACHE_KEEP="${DISK_GUARD_BUILD_CACHE_KEEP:-168h}"
STAMP="/tmp/.disk-guard-generic-last"
MIN_INTERVAL="${DISK_GUARD_MIN_INTERVAL:-1800}"
DRY="${DISK_GUARD_DRY_RUN:-0}"

usage_pct() { df -P / | awk 'NR==2 {gsub(/%/,"",$5); print $5}'; }
log() { echo "$(date '+%F %T') $*"; }
run() { if [ "$DRY" = "1" ]; then echo "    [dry] $*"; else eval "$@" >/dev/null 2>&1 || true; fi; }

USED="$(usage_pct)"
[[ "$USED" =~ ^[0-9]+$ ]] || exit 0
if [ "$USED" -lt "$THRESHOLD" ]; then exit 0; fi

now=$(date +%s)
if [ -f "$STAMP" ]; then
    last=$(cat "$STAMP" 2>/dev/null || echo 0)
    if [[ "$last" =~ ^[0-9]+$ ]] && [ $((now - last)) -lt "$MIN_INTERVAL" ]; then
        log "disk=${USED}% — 최근 정리 후 $((now - last))s, 건너뜀"; exit 0
    fi
fi
printf '%s' "$now" > "$STAMP"
log "disk=${USED}% >= ${THRESHOLD}% — 정리 시작"

# ① journald 아카이브
if command -v journalctl >/dev/null 2>&1; then
    log "  journald → ${JOURNAL_KEEP} 로 축소"
    run "journalctl --vacuum-size=${JOURNAL_KEEP}"
fi

# ② 로테이션된 과거 로그. 활성 파일(.log, syslog 등)은 건드리지 않는다.
log "  로테이션 로그 삭제(.gz / .1~.9, 7일 경과)"
run "find /var/log -type f \\( -name '*.gz' -o -name '*.[1-9]' -o -name '*.old' \\) -mtime +7 -delete"

# ③ logrotate 강제 실행 — 활성 로그를 정상 경로로 잘라낸다.
if command -v logrotate >/dev/null 2>&1 && [ -f /etc/logrotate.conf ]; then
    log "  logrotate 강제 실행"
    run "logrotate -f /etc/logrotate.conf"
fi

# ④ 그래도 거대한 활성 로그는 truncate. 파일은 남기고 내용만 비운다.
log "  ${TRUNCATE_OVER_MB}MB 초과 활성 로그 truncate"
while IFS= read -r f; do
    [ -n "$f" ] || continue
    sz=$(( $(stat -c%s "$f" 2>/dev/null || echo 0) / 1048576 ))
    log "    truncate ${f} (${sz}MB)"
    run ": > '$f'"
done < <(find /var/log -type f -name '*.log' -size +"${TRUNCATE_OVER_MB}"M 2>/dev/null)
for f in /var/log/syslog /var/log/messages /var/log/kern.log; do
    [ -f "$f" ] || continue
    sz=$(( $(stat -c%s "$f" 2>/dev/null || echo 0) / 1048576 ))
    [ "$sz" -gt "$TRUNCATE_OVER_MB" ] || continue
    log "    truncate ${f} (${sz}MB)"
    run ": > '$f'"
done

# ⑤ 방금 로테이션된 로그는 아직 압축 전이라 원본 크기 그대로 남는다.
# contabo14 실측: logrotate -f 직후 pgbouncer.log.1 4.0GB, syslog.1 4.9GB 가
# 그대로 있었고 mtime 기준 삭제 필터에도 안 걸려 회수가 1GB 에 그쳤다.
# 삭제하지 않고 압축한다 — 이력은 남기면서 공간을 돌려받는다(실측 8GB 회수).
log "  미압축 로테이션 로그 압축"
while IFS= read -r f; do
    [ -n "$f" ] || continue
    sz=$(( $(stat -c%s "$f" 2>/dev/null || echo 0) / 1048576 ))
    [ "$sz" -gt 50 ] || continue
    log "    gzip ${f} (${sz}MB)"
    run "nice -n 19 gzip -f '$f'"
done < <(find /var/log -type f \( -name '*.[1-9]' -o -name '*.log.[1-9]' \) ! -name '*.gz' 2>/dev/null)

# ⑥ Docker — 있을 때만
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    log "  docker dangling 이미지 / 중지 컨테이너 / 오래된 빌드 캐시"
    run "docker image prune -f --filter dangling=true"
    run "docker container prune -f"
    run "docker builder prune -af --filter until=${BUILD_CACHE_KEEP}"
fi

AFTER="$(usage_pct)"
log "정리 완료: ${USED}% → ${AFTER}%"
if [[ "$AFTER" =~ ^[0-9]+$ ]] && [ "$AFTER" -ge "$THRESHOLD" ]; then
    log "⚠️ 정리 후에도 ${AFTER}% — 큰 디렉터리를 사람이 확인해야 한다"
    du -sh /root/* /var/lib/* 2>/dev/null | sort -rh | head -5 | sed 's/^/    /'
fi
exit 0
