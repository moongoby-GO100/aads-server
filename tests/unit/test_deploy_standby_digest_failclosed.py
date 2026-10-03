"""Behavioral fail-closed contract for standby same-digest certification.

The real function bodies are sliced out of deploy.sh and executed in a bash
subprocess with docker/flock/curl/nginx replaced by stubs. Nothing here builds,
restarts, or routes anything.
"""

import re
import subprocess
from pathlib import Path

import pytest

DEPLOY = (Path(__file__).parents[2] / "deploy.sh").read_text(encoding="utf-8")

FUNCTIONS = (
    "container_for_port",
    "standby_ownership_valid",
    "standby_same_digest_verified",
    "sync_standby_slot_after_drain",
    "handle_standby_sync_result",
    "resolve_final_deploy_status",
)


def _function(name: str) -> str:
    match = re.search(rf"^{name}\(\) \{{\n.*?\n^\}}\n", DEPLOY, re.S | re.M)
    assert match, f"{name} not found in deploy.sh"
    return match.group(0)


def _cutover_verification_block() -> str:
    start = DEPLOY.index("        # ④ 전환 후 검증")
    end = DEPLOY.index("        # The routing transaction is complete.")
    return DEPLOY[start:end]


STUBS = r"""
set -u
LOG="$WORK/events.log"; : > "$LOG"
STATE_DIR="$WORK"; mkdir -p "$WORK/logs"
DEPLOY_GENERATION_FILE="$WORK/gen"; ACTIVE_PORT_FILE="$WORK/port"; ACTIVE_CONTAINER_FILE="$WORK/container"
COMPOSE_DIR="$WORK"; COMPOSE_ENV_ARGS=(); AADS_RELEASE_SHA=deadbeef
DOWNTIME_PROBE_HOST=probe.example; UPSTREAM_CONF="$WORK/upstream.conf"
MODE=bluegreen; DEPLOY_RUN_ID=1; DEPLOY_GENERATION=gen-1
NEW_CONTAINER=aads-server-green; NEW_PORT=8102; OLD_CONTAINER=aads-server; OLD_PORT=8100
printf gen-1 > "$DEPLOY_GENERATION_FILE"; printf 8102 > "$ACTIVE_PORT_FILE"; printf aads-server-green > "$ACTIVE_CONTAINER_FILE"

ev() { echo "$*" >> "$LOG"; }
docker() {
  case "$1" in
    inspect) local v="IMG_${2//-/_}"; printf '%s' "${!v:-}"; return 0 ;;
    compose) ev "docker-compose $*"; return 0 ;;
    *) ev "docker $*"; return 0 ;;
  esac
}
flock() { [[ "${LOCK_BUSY:-0}" != "1" ]]; }
curl() {
  ev "curl $*"
  case " $* " in *" -H Host: "*|*"127.0.0.1/api/v1/health"*) [[ "${ROUTED_HEALTH_OK:-1}" == "1" ]]; return ;; esac
  return 0
}
sleep() { :; }
audit_control() { ev "audit $1 $3 $4"; }
notify() { ev "notify $*"; }
deploy_phase_end() { ev "phase_end $1 $2 $3"; }
deploy_observe_update() { :; }
record_deploy() { ev "record_deploy $1"; }
schedule_standby_sync_retry() { ev "retry_scheduled"; }
set_deploy_stream_phase_metadata() { :; }
reconcile_inactive_target_recovery_executions() { :; }
stream_count_for_port() { echo "${STREAMS:-0}"; }
verify_container_memory_limit() { return 0; }
wait_port_health() { [[ "${PORT_HEALTH_OK:-1}" == "1" ]]; }
verify_container_slot_markers() { return 0; }
write_active_slot_state() { ev "write_active_slot_state $1 $2"; printf '%s' "$1" > "$ACTIVE_PORT_FILE"; printf '%s' "$2" > "$ACTIVE_CONTAINER_FILE"; }
nginx_reload() { ev "nginx_reload"; }
"""


def _run(tmp_path, body: str, env: dict | None = None):
    funcs = "\n".join(_function(name) for name in FUNCTIONS)
    script = f"{STUBS}\n{funcs}\n{body}\n"
    full_env = {"WORK": str(tmp_path), "PATH": "/usr/bin:/bin"}
    full_env.update(env or {})
    result = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, env=full_env, timeout=30
    )
    return result, (tmp_path / "events.log").read_text()


SAME = {"IMG_aads_server": "sha256:aaa", "IMG_aads_server_green": "sha256:aaa"}
DIFF = {"IMG_aads_server": "sha256:old", "IMG_aads_server_green": "sha256:aaa"}


def _sync(tmp_path, env, setup=""):
    body = f"""
{setup}
sync_standby_slot_after_drain aads-server 8100 gen-1; rc=$?
echo "RC=$rc reason=${{STANDBY_SYNC_SKIP_REASON:-}}"
"""
    result, events = _run(tmp_path, body, env)
    rc = int(re.search(r"RC=(\d+)", result.stdout).group(1))
    return rc, result.stdout, events


def test_lock_busy_is_skipped_not_success(tmp_path):
    rc, out, events = _sync(tmp_path, {**SAME, "LOCK_BUSY": "1"})
    assert rc == 3
    assert "reason=standby lock busy" in out
    assert "docker-compose" not in events


def test_stale_generation_is_skipped_not_success(tmp_path):
    rc, out, events = _sync(tmp_path, SAME, setup='printf gen-2 > "$DEPLOY_GENERATION_FILE"')
    assert rc == 3
    assert "reason=stale generation or slot became active" in out
    assert "docker-compose" not in events


def test_ownership_change_after_drain_is_skipped_not_success(tmp_path):
    # Ownership valid at entry, then the slot becomes active during the drain poll.
    setup = (
        'stream_count_for_port() { printf 8100 > "$ACTIVE_PORT_FILE"; echo 0; }\n'
        "AADS_DEPLOY_STANDBY_SYNC_MAX_WAIT=5\n"
    )
    rc, out, events = _sync(tmp_path, SAME, setup=setup)
    assert rc == 3
    assert "reason=ownership changed after drain" in out
    assert "docker-compose" not in events


def test_drain_pending_returns_2_and_does_not_recreate(tmp_path):
    rc, _, events = _sync(tmp_path, {**SAME, "STREAMS": "3"})
    assert rc == 2
    assert "docker-compose" not in events
    assert "audit standby-sync blocked" in events


def test_digest_mismatch_after_sync_is_failure(tmp_path):
    rc, out, events = _sync(tmp_path, DIFF)
    assert rc == 1
    assert "image digest mismatch" in out
    assert "docker-compose" in events
    assert "--no-build" in events


def test_health_failure_after_recreate_is_failure(tmp_path):
    rc, _, _ = _sync(tmp_path, {**SAME, "PORT_HEALTH_OK": "0"})
    assert rc == 1


def test_same_digest_sync_succeeds(tmp_path):
    rc, out, events = _sync(tmp_path, SAME)
    assert rc == 0
    assert "audit standby-sync success" in events
    assert "--no-build --no-deps --force-recreate" in events


def _handle(tmp_path, rc: int, env: dict):
    body = f"""
STANDBY_SYNC_SKIP_REASON="standby lock busy"
handle_standby_sync_result {rc}; hrc=$?
resolve_final_deploy_status
echo "HRC=$hrc DEFERRED=$STANDBY_SYNC_DEFERRED FINAL=$FINAL_DEPLOY_STATUS ERR=$FINAL_DEPLOY_ERROR"
"""
    result, events = _run(tmp_path, body, env)
    return result.stdout, events


@pytest.mark.parametrize("rc", [2, 3])
def test_pending_and_skipped_end_as_uncertified_success_partial(tmp_path, rc):
    out, events = _handle(tmp_path, rc, SAME)
    assert "HRC=0 DEFERRED=true FINAL=success_partial" in out
    assert "phase_end standby_same_digest_sync skipped" in events
    assert "phase_end standby_same_digest_sync success" not in events
    assert "retry_scheduled" in events
    assert "record_deploy" not in events


def test_skipped_reason_is_visible_in_final_error(tmp_path):
    out, _ = _handle(tmp_path, 3, SAME)
    assert "ERR=standby sync deferred: skipped: standby lock busy" in out


def test_rc0_with_digest_mismatch_cannot_certify(tmp_path):
    out, events = _handle(tmp_path, 0, DIFF)
    assert "HRC=1" in out
    assert "phase_end standby_same_digest_sync failed" in events
    assert "record_deploy failed" in events
    assert "phase_end standby_same_digest_sync success" not in events


def test_rc0_with_unreadable_digest_cannot_certify(tmp_path):
    out, _ = _handle(tmp_path, 0, {"IMG_aads_server_green": "sha256:aaa"})
    assert "HRC=1" in out


def test_hard_failure_rc_fails_the_release(tmp_path):
    out, events = _handle(tmp_path, 1, SAME)
    assert "HRC=1" in out
    assert "record_deploy failed" in events


def test_same_digest_success_is_the_only_certified_path(tmp_path):
    out, events = _handle(tmp_path, 0, SAME)
    assert "HRC=0 DEFERRED=false FINAL=success ERR=" in out
    assert "phase_end standby_same_digest_sync success" in events


def test_final_gate_downgrades_when_digest_drifts_after_monitoring(tmp_path):
    body = """
STANDBY_SYNC_DEFERRED=false
resolve_final_deploy_status
echo "FINAL=$FINAL_DEPLOY_STATUS ERR=$FINAL_DEPLOY_ERROR"
"""
    result, _ = _run(tmp_path, body, DIFF)
    assert "FINAL=success_partial" in result.stdout
    assert "digest mismatch at final certification" in result.stdout
    result, _ = _run(tmp_path, body, SAME)
    assert "FINAL=success ERR=" in result.stdout


def test_final_gate_does_not_apply_to_non_bluegreen_modes(tmp_path):
    body = """
MODE=code; STANDBY_SYNC_DEFERRED=false
resolve_final_deploy_status
echo "FINAL=$FINAL_DEPLOY_STATUS"
"""
    result, _ = _run(tmp_path, body, {})
    assert "FINAL=success" in result.stdout


def _cutover(tmp_path, routed_ok: bool):
    block = _cutover_verification_block()
    (tmp_path / "upstream.conf").write_text("switched\n")
    (tmp_path / "upstream.conf.pre_deploy").write_text("original\n")
    body = f"""
cp() {{ command cp "$@"; }}
DEPLOY_UPSTREAM_SWITCHED=false
CURRENT_PORT=8100
(
{block}
echo "REACHED_AFTER_BLOCK"
)
echo "SUBRC=$?"
"""
    return _run(
        tmp_path,
        body,
        {"ROUTED_HEALTH_OK": "1" if routed_ok else "0", "PATH": "/usr/bin:/bin"},
    )


def test_routed_health_failure_rolls_back_and_fails(tmp_path):
    result, events = _cutover(tmp_path, routed_ok=False)
    assert "SUBRC=1" in result.stdout
    assert "REACHED_AFTER_BLOCK" not in result.stdout
    assert (tmp_path / "upstream.conf").read_text() == "original\n"
    assert "nginx_reload" in events
    assert "write_active_slot_state 8100 aads-server" in events
    assert "phase_end nginx_cutover failed" in events
    assert "record_deploy failed" in events


def test_routed_health_success_keeps_new_route(tmp_path):
    result, events = _cutover(tmp_path, routed_ok=True)
    assert "REACHED_AFTER_BLOCK" in result.stdout
    assert (tmp_path / "upstream.conf").read_text() == "switched\n"
    assert "phase_end nginx_cutover success" in events
    assert "record_deploy" not in events
