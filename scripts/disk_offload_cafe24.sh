#!/usr/bin/env bash
# Daily Contabo -> cafe24 subdisk offload. --dry-run never changes source or remote data.
#
# R1 리뷰(runner-00a10393, score=0.545) 지적 4건을 반영한 재작업(2026-09-24):
#  1. 방금 생성된 worktree가 아직 pipeline_jobs 에 등록되기 전에 삭제되는 경쟁을 막기
#     위해 "생성 후 최소 경과 시간(WORKTREE_MIN_AGE_SEC)" 조건을 eligible() 에 추가했다.
#  2. pg_dump 파일이 카페24 /www 하위 경로로 나가므로 umask 077 + 원격 디렉터리
#     chmod 700 으로 웹 노출 위험을 줄였다.
#  3. pg_dump 전에 여유 공간을 확인해 기존 03:00 백업과 겹쳐 디스크가 가득 차는
#     것을 막는다.
#  4. Docker 빌드캐시는 원래 지시서에도 "재생성 가능한 캐시이므로 원격 백업 없이
#     prune"이 맞다 — 백업 대상이 아님을 주석으로 명시한다.
#  5. worktree 제거를 rm -rf 대신 git worktree remove 로 바꿔 git 내부 등록 정보가
#     남지 않게 한다.
#
# 2026-10-08 수정(AADS-DISK-OFFLOAD-FIX-20261008):
#  6. worktree 제거는 그 worktree 를 만든 저장소(git-common-dir)에서 실행한다.
#     /root/aads/.worktrees 에는 aads-dashboard 소속 worktree 가 섞여 있어, 고정된
#     aads-server 저장소에서 지우면 "is not a working tree" 로 매일 4건씩 실패했다.
#  7. git 이 dirty/locked 로 거부하면 ERROR 가 아니라 SKIP(보호 대상)으로 분류한다.
#  8. 덤프 rsync 는 --partial --inplace + ssh keepalive + 재시도(재개)로 보낸다.
#  9. 종료 시 errors>0 이면 텔레그램으로 쿨다운을 두고 1회 알린다.
set -uo pipefail

DRY_RUN=0
case "${1:-}" in
    --dry-run) DRY_RUN=1 ;;
    --install-cron)
        cron_text="$(crontab -l 2>&1)"
        cron_rc=$?
        if (( cron_rc != 0 )); then
            if [[ "$cron_text" == *'no crontab for'* ]]; then
                cron_text=''
            else
                printf 'cannot read crontab: %s\n' "$cron_text" >&2
                exit 1
            fi
        fi
        if printf '%s\n' "$cron_text" | grep -Fq 'aads-disk-offload'; then
            echo 'aads-disk-offload already registered'
            exit 0
        fi
        cron_file="$(mktemp /tmp/aads-disk-offload-cron.XXXXXX)" || exit 1
        trap 'rm -f -- "$cron_file"' EXIT
        printf '%s\n%s\n' "$cron_text" \
            '0 3 * * * /root/aads/aads-server/scripts/disk_offload_cafe24.sh # aads-disk-offload' \
            >"$cron_file"
        crontab "$cron_file" || exit 1
        echo 'aads-disk-offload registered'
        exit 0
        ;;
    '') ;;
    *) echo "usage: $0 [--dry-run|--install-cron]" >&2; exit 2 ;;
esac

LOG="${AADS_OFFLOAD_LOG:-/root/aads/logs/disk_offload_cafe24.log}"
WORKTREES="/root/aads/.worktrees"
DUMPS="/root/aads/backups/disk_offload"
REMOTE="root@114.207.244.86"
REMOTE_ROOT="/home/danharoo/www/data/files/goods/goodscode/_aads-backup"
DAY="$(date +%Y%m%d)"
SSH_OPTS='-p 7916 -o BatchMode=yes -o ConnectTimeout=15 -o ServerAliveInterval=20 -o ServerAliveCountMax=6'
RSYNC_SSH="ssh $SSH_OPTS"
# 덤프 rsync 재시도(재개) 설정. 7.9G 덤프가 약 1분마다 끊기는 회선에서도
# --partial --inplace 로 이어 보낸다.
RSYNC_RETRIES="${AADS_OFFLOAD_RSYNC_RETRIES:-8}"
RSYNC_RETRY_SLEEP="${AADS_OFFLOAD_RSYNC_RETRY_SLEEP:-15}"
RSYNC_ATTEMPT_TIMEOUT="${AADS_OFFLOAD_RSYNC_ATTEMPT_TIMEOUT:-1800}"
# 실패 알림 쿨다운(초). cron 이 하루 한 번 돌지만 수동 재실행이 겹쳐도 폭주하지 않게 한다.
ALERT_COOLDOWN_SEC="${AADS_OFFLOAD_ALERT_COOLDOWN_SEC:-21600}"
ALERT_STAMP="${AADS_OFFLOAD_ALERT_STAMP:-/tmp/aads-disk-offload-alert.stamp}"
# 방금 만든 worktree가 pipeline_jobs 에 등록되기 전에 삭제되지 않도록 최소 경과
# 시간을 둔다. auto_trigger.sh 는 worktree 생성 직후 바로 작업을 시작하므로
# 2시간이면 등록/첫 커밋 어느 쪽으로든 상태가 갈린다(R1 리뷰 지적 1).
WORKTREE_MIN_AGE_SEC="${AADS_OFFLOAD_MIN_AGE_SEC:-7200}"
# pg_dump 전 최소 여유 공간(R1 리뷰 지적 3). 03:00 정기 백업과 겹쳐도 안전하도록
# 넉넉히 잡는다.
MIN_FREE_GB="${AADS_OFFLOAD_MIN_FREE_GB:-5}"
ERRORS=0

mkdir -p "$(dirname "$LOG")" || exit 1
exec >>"$LOG" 2>&1
log() { printf '%s %s\n' "$(date '+%F %T')" "$*" >&2; }
fail() { log "ERROR $*"; ERRORS=$((ERRORS + 1)); }
# 텔레그램 자격증명은 .env 를 통째로 source 하지 않고 필요한 두 줄만 뽑는다
# (codex_auth_sync.sh 와 같은 방식 — .env 에 source 하면 죽는 값이 있다).
load_telegram_creds() {
    local f line val
    for f in /root/aads/aads-server/.env /root/aads/.env; do
        [[ -f "$f" ]] || continue
        while IFS= read -r line; do
            case "$line" in
                TELEGRAM_BOT_TOKEN=*) val="${line#TELEGRAM_BOT_TOKEN=}" ;;
                TELEGRAM_CHAT_ID=*)   val="${line#TELEGRAM_CHAT_ID=}" ;;
                *) continue ;;
            esac
            val="${val%\"}"; val="${val#\"}"; val="${val%\'}"; val="${val#\'}"
            case "$line" in
                TELEGRAM_BOT_TOKEN=*) if [[ -n "$val" ]]; then TELEGRAM_BOT_TOKEN="$val"; fi ;;
                TELEGRAM_CHAT_ID=*)   if [[ -n "$val" ]]; then TELEGRAM_CHAT_ID="$val"; fi ;;
            esac
        done < "$f"
    done
}

notify_errors() {
    local bot chat now last=0 text
    (( ERRORS > 0 )) || return 0
    if (( DRY_RUN )); then
        log "DRY-RUN telegram alert skipped (errors=$ERRORS)"
        return 0
    fi
    TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-}"
    TELEGRAM_CHAT_ID="${TELEGRAM_CHAT_ID:-}"
    load_telegram_creds
    bot="$TELEGRAM_BOT_TOKEN"; chat="$TELEGRAM_CHAT_ID"
    if [[ -z "$bot" || -z "$chat" ]]; then
        log 'WARN telegram credentials missing; alert not sent'
        return 0
    fi
    now="$(date +%s)"
    [[ -f "$ALERT_STAMP" ]] && last="$(cat "$ALERT_STAMP" 2>/dev/null)"
    [[ "$last" =~ ^[0-9]+$ ]] || last=0
    if (( now - last < ALERT_COOLDOWN_SEC )); then
        log "SKIP telegram alert: cooldown ${ALERT_COOLDOWN_SEC}s"
        return 0
    fi
    text="[AADS disk offload] errors=${ERRORS} ($(hostname)). 로그: ${LOG}"
    if timeout 20 curl -sf -X POST "https://api.telegram.org/bot${bot}/sendMessage" \
        -d chat_id="$chat" --data-urlencode text="$text" >/dev/null 2>&1; then
        printf '%s\n' "$now" >"$ALERT_STAMP"
        log 'telegram alert sent'
    else
        log 'WARN telegram alert failed'
    fi
}
finish() {
    log "FINISH errors=$ERRORS dry_run=$DRY_RUN"
    notify_errors
    # The final log line is always the requested root filesystem measurement.
    df -h / | tail -1
}
trap finish EXIT

exec 9>/tmp/aads-disk-offload.lock
if ! flock -n 9; then
    log 'SKIP another offload is running'
    exit 0
fi
log "START dry_run=$DRY_RUN day=$DAY"

# 실행 중인 빌드/배포와 캐시 정리가 겹치지 않도록 같은 lock 을 공유한다.
# 빌드 캐시는 재생성 가능한 산출물이라 원격 백업 대상이 아니다(위 R1 4번 참고).
exec 8>"${AADS_DEPLOY_FLOCKFILE:-/tmp/aads-deploy.flock}"
if flock -n 8; then
    if (( DRY_RUN )); then
        log 'DRY-RUN docker builder prune --filter until=24h -f'
    elif ! timeout 300 docker builder prune --filter until=24h -f; then
        fail 'builder prune failed or timed out'
    fi
    flock -u 8
else
    log 'SKIP builder prune: deployment lock held'
fi

# Query all active rows. Searching the complete row also catches paths in instruction,
# review metadata, or logs outside the worktree_path event. Query failure means no delete.
active_ref_count() {
    local path="$1" escaped result
    escaped="${path//\'/\'\'}"
    result="$(timeout 30 docker exec aads-postgres psql -X -v ON_ERROR_STOP=1 \
        -U aads -d aads -qAtc "SELECT count(*) FROM pipeline_jobs pj
          WHERE status IN ('running','queued','awaiting_approval','claimed','approved','deploying','rolling_back')
          AND position('${escaped}' in row_to_json(pj)::text) > 0" 2>&1)" || {
        fail "pipeline_jobs query failed for $path: $result"
        return 1
    }
    [[ "$result" =~ ^[0-9]+$ ]] || { fail "invalid pipeline_jobs result for $path: $result"; return 1; }
    ACTIVE_REFS="$result"
}

worktree_age_sec() {
    local wt="$1" mtime now
    mtime="$(stat -c %Y "$wt/.git" 2>/dev/null || stat -c %Y "$wt" 2>/dev/null)" || return 1
    now="$(date +%s)"
    echo $((now - mtime))
}

eligible() {
    local wt="$1" root status age
    [[ -d "$wt" && ! -L "$wt" ]] || { log "SKIP non-directory/symlink $wt"; return 1; }
    root="$(git -C "$wt" rev-parse --show-toplevel 2>/dev/null)" || { log "SKIP non-git $wt"; return 1; }
    [[ "$root" == "$wt" ]] || { log "SKIP nested/non-root $wt"; return 1; }
    age="$(worktree_age_sec "$wt")" || { log "SKIP cannot stat $wt"; return 1; }
    (( age >= WORKTREE_MIN_AGE_SEC )) || { log "SKIP too new age=${age}s $wt"; return 1; }
    status="$(git -C "$wt" status --short --untracked-files=all 2>&1)" || { log "SKIP status failure $wt: $status"; return 1; }
    [[ -z "$status" ]] || { log "SKIP dirty $wt"; return 1; }
    git -C "$wt" merge-base --is-ancestor HEAD origin/main 2>/dev/null || { log "SKIP unmerged/unknown origin/main $wt"; return 1; }
    active_ref_count "$wt" || return 1
    [[ "$ACTIVE_REFS" == 0 ]] || { log "SKIP active pipeline refs=$ACTIVE_REFS $wt"; return 1; }
    return 0
}

# 끊겨도 --partial --inplace 로 이어 받는다. 마지막 시도까지 실패하면 1 을 반환한다.
rsync_resume() {
    local src="$1" dst="$2" attempt rc=1
    for (( attempt = 1; attempt <= RSYNC_RETRIES; attempt++ )); do
        timeout "$RSYNC_ATTEMPT_TIMEOUT" rsync -a --partial --inplace \
            -e "$RSYNC_SSH" "$src" "$dst"
        rc=$?
        (( rc == 0 )) && return 0
        log "WARN rsync attempt ${attempt}/${RSYNC_RETRIES} rc=${rc} src=$src"
        (( attempt < RSYNC_RETRIES )) && sleep "$RSYNC_RETRY_SLEEP"
    done
    return "$rc"
}

# 그 worktree 를 만든 저장소의 작업 디렉터리를 돌려준다.
worktree_owner_repo() {
    local wt="$1" common
    common="$(git -C "$wt" rev-parse --git-common-dir 2>/dev/null)" || return 1
    [[ "$common" == /* ]] || common="$wt/$common"
    common="$(realpath -- "$common" 2>/dev/null)" || return 1
    [[ "${common##*/}" == .git && -d "$common" ]] || return 1
    dirname -- "$common"
}

remove_worktree() {
    local wt="$1" owner out
    owner="$(worktree_owner_repo "$wt")" || { fail "cannot resolve owner repo of $wt"; return 1; }
    if out="$(git -C "$owner" worktree remove -- "$wt" 2>&1)"; then
        return 0
    fi
    log "git worktree remove output ($owner): $out"
    case "$out" in
        *'modified or untracked files'*|*'is locked'*|*'contains submodules'*|*'use --force'*)
            log "SKIP protected worktree (dirty/locked, not removed) $wt"
            return 2 ;;
    esac
    fail "git worktree remove failed $wt"
    return 1
}

remote_mkdir() {
    timeout 30 ssh $SSH_OPTS "$REMOTE" \
        "mkdir -p -- '$1' && chmod 700 -- '$1'"
}

verify_tree() {
    local src="$1" dst="$2" local_count remote_count delta
    local_count="$(find "$src" -type f | wc -l)" || return 1
    remote_count="$(timeout 30 ssh -p 7916 -o BatchMode=yes -o ConnectTimeout=15 "$REMOTE" \
        "find '$dst' -type f | wc -l")" || return 1
    [[ "$local_count" == "$remote_count" ]] || return 1
    # A checksum dry run produces no itemized changes only when every file agrees.
    delta="$(timeout 1800 rsync -a --checksum --dry-run --delete --itemize-changes \
        -e "$RSYNC_SSH" "$src/" "$REMOTE:$dst/")" || return 1
    [[ -z "$delta" ]]
}

if [[ -d "$WORKTREES" ]]; then
    for wt in "$WORKTREES"/*; do
        [[ -e "$wt" ]] || continue
        name="${wt##*/}"
        [[ "$name" =~ ^[A-Za-z0-9._-]+$ ]] || { log "SKIP unsafe name $wt"; continue; }
        eligible "$wt" || continue
        dst="$REMOTE_ROOT/$DAY/worktrees/$name"
        if (( DRY_RUN )); then
            log "DRY-RUN worktree offload candidate $wt -> $dst; source preserved"
            continue
        fi
        if ! remote_mkdir "$dst" || ! timeout 1800 rsync -a --checksum \
            -e "$RSYNC_SSH" "$wt/" "$REMOTE:$dst/"; then
            fail "worktree rsync failed $wt"
            continue
        fi
        if ! verify_tree "$wt" "$dst"; then
            fail "worktree file count/checksum mismatch $wt"
            continue
        fi
        # Recheck immediately before removing a source that a runner may have touched.
        eligible "$wt" || { fail "worktree changed or became active after transfer $wt"; continue; }
        if ! verify_tree "$wt" "$dst"; then
            fail "worktree changed during final verification $wt"
            continue
        fi
        # git worktree remove 는 dirty 상태면 스스로 거부하므로 rm -rf 보다 안전하고,
        # .git/worktrees 등록 정보도 함께 정리한다(R1 리뷰 지적 5).
        remove_worktree "$wt"
        rm_rc=$?
        (( rm_rc == 0 )) || continue
        log "OFFLOADED worktree $wt -> $dst"
    done
fi

dump="$DUMPS/aads_$DAY.dump"
dump_dst="$REMOTE_ROOT/$DAY/postgres/aads_$DAY.dump"
if (( DRY_RUN )); then
    log "DRY-RUN free space check >= ${MIN_FREE_GB}GB on $(dirname "$DUMPS")"
    log "DRY-RUN timeout 1800 docker exec aads-postgres pg_dump -U aads -d aads -Fc > $dump"
    log "DRY-RUN dump rsync/checksum -> $dump_dst; local retention unchanged"
else
    mkdir -p "$DUMPS" || { fail "cannot create $DUMPS"; exit 1; }
    chmod 700 "$DUMPS" || fail "cannot chmod $DUMPS"
    free_kb="$(df -Pk "$DUMPS" | awk 'NR==2 {print $4}')"
    if [[ ! "$free_kb" =~ ^[0-9]+$ ]] || (( free_kb < MIN_FREE_GB * 1024 * 1024 )); then
        fail "insufficient free space before pg_dump: ${free_kb:-unknown}KB < ${MIN_FREE_GB}GB"
    else
        # umask 077: 덤프 파일이 rsync 로 /www 하위 경로에 나가므로 소유자 외
        # 읽기 권한을 주지 않는다(R1 리뷰 지적 2 — DB 덤프 웹 노출 위험).
        tmp="$dump.partial.$$"
        if (umask 077; timeout 1800 docker exec aads-postgres pg_dump -U aads -d aads -Fc >"$tmp") && [[ -s "$tmp" ]]; then
            mv -f -- "$tmp" "$dump"
            chmod 600 "$dump" || fail "cannot chmod $dump"
            if remote_mkdir "$REMOTE_ROOT/$DAY/postgres" && \
                rsync_resume "$dump" "$REMOTE:$dump_dst" && \
                timeout 30 ssh -p 7916 -o BatchMode=yes -o ConnectTimeout=15 "$REMOTE" "chmod 600 -- '$dump_dst'"; then
                local_sum="$(sha256sum "$dump" | awk '{print $1}')"
                remote_sum="$(timeout 1800 ssh -p 7916 -o BatchMode=yes -o ConnectTimeout=15 "$REMOTE" \
                    "sha256sum '$dump_dst'" | awk '{print $1}')" || remote_sum=''
                if [[ -n "$local_sum" && "$local_sum" == "$remote_sum" ]]; then
                    log "OFFLOADED dump $dump checksum=$local_sum files=1"
                    # Keep two newest local dumps. Older dumps require their own matching
                    # remote copy before removal; preserve them if verification fails.
                    mapfile -t old_dumps < <(find "$DUMPS" -maxdepth 1 -type f -name 'aads_????????.dump' | sort -r | tail -n +3)
                    for old in "${old_dumps[@]}"; do
                        old_name="${old##*/}"
                        old_day="${old_name#aads_}"; old_day="${old_day%.dump}"
                        [[ "$old_day" =~ ^[0-9]{8}$ ]] || continue
                        old_dst="$REMOTE_ROOT/$old_day/postgres/$old_name"
                        old_local_sum="$(sha256sum "$old" | awk '{print $1}')"
                        old_remote_sum="$(timeout 1800 ssh -p 7916 -o BatchMode=yes -o ConnectTimeout=15 "$REMOTE" \
                            "sha256sum '$old_dst'" 2>/dev/null | awk '{print $1}')" || old_remote_sum=''
                        if [[ -n "$old_local_sum" && "$old_local_sum" == "$old_remote_sum" ]]; then
                            rm -f -- "$old" && log "REMOVED verified old local dump $old"
                        else
                            fail "old dump unverified; retained $old"
                        fi
                    done
                else
                    fail "dump checksum mismatch; retained $dump"
                fi
            else
                fail "dump rsync failed; retained $dump"
            fi
        else
            rm -f -- "$tmp"
            fail 'pg_dump failed, timed out, or produced an empty file'
        fi
    fi
fi

(( ERRORS == 0 ))
