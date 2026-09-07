#!/usr/bin/env bash
set -euo pipefail

MODE="dry-run"
KEEP_SUCCESS="${AADS_IMAGE_RETENTION_KEEP_SUCCESS:-3}"

for arg in "$@"; do
    case "$arg" in
        --execute) MODE="execute" ;;
        --dry-run) MODE="dry-run" ;;
        --keep-success=*) KEEP_SUCCESS="${arg#*=}" ;;
        *)
            echo "unknown argument: $arg" >&2
            exit 2
            ;;
    esac
done

if [[ ! "$KEEP_SUCCESS" =~ ^[0-9]+$ ]] || [[ "$KEEP_SUCCESS" -lt 1 ]]; then
    KEEP_SUCCESS=3
fi

container_refs="$(docker ps -a --format '{{.Image}}' | sort -u || true)"
container_ids="$(docker ps -a --format '{{.Image}}' \
    | xargs -r docker image inspect --format '{{.Id}}' 2>/dev/null \
    | sort -u || true)"

db_shas=""
if docker inspect aads-postgres --format '{{.State.Running}}' 2>/dev/null | grep -q true; then
    db_shas="$(docker exec aads-postgres psql -U aads -d aads -qAtc "
        WITH keep_success AS (
            SELECT release_sha
            FROM deploy_runs
            WHERE project='AADS'
              AND status='success'
              AND release_sha IS NOT NULL
            ORDER BY phase_completed_at DESC NULLS LAST, updated_at DESC NULLS LAST, id DESC
            LIMIT ${KEEP_SUCCESS}
        ),
        keep_active AS (
            SELECT release_sha
            FROM deploy_runs
            WHERE project='AADS'
              AND status IN ('queued', 'running', 'verifying', 'syncing_standby')
              AND release_sha IS NOT NULL
        )
        SELECT DISTINCT release_sha FROM keep_success
        UNION
        SELECT DISTINCT release_sha FROM keep_active;
    " 2>/dev/null || true)"
fi

printf 'AADS image retention mode=%s keep_success=%s\n' "$MODE" "$KEEP_SUCCESS"
printf 'Preserved release tags:\n'
printf '%s\n' "$db_shas" | sed '/^$/d; s/^/  - /'

mapfile -t images < <(docker image ls aads-server --format '{{.Repository}}:{{.Tag}} {{.ID}} {{.Size}}' | sort -u)
candidates=()
for line in "${images[@]}"; do
    tag="${line%% *}"
    rest="${line#* }"
    image_id="${rest%% *}"
    release_sha="${tag#aads-server:}"
    [[ "$release_sha" == "$tag" || "$release_sha" == "latest" || "$release_sha" == "local" ]] && continue
    if printf '%s\n' "$container_refs" | grep -qx "$tag"; then
        continue
    fi
    if printf '%s\n' "$container_ids" | grep -q "^sha256:${image_id}"; then
        continue
    fi
    if printf '%s\n' "$db_shas" | grep -qx "$release_sha"; then
        continue
    fi
    candidates+=("$tag $image_id ${rest#* }")
done

printf 'Prune candidates:\n'
if [[ "${#candidates[@]}" -eq 0 ]]; then
    printf '  none\n'
    exit 0
fi
printf '%s\n' "${candidates[@]}" | sed 's/^/  - /'

if [[ "$MODE" != "execute" ]]; then
    printf 'Dry-run only. Re-run with --execute to remove candidates.\n'
    exit 0
fi

for line in "${candidates[@]}"; do
    docker image rm "${line%% *}"
done
