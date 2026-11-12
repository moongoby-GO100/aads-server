#!/usr/bin/env bash
# Reclaim expired, registered runner worktrees after preserving local work.
# 시간 기준(RUNNER_WT_RETENTION_HOURS, 하한 24h)은 '최소 보존 기간'을 보장하는 하한이다.
# 수량 상한(RUNNER_WT_MAX_KEEP)은 회전이 24h 보다 빨라 시간 기준이 못 지우는 잉여분을 자르는 상한이다.
#
# 즉시 회수 모드(RUNNER_WT_ONLY_JOB=<job_id> RUNNER_WT_ONLY_STATUS=<종료 상태>):
#   러너가 job 을 끝낸 직후 그 job 의 worktree 하나만 대상으로 한다. 시간·수량 기준을 건너뛰는 대신
#   "깨끗한 것"만 지운다 — 미커밋/ignored 변경, origin/main 에 없는 커밋, 열린 프로세스 중 하나라도
#   있으면 보존하고(기존 24h·patch 보존 정책으로 넘긴다) 아무것도 아카이브하지 않는다.
#   단, 재생성 가능한 ignored 캐시(__pycache__, .ruff_cache, .pytest_cache, .mypy_cache, .next, node_modules, *.pyc,
#   tsconfig.tsbuildinfo)만 남은 경우는 깨끗한 것으로 본다.
#   종료 상태(done|error|cancelled|rejected_done|failed)가 아니면(awaiting_approval 등) 손대지 않는다.
#   예외(RUNNER_WT_IMMEDIATE_ARCHIVE_DIRTY, 기본 1): 작업을 끝내지 못한 종료(error|cancelled|failed)는
#   미커밋·미추적·미병합 작업을 archive_dir 에 먼저 보존(번들 + 미추적 파일 내용까지 담은 패치, 검증 후 이동)하고 지운다.
#   2026-11-12 contabo14: error/cancelled 는 거의 항상 미커밋 변경이 있어 즉시 회수가 '깨끗하지 않음' 으로 보존했고,
#   폴더(260~290MB)가 24h 시간 기준까지 남았다. done/rejected_done 은 종전대로 깨끗한 것만 지운다.
#   폴더에 .preserve 파일이 있으면 어떤 모드에서도 지우지 않는다.
set -euo pipefail

repo="${RUNNER_WT_REPO:-/root/aads/aads-server}"
retention="${RUNNER_WT_RETENTION_HOURS:-24}"
reclaimed=0 freed=0 archived=0 skipped_active=0 skipped_recent=0 skipped_unknown=0 skipped_unknown_repo=0 skipped_archive_failed=0
reclaimed_over_cap=0 preserved_unclean=0 preserved_marker=0
only_job="${RUNNER_WT_ONLY_JOB:-}"
only_status="${RUNNER_WT_ONLY_STATUS:-}"
only_phase="${RUNNER_WT_ONLY_PHASE-__unset__}"
only_detail="${RUNNER_WT_ONLY_ERROR_DETAIL:-}"
only_is_rework="${RUNNER_WT_ONLY_IS_REWORK:-0}"
allow_dirty_archive=0
if [[ "${RUNNER_WT_IMMEDIATE_ARCHIVE_DIRTY:-1}" == 1 ]]; then
    case "$only_status" in error|cancelled|failed) allow_dirty_archive=1 ;; esac
fi
# 산출물 재사용이 예정된 phase 는 즉시 아카이브·삭제하지 않는다(종전: 깨끗하면 삭제, 아니면 24h 시간 기준까지 보존).
#  - review_failed: 러너가 "ARTIFACT_MAX_AGE_HOURS 시간 보존 후 자동 회수" 라고 안내한다(오판 가능성).
#  - push_stale_base / deploy_isolated_*: pipeline_auto_rework.AUTO_REWORK_FAILURE_PHASES — 자동 재작업이 부모 산출물을 쓴다.
#  - review_hold*: 검수 판정·재검수 대기(review-hold-sweeper). 상태는 review_hold 라 이미 비종료지만 error 로 끝난 변종도 포함.
#  - is_rework_candidate: pipeline_auto_rework.is_request_changes_failure 와 동치인 SQL 판정 결과(stale_base 등 포함)가 1이면 재작업 대상.
#  - fail-closed: phase 를 알 수 없으면(미전달 __unset__, 조회 실패 __unknown__) 삭제를 허용하지 않는다.
case "$only_phase" in
    __unset__|__unknown__|review_failed|review_hold|review_hold_*|push_stale_base|deploy_isolated_push_state|deploy_isolated_stale_approval|ai_review)
        allow_dirty_archive=0 ;;
    error)
        case "$only_detail" in
            deploy_isolated_stale_approval*) allow_dirty_archive=0 ;;
            deploy_isolated_push_state*)
                if [[ "$only_detail" == *stale_base* && "$only_detail" != *fetch_fail* ]]; then allow_dirty_archive=0; fi ;;
        esac ;;
esac
if [[ "$only_is_rework" == "1" || "$only_is_rework" == "t" || "$only_is_rework" == "true" ]]; then
    allow_dirty_archive=0
fi
archive_max_mb="${RUNNER_WT_ARCHIVE_MAX_PATCH_MB:-50}"
if [[ ! "$archive_max_mb" =~ ^[0-9]+$ ]]; then
    echo "[reclaim-wt] invalid RUNNER_WT_ARCHIVE_MAX_PATCH_MB: $archive_max_mb" >&2
    exit 2
fi
if [[ -n "$only_job" && ! "$only_job" =~ ^runner-[0-9a-zA-Z_-]+$ ]]; then
    echo "[reclaim-wt] invalid RUNNER_WT_ONLY_JOB: $only_job" >&2
    exit 2
fi
if [[ ! "$retention" =~ ^[0-9]+$ ]]; then
    echo "[reclaim-wt] invalid RUNNER_WT_RETENTION_HOURS: $retention" >&2
    exit 2
fi
(( retention < 24 )) && retention=24
unknown_retention="${RUNNER_WT_UNKNOWN_RETENTION_HOURS:-168}"
if [[ ! "$unknown_retention" =~ ^[0-9]+$ ]]; then
    echo "[reclaim-wt] invalid RUNNER_WT_UNKNOWN_RETENTION_HOURS: $unknown_retention" >&2
    exit 2
fi

# The hourly cron does not inherit the runner service's EnvironmentFile.
# Read only this numeric setting, without sourcing credentials or shell code.
runner_env="${RUNNER_WT_ENV_FILE:-/root/.config/aads-runner.env}"
configured_age="${ARTIFACT_MAX_AGE_HOURS:-}"
if [[ -f "$runner_env" ]]; then
    file_age="$(sed -nE "s/^[[:space:]]*(export[[:space:]]+)?ARTIFACT_MAX_AGE_HOURS=['\"]?([0-9]+)['\"]?[[:space:]]*(#.*)?$/\2/p" "$runner_env" | tail -1)"
    if [[ "$file_age" =~ ^[0-9]+$ ]]; then
        (( file_age > retention )) && retention="$file_age"
    fi
fi
if [[ "$configured_age" =~ ^[0-9]+$ ]] && (( configured_age > retention )); then
    retention="$configured_age"
fi
# 0 disables the count cap (age-only behavior). Environment wins over the env file.
max_keep_env="${RUNNER_WT_MAX_KEEP-}"
if [[ -n "$max_keep_env" && ! "$max_keep_env" =~ ^[0-9]+$ ]]; then
    echo "[reclaim-wt] invalid RUNNER_WT_MAX_KEEP: $max_keep_env" >&2
    exit 2
fi
max_keep=12
if [[ -f "$runner_env" ]]; then
    file_keep="$(sed -nE "s/^[[:space:]]*(export[[:space:]]+)?RUNNER_WT_MAX_KEEP=['\"]?([0-9]+)['\"]?[[:space:]]*(#.*)?$/\2/p" "$runner_env" | tail -1)"
    [[ "$file_keep" =~ ^[0-9]+$ ]] && max_keep=$((10#$file_keep))
fi
[[ -n "$max_keep_env" ]] && max_keep=$((10#$max_keep_env))
archive_dir="${RUNNER_WT_ARCHIVE_DIR:-/root/aads/aads-server/.runner_archive}"
archive_max_age="${RUNNER_WT_ARCHIVE_MAX_AGE_DAYS:-14}"
if [[ ! "$archive_max_age" =~ ^[0-9]+$ ]] || (( archive_max_age < 1 )); then
    echo "[reclaim-wt] invalid RUNNER_WT_ARCHIVE_MAX_AGE_DAYS: $archive_max_age" >&2
    exit 2
fi
repo_real="$(cd "$repo" && pwd -P 2>/dev/null)" || repo_real=""
compose_real=""
deploy_real=""
if [[ -d "${COMPOSE_DIR:-}" ]]; then compose_real="$(cd "$COMPOSE_DIR" && pwd -P)"; fi
if [[ -d "${AADS_DEPLOY_WORKTREE:-}" ]]; then deploy_real="$(cd "$AADS_DEPLOY_WORKTREE" && pwd -P)"; fi

if command -v lsof >/dev/null 2>&1; then
    process_probe=lsof
elif command -v fuser >/dev/null 2>&1; then
    process_probe=fuser
else
    process_probe=none
    echo "[reclaim-wt] warning: lsof/fuser unavailable; process check unavailable, preserving candidates" >&2
fi

# Query once for the whole run. Unknown job status is preserved by default.
declare -A job_status=()
db_rows=""
db_ready=0
status_source=none
# Completed jobs move to pipeline_jobs_archive after a few hours; archived rows
# keep their original status in row_data. Prefer a live row if both exist.
sql="SELECT job_id || '|' || status FROM pipeline_jobs WHERE job_id LIKE 'runner-%'
UNION ALL
SELECT a.job_id || '|' || (a.row_data->>'status') FROM pipeline_jobs_archive a
WHERE a.job_id LIKE 'runner-%'
AND NOT EXISTS (SELECT 1 FROM pipeline_jobs p WHERE p.job_id = a.job_id)"
# 즉시 회수 모드는 호출자(러너)가 종료 상태를 넘기므로 DB 를 조회하지 않는다.
# 접속 정보는 러너(_cleanup_old_artifacts)가 PG* / PG_CONTAINER 환경변수로 넘긴다.
# 원격 러너(contabo14)에는 aads-postgres 컨테이너가 없고 로컬 소켓의 DB 는 다른 DB 라서,
# PGHOST 가 주어지면 -h/-p/-U/-d 를 명시해야 한다. 비밀번호는 PGPASSWORD 환경변수로만 받는다(인자 금지).
# PG_CONTAINER 가 미설정이면 기본 aads-postgres(contabo116 기존 동작), 빈 문자열로 명시되면
# DB_MODE=psql 러너가 "컨테이너 없음"을 뜻하므로 docker 조회를 건너뛰고 PG* 로 지정한 DB 만 본다.
pg_container="${PG_CONTAINER-aads-postgres}"
psql_args=()
if [[ -n "${PGHOST:-}" ]]; then
    psql_args+=(-h "$PGHOST")
    [[ -n "${PGPORT:-}" ]] && psql_args+=(-p "$PGPORT")
    [[ -n "${PGUSER:-}" ]] && psql_args+=(-U "$PGUSER")
    [[ -n "${PGDATABASE:-}" ]] && psql_args+=(-d "$PGDATABASE")
fi
if [[ -n "$only_job" ]]; then
    :
elif [[ -n "$pg_container" ]] && command -v docker >/dev/null 2>&1 && db_rows="$(timeout 10 docker exec "$pg_container" psql -U "${PGUSER:-aads}" -d "${PGDATABASE:-aads}" -X -qAtc "$sql" 2>/dev/null)"; then
    db_ready=1
elif command -v psql >/dev/null 2>&1 && db_rows="$(timeout 10 psql ${psql_args[@]+"${psql_args[@]}"} -X -qAtc "$sql" 2>/dev/null)"; then
    db_ready=1
fi
if (( db_ready )); then
    status_source=db
elif [[ -z "$only_job" && -n "${RUNNER_WT_STATUS_FILE:-}" ]]; then
    status_max_age="${RUNNER_WT_STATUS_MAX_AGE_MIN:-120}"
    if [[ ! "$status_max_age" =~ ^[0-9]+$ ]]; then
        echo "[reclaim-wt] invalid RUNNER_WT_STATUS_MAX_AGE_MIN" >&2
        exit 2
    fi
    # Read a single inode: an atomic exporter replacement cannot mix generations.
    if [[ -f "$RUNNER_WT_STATUS_FILE" && -r "$RUNNER_WT_STATUS_FILE" ]] && { exec {status_fd}< "$RUNNER_WT_STATUS_FILE"; } 2>/dev/null; then
        status_mtime="$(stat -Lc %Y "/proc/$$/fd/$status_fd" 2>/dev/null)" || status_mtime=0
        status_now="$(date +%s)"
        if [[ "$status_mtime" =~ ^[0-9]+$ ]] && (( status_mtime > 0 && status_now - status_mtime <= 10#$status_max_age * 60 )); then
            invalid_rows=0
            while IFS= read -r row || [[ -n "$row" ]]; do
                if [[ "$row" =~ ^(runner-[0-9a-zA-Z_-]+)\|([a-z][a-z0-9_]*)$ ]]; then
                    job_status["${BASH_REMATCH[1]}"]="${BASH_REMATCH[2]}"
                    db_ready=1
                else
                    invalid_rows=$((invalid_rows + 1))
                fi
            done <&$status_fd
            (( invalid_rows == 0 )) || echo "[reclaim-wt] status file skipped invalid rows=$invalid_rows" >&2
            if (( db_ready )); then status_source=file; fi
        fi
        exec {status_fd}<&-
    fi
fi
if (( db_ready )) && [[ "$status_source" == db ]]; then
    while IFS='|' read -r id status; do
        [[ "$id" =~ ^runner-[0-9a-zA-Z_-]+$ && -n "$status" ]] && job_status["$id"]="$status"
    done <<< "$db_rows"
elif (( ! db_ready )) && [[ -z "$only_job" ]]; then
    echo "[reclaim-wt] DB unavailable; preserving unknown jobs by default (tried: docker=${pg_container:-none}, psql=${PGHOST:-local-socket}${PGPORT:+:$PGPORT})" >&2
fi

_path_busy() {
    # Returns 0 (busy) unless the probe positively reports no open files.
    local path="$1" probe_rc=0 probe_list entry rc
    [[ "$process_probe" != none ]] || return 0
    if [[ "$process_probe" == lsof ]]; then
        timeout 5 lsof +D "$path" >/dev/null 2>&1 || probe_rc=$?
    else
        # fuser on the directory alone misses open files below it.
        # Check every path and treat an incomplete traversal as busy.
        probe_list="$(mktemp)" || return 0
        if ! find "$path" -xdev -print0 > "$probe_list" 2>/dev/null; then
            probe_rc=124
        else
            while IFS= read -r -d '' entry; do
                if timeout 1 fuser "$entry" >/dev/null 2>&1; then
                    probe_rc=0
                    break
                else
                    rc=$?
                    if (( rc != 1 )); then probe_rc=124; break; fi
                fi
                probe_rc=1
            done < "$probe_list"
        fi
        rm -f -- "$probe_list"
    fi
    [[ "$probe_rc" != 1 ]]
}

# Archive local work, then remove one worktree. Returns 0 only when removed.
# cap_pass=1: the caller already probed for open files and the status gate.
# clean_only=1: remove only when nothing needs archiving (no dirty/ignored files, no unmerged commits).
_reclaim_path() {
    local path="$1" job_id="$2" common_real="$3" cap_pass="$4" clean_only="${5:-0}"
    local dirty head_sha needs_bundle merge_base size archive_ok staging item ignored_noncache ignored_count full_idx full_kb
    # Inspect tracked, untracked, and ignored changes before archiving.
    dirty="$(timeout 5 git -C "$path" status --porcelain --ignored --untracked-files=all 2>/dev/null)" || {
        skipped_archive_failed=$((skipped_archive_failed + 1))
        echo "[reclaim-wt] cannot inspect git status: $path" >&2
        return 1
    }
    head_sha="$(timeout 5 git -C "$path" rev-parse HEAD 2>/dev/null)" || {
        skipped_archive_failed=$((skipped_archive_failed + 1))
        echo "[reclaim-wt] cannot inspect HEAD: $path" >&2
        return 1
    }
    needs_bundle=0
    if ! timeout 5 git -C "$path" merge-base --is-ancestor "$head_sha" origin/main 2>/dev/null; then
        needs_bundle=1
        merge_base="$(timeout 5 git -C "$path" merge-base "$head_sha" origin/main 2>/dev/null)" || {
            skipped_archive_failed=$((skipped_archive_failed + 1))
            echo "[reclaim-wt] cannot find merge base: $path" >&2
            return 1
        }
    fi

    if [[ "$clean_only" == 1 && "$allow_dirty_archive" == 1 && -n "$dirty" ]]; then
        # dirty-full.patch 는 git add -A 로 만들어 .gitignore 대상 파일 내용을 담지 못하고 ignored.list 는 이름만 남는다.
        # 재생성 가능한 캐시 밖의 ignored(!!) 파일(.vault.key, local.db, 데이터)은 내용 보존 수단이 없으므로 삭제하지 않는다.
        # tar 로 archive_dir 에 복제하지 않는다 — 시크릿 파일이 복제된다.
        ignored_noncache="$(printf '%s\n' "$dirty" | grep '^!! ' | grep -vE '^!! (.*/)?(__pycache__|\.ruff_cache|\.pytest_cache|\.mypy_cache|\.next|node_modules)/|^!! (.*/)?[^/]+\.pyc$|^!! (.*/)?tsconfig\.tsbuildinfo$' || true)"
        if [[ -n "$ignored_noncache" ]]; then
            ignored_count="$(printf '%s\n' "$ignored_noncache" | grep -c .)"
            preserved_unclean=$((preserved_unclean + 1))
            echo "[reclaim-wt] preserved (ignored non-cache) path=$path count=$ignored_count"
            return 1
        fi
    fi

    if [[ "$clean_only" == 1 && -n "$dirty" ]]; then
        # 재생성 가능한 ignored 캐시(!! 항목)만 있으면 깨끗한 것으로 본다. 추적/미추적 변경과
        # 그 밖의 ignored 파일(.vault.key, 데이터, patch 등)은 그대로 보존 대상이다.
        dirty="$(printf '%s\n' "$dirty" | grep -vE '^!! (.*/)?(__pycache__|\.ruff_cache|\.pytest_cache|\.mypy_cache|\.next|node_modules)/|^!! (.*/)?[^/]+\.pyc$|^!! (.*/)?tsconfig\.tsbuildinfo$' | grep -v '^$' || true)"
    fi

    if [[ "$clean_only" == 1 && "$allow_dirty_archive" != 1 ]] && { [[ -n "$dirty" ]] || (( needs_bundle )); }; then
        preserved_unclean=$((preserved_unclean + 1))
        echo "[reclaim-wt] preserved (not clean) path=$path dirty=$([[ -n "$dirty" ]] && echo 1 || echo 0) unmerged=$needs_bundle"
        return 1
    fi

    if [[ "$cap_pass" != 1 ]] && _path_busy "$path"; then
        skipped_active=$((skipped_active + 1)); return 1
    fi

    size="$(du -sm "$path" 2>/dev/null | awk '{print $1}' || true)"
    size="${size:-0}"
    if [[ "${DRY_RUN:-0}" == 1 ]]; then
        echo "[reclaim-wt] DRY_RUN candidate=$path size=${size}MB bundle=$needs_bundle dirty=$([[ -n "$dirty" ]] && echo 1 || echo 0)$([[ "$cap_pass" == 1 ]] && echo ' over_cap=1')"
        return 1
    fi
    if (( needs_bundle )) || [[ -n "$dirty" ]]; then
        archive_ok=1
        mkdir -p -- "$archive_dir" || archive_ok=0
        if (( archive_ok )); then
            staging="$(mktemp -d "$archive_dir/.${job_id}.XXXXXX")" || archive_ok=0
        fi
        if (( archive_ok )) && (( needs_bundle )); then
            timeout 90 git -C "$path" bundle create "$staging/$job_id.bundle" "$merge_base..HEAD" >/dev/null 2>&1 || archive_ok=0
            if (( archive_ok )); then
                timeout 90 git -C "$path" bundle verify "$staging/$job_id.bundle" >/dev/null 2>&1 || archive_ok=0
            fi
        fi
        if (( archive_ok )) && [[ -n "$dirty" ]]; then
            timeout 90 git -C "$path" diff HEAD > "$staging/$job_id.dirty.patch" || archive_ok=0
            timeout 90 git -C "$path" ls-files -o --exclude-standard > "$staging/$job_id.untracked.list" || archive_ok=0
            # 실제 index 에만 남은 staged 내용(git add 후 작업 파일을 HEAD 로 되돌린 MM 상태)은 diff HEAD·dirty-full 어느 쪽에도 없다.
            # 읽기 전용 diff --cached 로 별도 보존한다(index 는 건드리지 않는다).
            timeout 90 git -C "$path" diff --cached --binary HEAD > "$staging/$job_id.staged.patch" 2>/dev/null || archive_ok=0
            # diff HEAD 는 미추적 파일의 내용을 담지 못한다. 임시 index 로 미추적 파일까지 담은 전체 패치를 따로 만든다(작업트리 index 는 건드리지 않는다).
            if (( archive_ok )); then
                full_idx="$(mktemp -d)" || archive_ok=0
            fi
            if (( archive_ok )); then
                { GIT_INDEX_FILE="$full_idx/index" timeout 90 git -C "$path" read-tree HEAD \
                    && GIT_INDEX_FILE="$full_idx/index" timeout 90 git -C "$path" add -A -- . \
                    && GIT_INDEX_FILE="$full_idx/index" timeout 90 git -C "$path" diff --cached --binary HEAD > "$staging/$job_id.dirty-full.patch"; } 2>/dev/null || archive_ok=0
                rm -rf -- "$full_idx"
                if (( archive_ok )) && [[ -f "$staging/$job_id.dirty-full.patch" ]]; then
                    full_kb="$(du -sk "$staging/$job_id.dirty-full.patch" "$staging/$job_id.staged.patch" | awk '{s+=$1} END {print s+0}')"
                    if [[ "$full_kb" =~ ^[0-9]+$ ]] && (( full_kb > archive_max_mb * 1024 )); then
                        echo "[reclaim-wt] archive patch too large (${full_kb}KB > ${archive_max_mb}MB): $path" >&2
                        archive_ok=0
                    fi
                fi
            fi
            timeout 90 git -C "$path" ls-files -o -i --exclude-standard > "$staging/$job_id.ignored.list" || archive_ok=0
        fi
        if (( archive_ok )); then
            for item in "$staging"/*; do
                [[ -f "$item" ]] || continue
                if [[ -e "$archive_dir/${item##*/}" ]]; then archive_ok=0; break; fi
            done
        fi
        if (( archive_ok )); then
            for item in "$staging"/*; do
                [[ -f "$item" ]] || continue
                mv -- "$item" "$archive_dir/" || { archive_ok=0; break; }
            done
        fi
        if [[ -n "${staging:-}" ]]; then
            if (( archive_ok )); then
                rmdir -- "$staging" 2>/dev/null || true
            elif [[ "$staging" == "$archive_dir/.${job_id}."* && ! -L "$staging" && -d "$staging" ]]; then
                # 실패한 아카이브의 부분 산출물(패치·번들)이 숨김 staging 에 남아 호출마다 누적되지 않게 통째로 지운다.
                rm -rf -- "$staging"
            fi
        fi
        if (( ! archive_ok )); then
            skipped_archive_failed=$((skipped_archive_failed + 1))
            echo "[reclaim-wt] archive failed: $path" >&2
            return 1
        fi
        archived=$((archived + 1))
    fi
    # A locked or otherwise protected worktree must remain protected if Git
    # refuses removal; a filesystem fallback would bypass Git's decision.
    if git --git-dir="$common_real" worktree remove --force "$path" >/dev/null 2>&1; then
        reclaimed=$((reclaimed + 1))
        freed=$((freed + size))
        echo "[reclaim-wt] removed=$path size=${size}MB"
        return 0
    fi
    echo "[reclaim-wt] removal failed: $path" >&2
    return 1
}

# Confirm $1 is a registered runner worktree that is safe to touch.
# Sets path_real / common_real. Returns 1 (and counts the reason) when it must be left alone.
_validate_runner_worktree() {
    local path="$1" common_dir top_real listing
    # Limit deletion to the runner's known path shape, even if another repo's
    # worktree is registered. Never follow a symlink outside /tmp.
    [[ "$path" == /tmp/* && "${path##*/}" =~ ^aads-wt-runner-[0-9a-zA-Z_-]+$ ]] || return 1
    [[ ! -L "$path" ]] || return 1
    if [[ -e "$path/.preserve" ]]; then
        preserved_marker=$((preserved_marker + 1))
        echo "[reclaim-wt] preserved (.preserve marker) path=$path"
        return 1
    fi
    path_real="$(cd "$path" && pwd -P)" || { skipped_unknown_repo=$((skipped_unknown_repo + 1)); return 1; }
    [[ "$path_real" == /tmp/* ]] || return 1
    [[ "$path_real" != /root/aads/aads-server && "$path_real" != /root/aads/aads-dashboard && "$path_real" != "$repo_real" && "$path_real" != "$compose_real" && "$path_real" != "$deploy_real" ]] || { skipped_active=$((skipped_active + 1)); return 1; }
    common_dir="$(timeout 5 git -C "$path" rev-parse --git-common-dir 2>/dev/null)" || { skipped_unknown_repo=$((skipped_unknown_repo + 1)); return 1; }
    if [[ "$common_dir" != /* ]]; then common_dir="$path/$common_dir"; fi
    common_real="$(cd "$common_dir" 2>/dev/null && pwd -P)" || { skipped_unknown_repo=$((skipped_unknown_repo + 1)); return 1; }
    top_real="$(timeout 5 git -C "$path" rev-parse --show-toplevel 2>/dev/null)" || { skipped_unknown_repo=$((skipped_unknown_repo + 1)); return 1; }
    listing="$(timeout 5 git --git-dir="$common_real" worktree list --porcelain 2>/dev/null)" || { skipped_unknown_repo=$((skipped_unknown_repo + 1)); return 1; }
    if [[ "$top_real" != "$path_real" ]] || ! printf '%s\n' "$listing" | grep -Fxq "worktree $path_real"; then
        skipped_unknown_repo=$((skipped_unknown_repo + 1)); return 1
    fi
    return 0
}

if [[ -n "$only_job" ]]; then
    path="/tmp/aads-wt-${only_job}"
    result=removed
    case "$only_status" in
        done|error|cancelled|rejected_done|failed) ;;
        *) result="preserved_status" ;;
    esac
    if [[ ! -d "$path" ]]; then
        result=absent
    elif [[ "$result" == removed ]]; then
        if ! _validate_runner_worktree "$path"; then
            result=preserved_unverified
            if (( preserved_marker )); then result=preserved_marker; fi
        elif ! _reclaim_path "$path" "$only_job" "$common_real" 0 1; then
            result=preserved
            if [[ "${DRY_RUN:-0}" == 1 ]] && (( preserved_unclean + skipped_active + skipped_archive_failed == 0 )); then
                result=dry_run_candidate
            fi
        fi
    fi
    echo "[reclaim-wt] immediate job=${only_job} status=${only_status:-none} result=${result} reclaimed=${reclaimed} freed=${freed}MB preserved_unclean=${preserved_unclean} skipped_active=${skipped_active} skipped_archive_failed=${skipped_archive_failed} archived=${archived}"
    exit 0
fi

# 고아 임시 PG 스윕 (AADS-RUNNER-REAP-ORPHAN-TMP-PG-20261015, R-BG).
# 로직은 pipeline-runner.sh 의 temp-pg-reap 블록 한 벌이다 — 같은 디렉터리의 러너에서 잘라 쓴다(배포 단위 동일).
# environ 에 aads-wt-runner-<id> 가 있는데 /tmp/aads-wt-runner-<id> 가 이미 없고 클라이언트가 0 인 /tmp postmaster 만 정지한다.
if [[ "${RUNNER_TEMP_PG_REAP:-1}" == 1 ]]; then
    temp_pg_self="${BASH_SOURCE[0]}"
    [[ "$temp_pg_self" == */* ]] || temp_pg_self="./$temp_pg_self"
    temp_pg_src="${RUNNER_TEMP_PG_SOURCE:-${temp_pg_self%/*}/pipeline-runner.sh}"
    temp_pg_block=""
    [[ -r "$temp_pg_src" ]] && temp_pg_block="$(sed -n '/^# >>> temp-pg-reap/,/^# <<< temp-pg-reap/p' "$temp_pg_src")"
    if [[ -n "$temp_pg_block" ]]; then
        eval "$temp_pg_block"
        _reap_orphan_temp_pg || true
    else
        echo "[reclaim-wt] temp-pg-reap block unavailable: $temp_pg_src" >&2
    fi
fi

declare -a cap_paths=()
declare -A cap_common=() cap_mtime=()

now="$(date +%s)"
wt_glob="${RUNNER_WT_GLOB:-/tmp/aads-wt-runner-*}"
# Expand only the supplied glob, without shell word splitting.
mapfile -t wt_paths < <(compgen -G "$wt_glob" || true)
for path in "${wt_paths[@]}"; do
    [[ -d "$path" ]] || continue
    _validate_runner_worktree "$path" || continue

    cutoff=$((now - retention * 3600))
    # A top-level directory's mtime misses edits to files in subdirectories.
    recent="$(find "$path" -xdev -newermt "@$cutoff" -print -quit 2>/dev/null)" || {
        skipped_active=$((skipped_active + 1))
        echo "[reclaim-wt] cannot inspect timestamps: $path" >&2
        continue
    }
    if [[ -n "$recent" ]]; then
        skipped_recent=$((skipped_recent + 1))
        if (( max_keep > 0 )); then
            # Newest of directory and its .git link; top-level entry mtime alone is cheap and stable.
            m1="$(find "$path" -maxdepth 0 -printf '%T@' 2>/dev/null)" || m1=0
            m2="$(find "$path/.git" -maxdepth 0 -printf '%T@' 2>/dev/null)" || m2=0
            m1="${m1%.*}"; m2="${m2%.*}"
            [[ "$m1" =~ ^[0-9]+$ ]] || m1=0
            [[ "$m2" =~ ^[0-9]+$ ]] || m2=0
            cap_paths+=("$path")
            cap_common["$path"]="$common_real"
            cap_mtime["$path"]=$(( m1 > m2 ? m1 : m2 ))
        fi
        continue
    fi

    # runner creates /tmp/aads-wt-${job_id} (pipeline-runner.sh:2167).
    job_id="${path##*/aads-wt-}"
    status=""
    if (( db_ready )); then status="${job_status[$job_id]:-}"; fi
    case "$status" in
        done|error|cancelled|rejected|rejected_done|failed) ;;
        "")
            if [[ "${RUNNER_WT_ALLOW_NO_DB:-0}" != 1 ]]; then
                skipped_unknown=$((skipped_unknown + 1))
                continue
            fi
            unknown_cutoff=$((now - (unknown_retention > retention ? unknown_retention : retention) * 3600))
            unknown_recent="$(find "$path" -xdev -newermt "@$unknown_cutoff" -print -quit 2>/dev/null)" || {
                skipped_unknown=$((skipped_unknown + 1))
                continue
            }
            if [[ -n "$unknown_recent" ]]; then
                skipped_unknown=$((skipped_unknown + 1))
                continue
            fi
            ;;
        *) skipped_active=$((skipped_active + 1)); continue ;;
    esac

    _reclaim_path "$path" "$job_id" "$common_real" 0 || true
done

# Second pass: among age-skipped worktrees, keep only the newest max_keep
# reclaimable ones. Preserved kinds (busy, rejected, unknown status) never count.
if (( max_keep > 0 )) && (( ${#cap_paths[@]} > max_keep )); then
    eligible=()
    for cp in "${cap_paths[@]}"; do
        cap_status=""
        if (( db_ready )); then cap_status="${job_status[${cp##*/aads-wt-}]:-}"; fi
        case "$cap_status" in
            done|error|cancelled|rejected_done|failed) eligible+=("$cp") ;;
        esac
    done
    if (( ${#eligible[@]} > max_keep )); then
        sorted=()
        for cp in "${eligible[@]}"; do
            pos=${#sorted[@]}
            cp_mtime="${cap_mtime[$cp]}"
            while (( pos > 0 )); do
                prev="${sorted[pos-1]}"
                prev_mtime="${cap_mtime[$prev]}"
                if (( cp_mtime > prev_mtime )) || { (( cp_mtime == prev_mtime )) && [[ "$cp" > "$prev" ]]; }; then
                    pos=$((pos - 1))
                else
                    break
                fi
            done
            sorted=("${sorted[@]:0:pos}" "$cp" "${sorted[@]:pos}")
        done
        kept=0
        for cp in "${sorted[@]}"; do
            if _path_busy "$cp"; then continue; fi
            if (( kept < max_keep )); then kept=$((kept + 1)); continue; fi
            if _reclaim_path "$cp" "${cp##*/aads-wt-}" "${cap_common[$cp]}" 1; then
                reclaimed_over_cap=$((reclaimed_over_cap + 1))
            fi
        done
    fi
fi

if [[ "${DRY_RUN:-0}" != 1 ]]; then
    if [[ -d "$archive_dir" ]]; then
        find "$archive_dir" -maxdepth 1 -type f -name '*.bundle' -mmin +"$((archive_max_age * 1440))" -delete 2>/dev/null || echo "[reclaim-wt] archive pruning failed" >&2
    fi
fi
echo "[reclaim-wt] reclaimed=${reclaimed} freed=${freed}MB archived=${archived} skipped_active=${skipped_active} skipped_recent=${skipped_recent} skipped_unknown=${skipped_unknown} skipped_unknown_repo=${skipped_unknown_repo} skipped_archive_failed=${skipped_archive_failed} reclaimed_over_cap=${reclaimed_over_cap} status_source=${status_source}"
