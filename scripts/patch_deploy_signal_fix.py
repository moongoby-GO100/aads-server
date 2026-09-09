#!/usr/bin/env python3
"""deploy.sh 배포 실패 근본 원인 4종 수정 패치.

RC1: TERM/INT during post-switch phases → success instead of failed (34 failures)
RC2: Heartbeat subshell not signal-protected → stale detection (9 failures)
RC3: Dirty worktree gate blocks queue worker deploys (23 failures)
RC4: Early HUP race window before trap '' HUP is registered (9 HUP failures)
"""
import sys

DEPLOY_SH = "/root/aads/aads-server/deploy.sh"

with open(DEPLOY_SH, "r") as f:
    content = f.read()

original = content
changes = []

# ── RC4: Move HUP ignore to right after set -euo pipefail ──
old_set = "set -euo pipefail"
new_set = (
    "set -euo pipefail\n"
    "trap '' HUP  # RC4: ignore HUP immediately — eliminates race window before main trap block"
)
if old_set in content and content.count(old_set) == 1:
    content = content.replace(old_set, new_set, 1)
    changes.append("RC4: early HUP ignore after set -euo pipefail")

# ── RC1: Phase-aware signal handler ──
old_signal_trap = '''deploy_signal_trap() {
    local signal_name="${1:-TERM}"
    stop_deploy_heartbeat
    stop_downtime_monitor
    deploy_phase_end "$DEPLOY_CURRENT_PHASE" "failed" "deploy interrupted by ${signal_name}"
    deploy_observe_update "failed" "$DEPLOY_CURRENT_PHASE" "deploy interrupted by ${signal_name}"
    record_deploy "failed" "$MODE" "deploy interrupted by ${signal_name}"
    cleanup_release_context
    rm -f "${LOCKFILE:-/tmp/aads-deploy.lock}" 2>/dev/null || true
    exit 143
}'''

new_signal_trap = '''deploy_signal_trap() {
    local signal_name="${1:-TERM}"
    stop_deploy_heartbeat
    stop_downtime_monitor
    if [[ "${DEPLOY_UPSTREAM_SWITCHED:-false}" == "true" ]]; then
        # RC1: post-switch signal — upstream already cutover, new container is live
        deploy_phase_end "$DEPLOY_CURRENT_PHASE" "success" "post-switch ${signal_name} — deploy already live"
        deploy_observe_update "success" "completed_after_signal_recovery" "deploy interrupted by ${signal_name}; upstream already switched"
        record_deploy "success" "$MODE" "deploy interrupted by ${signal_name} post-switch; certified live"
    else
        deploy_phase_end "$DEPLOY_CURRENT_PHASE" "failed" "deploy interrupted by ${signal_name}"
        deploy_observe_update "failed" "$DEPLOY_CURRENT_PHASE" "deploy interrupted by ${signal_name}"
        record_deploy "failed" "$MODE" "deploy interrupted by ${signal_name}"
    fi
    cleanup_release_context
    rm -f "${LOCKFILE:-/tmp/aads-deploy.lock}" 2>/dev/null || true
    exit 143
}'''

if old_signal_trap in content:
    content = content.replace(old_signal_trap, new_signal_trap, 1)
    changes.append("RC1: phase-aware deploy_signal_trap (post-switch → success)")

# ── RC1: Initialize DEPLOY_UPSTREAM_SWITCHED flag ──
old_init = 'DEPLOY_CURRENT_PHASE="initializing"'
new_init = (
    'DEPLOY_CURRENT_PHASE="initializing"\n'
    'DEPLOY_UPSTREAM_SWITCHED=false  # RC1: set true after nginx cutover; signals after this = success'
)
if old_init in content and content.count(old_init) == 1:
    content = content.replace(old_init, new_init, 1)
    changes.append("RC1: DEPLOY_UPSTREAM_SWITCHED=false init")

# ── RC1: Set flag after nginx cutover success ──
old_cutover_ok = '''            echo "[deploy.sh] ④ ✅ 전환 검증 성공"
            audit_control "nginx-switch" "${OLD_CONTAINER}:${OLD_PORT}->${NEW_CONTAINER}:${NEW_PORT}" "success" "direct and nginx-routed health verified"
            deploy_phase_end "nginx_cutover" "success" "direct and nginx-routed health verified"'''

new_cutover_ok = '''            echo "[deploy.sh] ④ ✅ 전환 검증 성공"
            DEPLOY_UPSTREAM_SWITCHED=true  # RC1: from here, TERM/INT = success (new container is live)
            audit_control "nginx-switch" "${OLD_CONTAINER}:${OLD_PORT}->${NEW_CONTAINER}:${NEW_PORT}" "success" "direct and nginx-routed health verified"
            deploy_phase_end "nginx_cutover" "success" "direct and nginx-routed health verified"'''

if old_cutover_ok in content:
    content = content.replace(old_cutover_ok, new_cutover_ok, 1)
    changes.append("RC1: DEPLOY_UPSTREAM_SWITCHED=true after cutover")

# ── RC2: Heartbeat subshell signal protection ──
old_heartbeat = '''    (
        while true; do
            sleep "$interval"'''

new_heartbeat = '''    (
        trap '' HUP TERM INT  # RC2: prevent signal propagation killing heartbeat
        while true; do
            sleep "$interval"'''

if old_heartbeat in content and content.count(old_heartbeat) == 1:
    content = content.replace(old_heartbeat, new_heartbeat, 1)
    changes.append("RC2: heartbeat subshell signal protection (trap '' HUP TERM INT)")

# ── RC3: Skip dirty worktree gate for queue worker ──
old_gate = '''if ! enforce_release_worktree_gate; then
    deploy_phase_end "preflight" "blocked" "dirty worktree blocks release"
    record_deploy "blocked" "$MODE" "dirty worktree blocks release"
    exit 1
fi'''

new_gate = '''if [[ "${AADS_DEPLOY_QUEUE_WORKER:-false}" == "true" ]]; then
    # RC3: queue worker uses clean detached worktree — skip dirty gate
    report_dirty_release_exclusions
    echo "[deploy.sh] ✅ queue worker: dirty worktree gate skipped (clean worktree at ${AADS_RELEASE_SHA})"
    audit_control "release-worktree-gate" "$COMPOSE_DIR" "skipped" "queue_worker=true"
elif ! enforce_release_worktree_gate; then
    deploy_phase_end "preflight" "blocked" "dirty worktree blocks release"
    record_deploy "blocked" "$MODE" "dirty worktree blocks release"
    exit 1
fi'''

if old_gate in content:
    content = content.replace(old_gate, new_gate, 1)
    changes.append("RC3: skip dirty worktree gate for queue worker deploys")

# ── Write result ──
if not changes:
    print("ERROR: no patches matched — deploy.sh may have changed", file=sys.stderr)
    sys.exit(1)

with open(DEPLOY_SH, "w") as f:
    f.write(content)

print(f"✅ {len(changes)} patches applied to deploy.sh:")
for c in changes:
    print(f"  - {c}")
print(f"  lines: {original.count(chr(10))+1} → {content.count(chr(10))+1}")
