"""deploy.sh target_slot_drain 정책 (AADS-DEPLOY-DRAIN-TIMEOUT-PROCEED-20261006).

CEO 지시(2026-10-06): drain 대기(180초) 후에도 스트림이 남으면 차단하지 말고 진행한다.
block 은 AADS_DEPLOY_DRAIN_TIMEOUT_ACTION=block 으로만, ALLOW_BUSY_TARGET=true 는 즉시 진행.
deploy.sh 의 실제 함수를 잘라 스텁 위에서 실행한다.
"""

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEPLOY_SH = (ROOT / "deploy.sh").read_text()


def _fn(name: str) -> str:
    body = DEPLOY_SH.split("\n" + name + "() {", 1)[1].split("\n}\n", 1)[0]
    return name + "() {" + body + "\n}\n"


def _run(tmp_path, env_lines: str, streams: str = "1"):
    log = tmp_path / "log"
    script = f"""set -Eeuo pipefail
LOG="{log}"; : > "$LOG"
MODE=bluegreen
NEW_CONTAINER=aads-server-green; NEW_PORT=8102
TARGET_DRAIN_STARTED_EPOCH=$(date +%s)
{env_lines}
stream_count_for_port() {{ echo "{streams}"; }}
sleep() {{ :; }}
notify() {{ echo "NOTIFY $*" >> "$LOG"; }}
deploy_observe_update() {{ :; }}
deploy_phase_end() {{ echo "PHASE_END $*" >> "$LOG"; }}
record_deploy() {{ echo "RECORD $*" >> "$LOG"; }}
audit_control() {{ echo "AUDIT $*" >> "$LOG"; }}
container_for_port() {{ echo ""; }}
{_fn("target_active_session_ids")}
{_fn("target_slot_drain_gate")}
rc=0
target_slot_drain_gate || rc=$?
echo "RC=$rc"
echo "NOTE=${{TARGET_DRAIN_PROCEED_NOTE:-}}"
echo "ELAPSED=${{local_target_elapsed:-}}"
"""
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
    return proc.stdout, log.read_text()


def test_default_proceeds_after_drain_timeout(tmp_path):
    out, log = _run(tmp_path, "AADS_DEPLOY_TARGET_DRAIN_MAX_WAIT=30")
    assert "RC=0" in out
    assert "drain timeout — active=1 잔존, 정책 proceed 로 전환 진행" in out
    assert "target_drain proceeded_busy: active=1" in out
    assert "AUDIT target-drain aads-server-green:8102 proceeded_busy" in log
    assert "RECORD blocked" not in log
    assert "PHASE_END target_slot_drain blocked" not in log


def test_proceed_note_has_no_autoheal_trigger_text(tmp_path):
    out, _ = _run(tmp_path, "AADS_DEPLOY_TARGET_DRAIN_MAX_WAIT=30")
    note = [line for line in out.splitlines() if line.startswith("NOTE=")][0]
    assert "active streams=" not in note


def test_block_action_keeps_old_blocking(tmp_path):
    out, log = _run(
        tmp_path,
        "AADS_DEPLOY_TARGET_DRAIN_MAX_WAIT=30; AADS_DEPLOY_DRAIN_TIMEOUT_ACTION=block",
    )
    assert "RC=1" in out
    assert "RECORD blocked bluegreen target slot aads-server-green:8102 active streams=1" in log
    assert "PHASE_END target_slot_drain blocked" in log
    assert "proceeded_busy" not in log


def test_allow_busy_target_proceeds_immediately(tmp_path):
    out, log = _run(tmp_path, "AADS_DEPLOY_ALLOW_BUSY_TARGET=true; AADS_DEPLOY_DRAIN_TIMEOUT_ACTION=block")
    assert "RC=0" in out
    assert "대기중" not in out
    assert "blocked" not in log


def test_drained_target_proceeds_without_note(tmp_path):
    out, log = _run(tmp_path, "AADS_DEPLOY_TARGET_DRAIN_MAX_WAIT=30", streams="0")
    assert "RC=0" in out
    assert "NOTE=\n" in out
    assert "proceeded_busy" not in log


def test_invalid_action_falls_back_to_proceed(tmp_path):
    out, _ = _run(tmp_path, "AADS_DEPLOY_TARGET_DRAIN_MAX_WAIT=30; AADS_DEPLOY_DRAIN_TIMEOUT_ACTION=bogus")
    assert "RC=0" in out


def test_gate_is_wired_and_final_status_records_note():
    assert "if ! target_slot_drain_gate; then" in DEPLOY_SH
    assert 'AADS_DEPLOY_DRAIN_TIMEOUT_ACTION:-proceed' in DEPLOY_SH
    final = _fn("resolve_final_deploy_status")
    assert "TARGET_DRAIN_PROCEED_NOTE" in final


def test_final_status_stays_success_with_note():
    script = f"""set -u
MODE=code
{_fn("resolve_final_deploy_status")}
TARGET_DRAIN_PROCEED_NOTE="target_drain proceeded_busy: active=1"
resolve_final_deploy_status
echo "$FINAL_DEPLOY_STATUS|$FINAL_DEPLOY_ERROR"
"""
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30).stdout
    assert out.strip() == "success|target_drain proceeded_busy: active=1"
