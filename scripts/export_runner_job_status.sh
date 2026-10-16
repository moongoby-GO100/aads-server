#!/usr/bin/env bash
# Export only job ids and statuses; install each remote snapshot atomically.
set -euo pipefail
umask 077
snapshot="$(mktemp)"
trap 'rm -f -- "$snapshot"' EXIT
sql="SELECT job_id || '|' || status FROM pipeline_jobs WHERE job_id LIKE 'runner-%'
UNION ALL
SELECT a.job_id || '|' || (a.row_data->>'status') FROM pipeline_jobs_archive a
WHERE a.job_id LIKE 'runner-%'
AND NOT EXISTS (SELECT 1 FROM pipeline_jobs p WHERE p.job_id = a.job_id)"
pg_container="${PG_CONTAINER-aads-postgres}"
psql_args=()
if [[ -n "${PGHOST:-}" ]]; then
    psql_args+=(-h "$PGHOST")
    [[ -z "${PGPORT:-}" ]] || psql_args+=(-p "$PGPORT")
    [[ -z "${PGUSER:-}" ]] || psql_args+=(-U "$PGUSER")
    [[ -z "${PGDATABASE:-}" ]] || psql_args+=(-d "$PGDATABASE")
fi
if [[ -n "$pg_container" ]] && command -v docker >/dev/null 2>&1 &&
    timeout 10 docker exec "$pg_container" psql -U "${PGUSER:-aads}" -d "${PGDATABASE:-aads}" -X -qAtc "$sql" > "$snapshot" 2>/dev/null; then
    :
elif ! timeout 10 psql "${psql_args[@]}" -X -qAtc "$sql" > "$snapshot" 2>/dev/null; then
    echo '[runner-status] query failed' >&2
    exit 1
fi
# Never distribute an empty or malformed snapshot.
if [[ ! -s "$snapshot" ]] || ! awk '!/^runner-[0-9a-zA-Z_-]+\|[a-z][a-z0-9_]*$/ {bad=1} END {exit bad}' "$snapshot"; then
    echo '[runner-status] empty or invalid query result' >&2
    exit 1
fi
read -r -a targets <<< "${RUNNER_STATUS_TARGETS:-root@5.104.86.14}"
(( ${#targets[@]} > 0 )) || exit 2
for target in "${targets[@]}"; do
    [[ "$target" =~ ^[a-zA-Z0-9_][a-zA-Z0-9_.@-]*$ ]] || { echo '[runner-status] invalid target' >&2; exit 2; }
done
for target in "${targets[@]}"; do
    # Unique staging path prevents overlapping exports from mixing snapshots.
    remote_tmp="$(timeout 10 ssh -o BatchMode=yes -o ConnectTimeout=5 "$target" 'umask 077; mkdir -p /var/lib/aads-runner && mktemp /var/lib/aads-runner/.job_status.XXXXXXXXXX' 2>/dev/null)" || { echo '[runner-status] remote preparation failed' >&2; exit 1; }
    [[ "$remote_tmp" =~ ^/var/lib/aads-runner/\.job_status\.[a-zA-Z0-9]+$ ]] || exit 1
    if ! timeout 30 scp -q -o BatchMode=yes -o ConnectTimeout=5 "$snapshot" "$target:$remote_tmp" 2>/dev/null; then
        timeout 10 ssh -o BatchMode=yes -o ConnectTimeout=5 "$target" "rm -f -- '$remote_tmp'" >/dev/null 2>&1 || true
        echo '[runner-status] transfer failed' >&2
        exit 1
    fi
    if ! timeout 10 ssh -o BatchMode=yes -o ConnectTimeout=5 "$target" "mv -f -- '$remote_tmp' /var/lib/aads-runner/job_status.txt" 2>/dev/null; then
        echo '[runner-status] atomic replacement failed' >&2
        exit 1
    fi
done
