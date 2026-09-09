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
grep -q -- '--no-build --no-deps --force-recreate' "$deploy_file" \
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
grep -q -- '--env-file" "$AADS_RUNTIME_ENV_FILE"' "$deploy_file" \
    || fail "docker compose must use the runtime env file from STATE_DIR"
grep -Eq 'AADS_DEPLOY_STANDBY_SYNC_MAX_WAIT:-[0-9]+' "$deploy_file" \
    || fail "standby sync must have a bounded default timeout"
grep -q 'reconcile_stale_deploy_runs' "$deploy_file" \
    || fail "stale deployment run reconciliation is missing"
grep -q 'AADS_DEPLOY_TARGET_DRAIN_MAX_WAIT:-1800' "$deploy_file" \
    || fail "target slot drain must have a bounded default timeout"
grep -q 'DEPLOY_FLOCKFILE="/tmp/aads-deploy.flock"' "$deploy_file" \
    || fail "deploy entry flock is missing"

# ── 릴리스 계보(goal 릴리스 증거) 계약 ──────────────────────────────────────
# 런타임 컨테이너에는 /app/.git 이 없다. 계보를 기록할 수 있는 유일한 지점은
# 호스트의 이 배포 경로이므로, 훅이 사라지면 목표 자동전진이 조용히 멈춘다.
provenance_hook="${root_dir}/scripts/record-release-provenance.sh"
[[ -x "$provenance_hook" ]] \
    || fail "missing executable ${provenance_hook}"
grep -q 'record-release-provenance.sh' "$deploy_file" \
    || fail "release provenance hook is not wired into deploy.sh"
# 훅 호출은 P0/P1 모니터링 게이트 통과 이후여야 한다 — 인증 전 기록 금지.
hook_line="$(grep -n 'scripts/record-release-provenance.sh' "$deploy_file" | tail -1 | cut -d: -f1)"
monitor_line="$(grep -n 'deploy_phase_end "p0p1_monitoring" "success"' "$deploy_file" | tail -1 | cut -d: -f1)"
[[ -n "$hook_line" && -n "$monitor_line" && "$hook_line" -gt "$monitor_line" ]] \
    || fail "release provenance must be recorded after the P0/P1 monitoring gate"
grep -q "d.image_digest = d.standby_digest" "$provenance_hook" \
    || fail "provenance INSERT must carry the certified-deploy digest gate"
grep -q "ON CONFLICT (deploy_run_id, task_sha) DO NOTHING" "$provenance_hook" \
    || fail "provenance INSERT must be idempotent"
grep -q '\^\[0-9a-f\]{40}\$' "$provenance_hook" \
    || fail "provenance must reject anything that is not a full 40-char SHA"
grep -qx '\.git' "${root_dir}/.dockerignore" \
    || fail ".dockerignore must keep excluding .git — runtime git provenance is not a supported path"
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
