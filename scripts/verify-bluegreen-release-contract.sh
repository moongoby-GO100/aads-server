#!/usr/bin/env bash
set -euo pipefail

root_dir="${1:-/root/aads/aads-server}"
compose_file="${root_dir}/docker-compose.prod.yml"
deploy_file="${root_dir}/deploy.sh"

fail() {
    echo "[release-contract] FAIL: $1" >&2
    exit 1
}

[[ -f "$compose_file" ]] || fail "missing ${compose_file}"
[[ -f "$deploy_file" ]] || fail "missing ${deploy_file}"

grep -q 'image: aads-server:${AADS_RELEASE_SHA:-local}' "$compose_file" \
    || fail "API services must share the release-SHA image tag"
[[ "$(grep -c 'image: aads-server:${AADS_RELEASE_SHA:-local}' "$compose_file")" -eq 2 ]] \
    || fail "expected exactly two API slots using the same image tag"
grep -q -- '--no-build --no-deps' "$deploy_file" \
    || fail "slot starts must use --no-build"
grep -q -- '--force-recreate --no-build --no-deps' "$deploy_file" \
    || fail "slot starts must force recreate from the release-SHA image"
grep -q 'release_nginx_switch_lock' "$deploy_file" \
    || fail "nginx cutover lock must be explicitly released"
grep -q 'active/standby image digest mismatch' "$deploy_file" \
    || fail "same-image digest verification is missing"
grep -q 'git -C "$COMPOSE_DIR" archive --format=tar HEAD' "$deploy_file" \
    || fail "API image must be built from an isolated committed release context"
grep -q 'enforce_release_worktree_gate' "$deploy_file" \
    || fail "dirty worktree release gate is missing"
grep -q 'AADS_DEPLOY_ALLOW_DIRTY_ARCHIVE' "$deploy_file" \
    || fail "dirty worktree override must be explicit and auditable"
grep -q 'AADS_DEPLOY_STANDBY_SYNC_MAX_WAIT:-300' "$deploy_file" \
    || fail "standby sync must have a bounded default timeout"
grep -q 'reconcile_stale_deploy_runs' "$deploy_file" \
    || fail "stale deployment run reconciliation is missing"
grep -q 'AADS_DEPLOY_TARGET_DRAIN_MAX_WAIT:-180' "$deploy_file" \
    || fail "target slot drain must have a bounded default timeout"
api_sections="$(
    awk '
      /^  aads-server:$/ {in_api=1}
      /^  aads-server-green:$/ {in_api=1}
      /^  [a-zA-Z0-9_-]+:$/ && $1 !~ /^aads-server:?$/ && $1 !~ /^aads-server-green:?$/ {in_api=0}
      in_api {print}
    ' "$compose_file"
)"
! grep -q '/root/aads/aads-server/app:/app/app:rw' <<<"$api_sections" \
    || fail "API app source bind mount bypasses release-SHA image"
! grep -q '/root/aads/aads-server/scripts:/app/scripts:rw' <<<"$api_sections" \
    || fail "API scripts source bind mount bypasses release-SHA image"

dashboard_deploy="/root/aads/aads-dashboard/deploy.sh"
if [[ -f "$dashboard_deploy" ]]; then
    [[ "$(grep -c 'image: aads-dashboard:${AADS_RELEASE_SHA:-local}' "$compose_file")" -eq 2 ]] \
        || fail "expected exactly two dashboard slots using the same image tag"
    grep -q -- '--no-build --no-deps' "$dashboard_deploy" \
        || fail "dashboard slot starts must use --no-build"
    grep -q 'release_nginx_switch_lock' "$dashboard_deploy" \
        || fail "dashboard nginx cutover lock must be explicitly released"
    grep -q 'git -C "$STATE_DIR" archive --format=tar HEAD' "$dashboard_deploy" \
        || fail "dashboard image must be built from an isolated committed release context"
fi

echo "[release-contract] PASS"
