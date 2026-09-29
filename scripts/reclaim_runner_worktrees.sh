#!/usr/bin/env bash
# Reclaim expired, registered runner worktrees after preserving local work.
set -euo pipefail

repo="${RUNNER_WT_REPO:-/root/aads/aads-server}"
retention="${RUNNER_WT_RETENTION_HOURS:-24}"
reclaimed=0 freed=0 archived=0 skipped_active=0 skipped_recent=0 skipped_unknown=0 skipped_unknown_repo=0 skipped_archive_failed=0
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
# Completed jobs move to pipeline_jobs_archive after a few hours; archived rows
# keep their original status in row_data. Prefer a live row if both exist.
sql="SELECT job_id || '|' || status FROM pipeline_jobs WHERE job_id LIKE 'runner-%'
UNION ALL
SELECT a.job_id || '|' || (a.row_data->>'status') FROM pipeline_jobs_archive a
WHERE a.job_id LIKE 'runner-%'
AND NOT EXISTS (SELECT 1 FROM pipeline_jobs p WHERE p.job_id = a.job_id)"
if command -v docker >/dev/null 2>&1 && db_rows="$(timeout 10 docker exec aads-postgres psql -U aads -d aads -X -qAtc "$sql" 2>/dev/null)"; then
    db_ready=1
elif command -v psql >/dev/null 2>&1 && db_rows="$(timeout 10 psql -X -qAtc "$sql" 2>/dev/null)"; then
    db_ready=1
fi
if (( db_ready )); then
    while IFS='|' read -r id status; do
        [[ "$id" =~ ^runner-[0-9a-zA-Z_-]+$ && -n "$status" ]] && job_status["$id"]="$status"
    done <<< "$db_rows"
else
    echo "[reclaim-wt] DB unavailable; preserving unknown jobs by default" >&2
fi

now="$(date +%s)"
for path in /tmp/aads-wt-runner-*; do
    [[ -d "$path" ]] || continue
    # Limit deletion to the runner's known path shape, even if another repo's
    # worktree is registered. Never follow a symlink outside /tmp.
    [[ "$path" =~ ^/tmp/aads-wt-runner-[0-9a-zA-Z_-]+$ ]] || continue
    [[ ! -L "$path" ]] || continue
    path_real="$(cd "$path" && pwd -P)" || { skipped_unknown_repo=$((skipped_unknown_repo + 1)); continue; }
    [[ "$path_real" != /root/aads/aads-server && "$path_real" != /root/aads/aads-dashboard && "$path_real" != "$repo_real" && "$path_real" != "$compose_real" && "$path_real" != "$deploy_real" ]] || { skipped_active=$((skipped_active + 1)); continue; }
    common_dir="$(timeout 5 git -C "$path" rev-parse --git-common-dir 2>/dev/null)" || { skipped_unknown_repo=$((skipped_unknown_repo + 1)); continue; }
    if [[ "$common_dir" != /* ]]; then common_dir="$path/$common_dir"; fi
    common_real="$(cd "$common_dir" 2>/dev/null && pwd -P)" || { skipped_unknown_repo=$((skipped_unknown_repo + 1)); continue; }
    top_real="$(timeout 5 git -C "$path" rev-parse --show-toplevel 2>/dev/null)" || { skipped_unknown_repo=$((skipped_unknown_repo + 1)); continue; }
    listing="$(timeout 5 git --git-dir="$common_real" worktree list --porcelain 2>/dev/null)" || { skipped_unknown_repo=$((skipped_unknown_repo + 1)); continue; }
    if [[ "$top_real" != "$path_real" ]] || ! printf '%s\n' "$listing" | grep -Fxq "worktree $path_real"; then
        skipped_unknown_repo=$((skipped_unknown_repo + 1)); continue
    fi

    cutoff=$((now - retention * 3600))
    # A top-level directory's mtime misses edits to files in subdirectories.
    recent="$(find "$path" -xdev -newermt "@$cutoff" -print -quit 2>/dev/null)" || {
        skipped_active=$((skipped_active + 1))
        echo "[reclaim-wt] cannot inspect timestamps: $path" >&2
        continue
    }
    if [[ -n "$recent" ]]; then
        skipped_recent=$((skipped_recent + 1))
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

    # Inspect tracked, untracked, and ignored changes before archiving.
    dirty="$(timeout 5 git -C "$path" status --porcelain --ignored --untracked-files=all 2>/dev/null)" || {
        skipped_archive_failed=$((skipped_archive_failed + 1))
        echo "[reclaim-wt] cannot inspect git status: $path" >&2
        continue
    }
    head_sha="$(timeout 5 git -C "$path" rev-parse HEAD 2>/dev/null)" || {
        skipped_archive_failed=$((skipped_archive_failed + 1))
        echo "[reclaim-wt] cannot inspect HEAD: $path" >&2
        continue
    }
    needs_bundle=0
    if ! timeout 5 git -C "$path" merge-base --is-ancestor "$head_sha" origin/main 2>/dev/null; then
        needs_bundle=1
        merge_base="$(timeout 5 git -C "$path" merge-base "$head_sha" origin/main 2>/dev/null)" || {
            skipped_archive_failed=$((skipped_archive_failed + 1))
            echo "[reclaim-wt] cannot find merge base: $path" >&2
            continue
        }
    fi

    if [[ "$process_probe" == none ]]; then
        skipped_active=$((skipped_active + 1))
        continue
    else
        probe_rc=0
        if [[ "$process_probe" == lsof ]]; then
            timeout 5 lsof +D "$path" >/dev/null 2>&1 || probe_rc=$?
        else
            # fuser on the directory alone misses open files below it.
            # Check every path and treat an incomplete traversal as busy.
            probe_list="$(mktemp)" || { skipped_active=$((skipped_active + 1)); continue; }
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
        if [[ "$probe_rc" != 1 ]]; then
            skipped_active=$((skipped_active + 1)); continue
        fi
    fi

    size="$(du -sm "$path" 2>/dev/null | awk '{print $1}' || true)"
    size="${size:-0}"
    if [[ "${DRY_RUN:-0}" == 1 ]]; then
        echo "[reclaim-wt] DRY_RUN candidate=$path size=${size}MB bundle=$needs_bundle dirty=$([[ -n "$dirty" ]] && echo 1 || echo 0)"
        continue
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
        rmdir -- "$staging" 2>/dev/null || true
        if (( ! archive_ok )); then
            skipped_archive_failed=$((skipped_archive_failed + 1))
            echo "[reclaim-wt] archive failed: $path" >&2
            continue
        fi
        archived=$((archived + 1))
    fi
    # A locked or otherwise protected worktree must remain protected if Git
    # refuses removal; a filesystem fallback would bypass Git's decision.
    if git --git-dir="$common_real" worktree remove --force "$path" >/dev/null 2>&1; then
        reclaimed=$((reclaimed + 1))
        freed=$((freed + size))
        echo "[reclaim-wt] removed=$path size=${size}MB"
    else
        echo "[reclaim-wt] removal failed: $path" >&2
    fi
done

if [[ "${DRY_RUN:-0}" != 1 ]]; then
    if [[ -d "$archive_dir" ]]; then
        find "$archive_dir" -maxdepth 1 -type f -name '*.bundle' -mmin +"$((archive_max_age * 1440))" -delete 2>/dev/null || echo "[reclaim-wt] archive pruning failed" >&2
    fi
fi
echo "[reclaim-wt] reclaimed=${reclaimed} freed=${freed}MB archived=${archived} skipped_active=${skipped_active} skipped_recent=${skipped_recent} skipped_unknown=${skipped_unknown} skipped_unknown_repo=${skipped_unknown_repo} skipped_archive_failed=${skipped_archive_failed}"
