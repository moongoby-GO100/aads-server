#!/bin/bash
# ops queue target ACCT/fb-cutover: fb.newtalk.kr public routing -> cafe24 ACCT app.
# Wraps scripts/cutover_fb_cafe24.sh in the unified worker's "<release_sha> <run_id>" convention.
# Never touches the AADS API/dashboard images and never routes fb back to the retired jinah upstream.
#
# Exit codes: 2 bad SHA | 3 already running | 4 HEAD != release | 5 cutover files dirty/untracked
#             6 apply-edge failed | 7 monitor failed | 8 edge firewall change not approved
#             9 lint/preflight/apply-origin failed | 10 allow-edge-fw failed | 124 time budget | 129/143 HUP/TERM
#
# Failure always ends in the fb maintenance response (EXIT trap), never the retired upstream.
# The cafe24 firewall is opened only with an explicit approval: CONFIRM_FIREWALL_CHANGE=1 supplied by the
# caller, or FW_APPROVAL_FILE containing this release's full SHA. This script never invents that approval.

set -euo pipefail

RELEASE_SHA="${1:-}"
RUN_ID="${2:-0}"
REPO="${AADS_DEPLOY_REPO_DIR:-/root/aads/aads-server}"
CUTOVER="${REPO}/scripts/cutover_fb_cafe24.sh"
EDGE_CONF="${EDGE_CONF:-/etc/nginx/conf.d/fb.conf}"
CAFE24_IP="${CAFE24_IP:-114.207.244.86}"
LOCK="${ACCT_FB_LOCK_FILE:-/tmp/aads-acct-fb-cutover.lock}"
FW_APPROVAL_FILE="${FW_APPROVAL_FILE:-/root/aads/state/acct-fb-edge-fw.approved}"
MONITOR_SECONDS="${MONITOR_SECONDS:-300}"
BUDGET_SECONDS="${DEPLOY_BUDGET_SECONDS:-1080}"   # target timeout_seconds is 1200; leave room for recovery
RECOVERY_SECONDS=120
export MONITOR_SECONDS EDGE_CONF CAFE24_IP

say() { printf '[acct-fb-cutover run=%s] %s\n' "$RUN_ID" "$*"; }

if [[ ! "$RELEASE_SHA" =~ ^[0-9a-fA-F]{7,40}$ ]]; then
    echo "invalid release SHA"
    exit 2
fi
if [[ ! "$MONITOR_SECONDS" =~ ^[0-9]+$ ]]; then
    echo "invalid MONITOR_SECONDS"
    exit 2
fi

exec 9>"$LOCK"
if ! flock -n 9; then
    echo "ACCT fb cutover already running"
    exit 3
fi

git -C "$REPO" cat-file -e "${RELEASE_SHA}^{commit}"
head_sha="$(git -C "$REPO" rev-parse HEAD)"
if [[ "$head_sha" != "$RELEASE_SHA"* ]]; then
    echo "ACCT fb cutover blocked: host HEAD ${head_sha:0:12} != release ${RELEASE_SHA:0:12}"
    exit 4
fi
if ! git -C "$REPO" ls-files --error-unmatch scripts/cutover_fb_cafe24.sh config/apache/fb-cafe24.conf >/dev/null 2>&1; then
    echo "ACCT fb cutover blocked: cutover script or apache template is not tracked at release ${head_sha:0:12}"
    exit 5
fi
if [[ -n "$(git -C "$REPO" status --porcelain --untracked-files=no -- scripts/cutover_fb_cafe24.sh config/apache)" ]]; then
    echo "ACCT fb cutover blocked: cutover script or apache template has uncommitted changes"
    exit 5
fi

edge_points_at_cafe24() {
    local ip_re="${CAFE24_IP//./\\.}"
    grep -Ev '^[[:space:]]*#' "$EDGE_CONF" 2>/dev/null \
        | grep -Eq "proxy_pass[[:space:]]+https?://${ip_re}([:/;[:space:]]|\$)"
}

fw_approved() {
    [[ "${CONFIRM_FIREWALL_CHANGE:-}" == 1 ]] && return 0
    [[ -f "$FW_APPROVAL_FILE" ]] && grep -Fxq "$head_sha" "$FW_APPROVAL_FILE"
}

child=0
fw_pending=0
edge_attempted=0
completed=0

run_timed() { # run_timed <seconds> <cmd...>: background + wait so TERM/HUP reach the child immediately
    local secs="$1" rc=0
    shift
    timeout -k 10 "$secs" "$@" &
    child=$!
    wait "$child" || rc=$?
    child=0
    return "$rc"
}

step() { # step <cmd...>: bounded by what is left of the overall budget
    local left=$((BUDGET_SECONDS - SECONDS))
    if (( left <= 0 )); then
        say "time budget exhausted before: $*"
        return 124
    fi
    run_timed "$left" "$@"
}

recover() {
    say "recovery: fb maintenance response"
    if [[ $edge_attempted == 1 ]] && edge_points_at_cafe24; then
        run_timed "$RECOVERY_SECONDS" bash "$CUTOVER" maintenance || say "maintenance write failed"
    fi
    if [[ $fw_pending == 1 ]]; then
        CONFIRM_FIREWALL_CHANGE=1 run_timed "$RECOVERY_SECONDS" bash "$CUTOVER" revoke-edge-fw || say "revoke-edge-fw failed"
    fi
}

on_exit() {
    local rc=$?
    trap - EXIT
    if [[ $completed != 1 ]]; then
        recover
    fi
    exit "$rc"
}

on_signal() { # on_signal <name> <exit code>
    say "received $1; stopping and recovering"
    if [[ $child != 0 ]]; then
        kill -TERM "$child" 2>/dev/null || true
    fi
    exit "$2"
}

trap on_exit EXIT
trap 'on_signal TERM 143' TERM
trap 'on_signal HUP 129' HUP

for stage in lint preflight apply-origin; do
    if ! step bash "$CUTOVER" "$stage"; then
        say "$stage failed"
        exit 9
    fi
done

reach="$(curl -s -m 8 -o /dev/null -w '%{http_code}' --resolve "fb.newtalk.kr:443:${CAFE24_IP}" https://fb.newtalk.kr/health/live || true)"
if [[ "$reach" != "200" ]]; then
    if ! fw_approved; then
        say "edge cannot reach cafe24 origin (HTTP $reach) and no edge-firewall approval (CONFIRM_FIREWALL_CHANGE=1 or $FW_APPROVAL_FILE with ${head_sha}); origin is applied, edge untouched"
        exit 8
    fi
    say "edge cannot reach cafe24 origin (HTTP $reach); approved -> opening fb-edge firewall rule"
    fw_pending=1
    if ! CONFIRM_FIREWALL_CHANGE=1 step bash "$CUTOVER" allow-edge-fw; then
        say "allow-edge-fw failed"
        exit 10
    fi
fi

edge_attempted=1
if ! step bash "$CUTOVER" apply-edge; then
    exit 6
fi
if ! step bash "$CUTOVER" monitor; then
    exit 7
fi
completed=1
say "fb served by cafe24 origin, ${MONITOR_SECONDS}s monitor clean"
