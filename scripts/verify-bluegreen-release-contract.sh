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
grep -q 'release_nginx_switch_lock' "$deploy_file" \
    || fail "nginx cutover lock must be explicitly released"
grep -q 'active/standby image digest mismatch' "$deploy_file" \
    || fail "same-image digest verification is missing"

dashboard_deploy="/root/aads/aads-dashboard/deploy.sh"
if [[ -f "$dashboard_deploy" ]]; then
    [[ "$(grep -c 'image: aads-dashboard:${AADS_RELEASE_SHA:-local}' "$compose_file")" -eq 2 ]] \
        || fail "expected exactly two dashboard slots using the same image tag"
    grep -q -- '--no-build --no-deps' "$dashboard_deploy" \
        || fail "dashboard slot starts must use --no-build"
    grep -q 'release_nginx_switch_lock' "$dashboard_deploy" \
        || fail "dashboard nginx cutover lock must be explicitly released"
fi

echo "[release-contract] PASS"
