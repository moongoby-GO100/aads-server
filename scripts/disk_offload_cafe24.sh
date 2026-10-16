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
#  8. 덤프 rsync 는 --partial --append + ssh keepalive + 재시도(재개)로 보낸다.
#  9. 종료 시 errors>0 이면 CEO 오비스 알림함에 6시간 중복방지로 기록한다.
# 10. 전날 전송에 실패한 덤프도 매일 먼저 백필하고, 원격 SHA-256이 일치하는
#     오래된 로컬 덤프만 지운다. 새 덤프 전에는 최근 덤프 크기+여유공간을 요구한다.
# 2026-10-15 수정(AADS-DISK-OFFLOAD-KEEP-ONE-DUMP-20261015):
# 11. 로컬 덤프 보존 수를 2→1(LOCAL_DUMP_KEEP)로 줄였다. 덤프 2개(약 16GB) 상시 보존으로
#     contabo116 여유가 9GB 아래로 떨어져 10-16 DB 백업(약 12.6GiB)이 위험했다.
#     삭제 조건(원격 sha256 일치)은 그대로다.
# 2026-10-16 수정(AADS-DISK-OFFLOAD-DUMP-SPACE-DEADLOCK-20261016):
# 12. 덤프 직전 여유공간이 부족하면 더 이상 덤프를 건너뛰지 않고 카페24로 직접
#     스트리밍한다(pg_dump | tee sha256 | ssh cat > 원격.partial). 로컬 덤프 파일은
#     남기지 않고, pg_dump/ssh 종료코드와 원격 sha256·크기를 모두 확인한 뒤에만
#     원격 최종 이름(aads_YYYYMMDD.dump)으로 mv 한다. 로컬 경로 판정식은 그대로다.
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
# --partial --append 로 이어 보낸다. 완성 후 SHA-256으로 검증하고, 불일치하면
# 원격 불완전본을 격리한 다음 처음부터 한 번 더 전송한다.
RSYNC_RETRIES="${AADS_OFFLOAD_RSYNC_RETRIES:-30}"
RSYNC_RETRY_SLEEP="${AADS_OFFLOAD_RSYNC_RETRY_SLEEP:-15}"
RSYNC_ATTEMPT_TIMEOUT="${AADS_OFFLOAD_RSYNC_ATTEMPT_TIMEOUT:-1800}"
# 방금 만든 worktree가 pipeline_jobs 에 등록되기 전에 삭제되지 않도록 최소 경과
# 시간을 둔다. auto_trigger.sh 는 worktree 생성 직후 바로 작업을 시작하므로
# 2시간이면 등록/첫 커밋 어느 쪽으로든 상태가 갈린다(R1 리뷰 지적 1).
WORKTREE_MIN_AGE_SEC="${AADS_OFFLOAD_MIN_AGE_SEC:-7200}"
# pg_dump 전 최소 여유 공간(R1 리뷰 지적 3). 03:00 정기 백업과 겹쳐도 안전하도록
# 넉넉히 잡는다.
MIN_FREE_GB="${AADS_OFFLOAD_MIN_FREE_GB:-5}"
EXPECTED_DUMP_GB="${AADS_OFFLOAD_EXPECTED_DUMP_GB:-8}"
# 로컬에 남길 최신 덤프 개수. 나머지는 원격 sha256 일치 시에만 삭제한다.
LOCAL_DUMP_KEEP=1
ERRORS=0

mkdir -p "$(dirname "$LOG")" || exit 1
exec >>"$LOG" 2>&1
log() { printf '%s %s\n' "$(date '+%F %T')" "$*" >&2; }
fail() { log "ERROR $*"; ERRORS=$((ERRORS + 1)); }
notify_errors() {
    local result
    (( ERRORS > 0 )) || return 0
    if (( DRY_RUN )); then
        log "DRY-RUN OHVIS alert skipped (errors=$ERRORS)"
        return 0
    fi
    result="$(timeout 30 docker exec aads-postgres psql -X -v ON_ERROR_STOP=1 \
        -U aads -d aads -qAtc \
        "INSERT INTO alert_history (severity, category, title, message, server, project, acknowledged)
         SELECT 'critical', 'backup', 'AADS 디스크 오프로드 실패',
                'errors=${ERRORS}; server=contabo116; log=/root/aads/logs/disk_offload_cafe24.log',
                'contabo116', 'AADS', false
         WHERE NOT EXISTS (
             SELECT 1 FROM alert_history
             WHERE title = 'AADS 디스크 오프로드 실패'
               AND created_at > NOW() - INTERVAL '6 hours'
         )
         RETURNING id" 2>&1)" && {
        if [[ "$result" =~ ^[0-9]+$ ]]; then
            log "OHVIS alert recorded id=$result"
        else
            log 'SKIP OHVIS alert: duplicate within 6 hours'
        fi
        return 0
    }
    if [[ -n "$result" ]]; then
        log "WARN OHVIS alert failed: ${result:0:240}"
    else
        log 'WARN OHVIS alert failed'
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
        timeout "$RSYNC_ATTEMPT_TIMEOUT" rsync -a --partial --append \
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

dump_remote_path() {
    local local_dump="$1" name day
    name="${local_dump##*/}"
    [[ "$name" =~ ^aads_([0-9]{8})\.dump$ ]] || return 1
    day="${BASH_REMATCH[1]}"
    printf '%s/%s/postgres/%s\n' "$REMOTE_ROOT" "$day" "$name"
}

offload_dump_file() {
    local local_dump="$1" remote_dump local_sum remote_sum quarantine
    remote_dump="$(dump_remote_path "$local_dump")" || return 1
    remote_mkdir "$(dirname "$remote_dump")" || return 1
    rsync_resume "$local_dump" "$REMOTE:$remote_dump" || return 1
    timeout 30 ssh $SSH_OPTS "$REMOTE" "chmod 600 -- '$remote_dump'" || return 1
    local_sum="$(sha256sum "$local_dump" | awk '{print $1}')"
    remote_sum="$(timeout 1800 ssh $SSH_OPTS "$REMOTE" \
        "sha256sum '$remote_dump'" 2>/dev/null | awk '{print $1}')" || remote_sum=''
    if [[ -z "$local_sum" || "$local_sum" != "$remote_sum" ]]; then
        quarantine="${remote_dump}.checksum-mismatch.$(date +%s)"
        log "WARN dump checksum mismatch; quarantining remote file $remote_dump"
        timeout 30 ssh $SSH_OPTS "$REMOTE" "mv -- '$remote_dump' '$quarantine'" || return 1
        rsync_resume "$local_dump" "$REMOTE:$remote_dump" || return 1
        timeout 30 ssh $SSH_OPTS "$REMOTE" "chmod 600 -- '$remote_dump'" || return 1
        remote_sum="$(timeout 1800 ssh $SSH_OPTS "$REMOTE" \
            "sha256sum '$remote_dump'" 2>/dev/null | awk '{print $1}')" || remote_sum=''
        [[ -n "$local_sum" && "$local_sum" == "$remote_sum" ]] || return 1
        timeout 30 ssh $SSH_OPTS "$REMOTE" "rm -f -- '$quarantine'" || \
            log "WARN verified dump but could not remove quarantined remote file $quarantine"
    fi
    log "OFFLOADED dump $local_dump checksum=$local_sum files=1"
}

prune_verified_old_dumps() {
    local old old_remote old_local_sum old_remote_sum
    local -a old_dumps=()
    mapfile -t old_dumps < <(
        find "$DUMPS" -maxdepth 1 -type f -name 'aads_????????.dump' | sort -r | tail -n +$((LOCAL_DUMP_KEEP + 1))
    )
    for old in "${old_dumps[@]}"; do
        old_remote="$(dump_remote_path "$old")" || continue
        old_local_sum="$(sha256sum "$old" | awk '{print $1}')"
        old_remote_sum="$(timeout 1800 ssh $SSH_OPTS "$REMOTE" \
            "sha256sum '$old_remote'" 2>/dev/null | awk '{print $1}')" || old_remote_sum=''
        if [[ -n "$old_local_sum" && "$old_local_sum" == "$old_remote_sum" ]]; then
            rm -f -- "$old" && log "REMOVED verified old local dump $old"
        else
            fail "old dump unverified; retained $old"
        fi
    done
}

# 로컬 여유공간 판정: 충분하면 local, 부족하거나 값을 알 수 없으면 stream.
choose_dump_mode() {
    local free_kb="$1" required_kb="$2"
    if [[ "$free_kb" =~ ^[0-9]+$ && "$required_kb" =~ ^[0-9]+$ ]] && (( free_kb >= required_kb )); then
        echo local
    else
        echo stream
    fi
}

# pg_dump 를 로컬 파일 없이 카페24 로 바로 보낸다. 실패 사유는 STREAM_FAIL_REASON 에 남긴다.
STREAM_FAIL_REASON=''
stream_dump_to_remote() {
    local local_dump="$1" remote_final remote_part sumfile local_sum remote_out remote_sum remote_size i
    local -a rcs
    STREAM_FAIL_REASON=''
    remote_final="$(dump_remote_path "$local_dump")" || { STREAM_FAIL_REASON='cannot derive remote path'; return 1; }
    remote_part="${remote_final}.partial.$$"
    remote_mkdir "$(dirname "$remote_final")" || { STREAM_FAIL_REASON='remote mkdir failed'; return 1; }
    sumfile="$(mktemp /tmp/aads-offload-sum.XXXXXX)" || { STREAM_FAIL_REASON='mktemp failed'; return 1; }

    timeout "${AADS_OFFLOAD_STREAM_TIMEOUT:-1800}" docker exec aads-postgres pg_dump -U aads -d aads -Fc \
        | tee >(sha256sum | awk '{print $1}' >"$sumfile") \
        | timeout "${AADS_OFFLOAD_STREAM_TIMEOUT:-1800}" ssh $SSH_OPTS "$REMOTE" "umask 077; cat > '$remote_part'"
    rcs=("${PIPESTATUS[@]}")

    # tee 의 sha256sum 프로세스 치환은 파이프라인 종료 뒤에 끝날 수 있어 잠시 기다린다.
    for (( i = 0; i < 100; i++ )); do
        [[ -s "$sumfile" ]] && break
        sleep 0.1
    done
    local_sum="$(tr -d '[:space:]' <"$sumfile")"
    rm -f -- "$sumfile"

    if (( rcs[0] != 0 || rcs[2] != 0 )); then
        STREAM_FAIL_REASON="pipeline exit pg_dump=${rcs[0]} ssh=${rcs[2]}"
    elif [[ ! "$local_sum" =~ ^[0-9a-f]{64}$ ]]; then
        STREAM_FAIL_REASON='local stream sha256 missing'
    else
        remote_out="$(timeout 1800 ssh $SSH_OPTS "$REMOTE" \
            "sha256sum '$remote_part' && stat -c %s '$remote_part'" 2>/dev/null)" || remote_out=''
        remote_sum="$(printf '%s\n' "$remote_out" | awk 'NR==1 {print $1}')"
        remote_size="$(printf '%s\n' "$remote_out" | awk 'NR==2 {print $1}')"
        if [[ -z "$remote_sum" || "$remote_sum" != "$local_sum" ]]; then
            STREAM_FAIL_REASON="remote sha256 mismatch or unreadable (local=$local_sum remote=${remote_sum:-none})"
        elif [[ ! "$remote_size" =~ ^[0-9]+$ ]] || (( remote_size <= 0 )); then
            STREAM_FAIL_REASON="remote dump size invalid (${remote_size:-none})"
        elif ! timeout 30 ssh $SSH_OPTS "$REMOTE" \
            "mv -f -- '$remote_part' '$remote_final' && chmod 600 -- '$remote_final'"; then
            STREAM_FAIL_REASON='remote finalize (mv/chmod) failed'
        else
            log "OFFLOADED dump (streamed, no local file) $remote_final checksum=$local_sum bytes=$remote_size"
            return 0
        fi
    fi
    timeout 30 ssh $SSH_OPTS "$REMOTE" "rm -f -- '$remote_part'" \
        || log "WARN could not remove remote partial $remote_part"
    return 1
}

backfill_retained_dumps() {
    local retained
    local -a retained_dumps=()
    mapfile -t retained_dumps < <(
        find "$DUMPS" -maxdepth 1 -type f -name 'aads_????????.dump' | sort
    )
    for retained in "${retained_dumps[@]}"; do
        if ! offload_dump_file "$retained"; then
            fail "retained dump offload failed; retained $retained"
        fi
    done
    prune_verified_old_dumps
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
dump_dst="$(dump_remote_path "$dump")"
if (( DRY_RUN )); then
    log "DRY-RUN retained dump append backfill; source preserved until SHA-256 match"
    log "DRY-RUN free space check >= latest dump size + ${MIN_FREE_GB}GB on $(dirname "$DUMPS")"
    log "DRY-RUN timeout 1800 docker exec aads-postgres pg_dump -U aads -d aads -Fc > $dump"
    log "DRY-RUN dump rsync/checksum -> $dump_dst; local retention unchanged"
    log "DRY-RUN if free space is short: streaming path pg_dump | tee sha256 | ssh cat > ${dump_dst}.partial.<pid>, verify sha256+size, mv -> $dump_dst (no local file, timeout ${AADS_OFFLOAD_STREAM_TIMEOUT:-1800}s)"
else
    mkdir -p "$DUMPS" || { fail "cannot create $DUMPS"; exit 1; }
    chmod 700 "$DUMPS" || fail "cannot chmod $DUMPS"
    backfill_retained_dumps
    free_kb="$(df -Pk "$DUMPS" | awk 'NR==2 {print $4}')"
    latest_dump_bytes="$(find "$DUMPS" -maxdepth 1 -type f -name 'aads_????????.dump' \
        -printf '%s\n' | sort -nr | head -n 1)"
    [[ "$latest_dump_bytes" =~ ^[0-9]+$ ]] || latest_dump_bytes=$((EXPECTED_DUMP_GB * 1024 * 1024 * 1024))
    expected_dump_kb=$(((latest_dump_bytes + 1023) / 1024))
    required_free_kb=$((expected_dump_kb + MIN_FREE_GB * 1024 * 1024))
    if [[ "$(choose_dump_mode "$free_kb" "$required_free_kb")" == stream ]]; then
        log "WARN insufficient free space before pg_dump: ${free_kb:-unknown}KB < ${required_free_kb}KB (latest dump + ${MIN_FREE_GB}GB reserve); streaming directly to $dump_dst"
        if ! stream_dump_to_remote "$dump"; then
            fail "pg_dump skipped locally (free ${free_kb:-unknown}KB < ${required_free_kb}KB) -> streaming to cafe24 failed: ${STREAM_FAIL_REASON:-unknown}"
        fi
    else
        # umask 077: 덤프 파일이 rsync 로 /www 하위 경로에 나가므로 소유자 외
        # 읽기 권한을 주지 않는다(R1 리뷰 지적 2 — DB 덤프 웹 노출 위험).
        tmp="$dump.partial.$$"
        if (umask 077; timeout 1800 docker exec aads-postgres pg_dump -U aads -d aads -Fc >"$tmp") && [[ -s "$tmp" ]]; then
            mv -f -- "$tmp" "$dump"
            chmod 600 "$dump" || fail "cannot chmod $dump"
            if offload_dump_file "$dump"; then
                prune_verified_old_dumps
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
