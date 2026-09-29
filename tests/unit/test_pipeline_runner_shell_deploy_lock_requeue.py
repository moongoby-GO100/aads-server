"""AADS-LLM-M6-DEPLOY-REGRESSION-GUARD: 셸 러너의 배포 락 대기 회귀시험.

AADS 작업은 pipeline_runner_service.py 의 `if self.project != "AADS"` 가드 때문에
셸 경로(scripts/pipeline-runner.sh deploy_job)만 탄다. 셸 경로는 30+60+90=180초만
기다리고 status='error' 로 확정했는데, AADS bluegreen 실측(deploy_runs success, 24h
n=16)은 중앙값 514s·최대 631s 였다. 24시간에 승인 작업 8건이 이렇게 폐기됐다.
Python 경로의 재큐잉 테스트(test_pipeline_runner_deploy_lock_requeue.py)는 이
경로를 덮지 않았으므로 게이트가 초록인 채 결함이 살아남았다.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = (ROOT / "scripts" / "pipeline-runner.sh").read_text(encoding="utf-8")

# 실측 최대 배포 시간 (deploy_runs status=success, 24h n=16)
MEASURED_MAX_DEPLOY_SEC = 631


def _function(name: str) -> str:
    start = SCRIPT.index(f"{name}() {{")
    return SCRIPT[start : SCRIPT.index("\n}\n", start) + 3]


def _config_lines() -> str:
    return "\n".join(
        line for line in SCRIPT.splitlines() if re.match(r"^DEPLOY_LOCK_[A-Z_]+=", line)
    )


HARNESS = r"""
set -eo pipefail
AADS_API_URL="http://fake-api"
log() { echo "LOG $*" >&2; }
db_update() { printf '%s\n---\n' "$1" >> "$WORK/db.log"; }
record_runner_event() { echo "EVENT $*" >> "$WORK/events.log"; }
post_to_chat() { echo "CHAT $*" >> "$WORK/events.log"; }
_release_deploy_lock() { echo "RELEASE $*" >> "$WORK/events.log"; }
_notify_ai() { :; }
promote_next_queued() { :; }
sleep() { echo "$1" >> "$WORK/sleep.log"; }
_slept() { awk '{s+=$1} END {print s+0}' "$WORK/sleep.log" 2>/dev/null || echo 0; }
curl() {
    local n
    n=$(( $(cat "$WORK/calls" 2>/dev/null || echo 0) + 1 ))
    echo "$n" > "$WORK/calls"
    case "$FAKE_LOCK_MODE" in
        down) return 22 ;;
        busy) echo '{"acquired":false,"holder":"runner-other","wait_seconds":300}' ;;
        busy_until_sec:*)
            if (( $(_slept) < ${FAKE_LOCK_MODE#busy_until_sec:} )); then
                echo '{"acquired":false,"holder":"runner-other","wait_seconds":300}'
            else
                echo '{"acquired":true,"holder":null,"wait_seconds":0}'
            fi ;;
        free) echo '{"acquired":true,"holder":null,"wait_seconds":0}' ;;
    esac
}
"""


@pytest.fixture(scope="module")
def fn_file(tmp_path_factory):
    if shutil.which("bash") is None:
        pytest.skip("bash 미설치 환경")
    path = tmp_path_factory.mktemp("dl") / "fn.sh"
    path.write_text(
        HARNESS + "\n" + _config_lines() + "\n" + _function("acquire_deploy_lock_with_requeue"),
        encoding="utf-8",
    )
    return path


def _run(fn_file: Path, work: Path, mode: str, max_wait: str | None = None):
    env = {"PATH": "/usr/bin:/bin", "WORK": str(work), "FAKE_LOCK_MODE": mode}
    if max_wait is not None:
        env["DEPLOY_LOCK_MAX_WAIT_SEC"] = max_wait
    proc = subprocess.run(
        [
            "bash",
            "-c",
            f'source "{fn_file}"; '
            'acquire_deploy_lock_with_requeue runner-test AADS sess-1',
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    db = (work / "db.log").read_text() if (work / "db.log").exists() else ""
    events = (work / "events.log").read_text() if (work / "events.log").exists() else ""
    sleeps = (work / "sleep.log").read_text().split() if (work / "sleep.log").exists() else []
    return proc, db, events, sum(int(s) for s in sleeps)


def test_lock_busy_within_budget_requeues_not_terminal(fn_file, tmp_path):
    """① 락 점유 중 상한 내 → status='queued' 재큐잉, terminal 아님, 획득 후 deploying 복귀."""
    proc, db, events, slept = _run(fn_file, tmp_path, "busy_until_sec:150")

    assert proc.returncode == 0, proc.stderr
    assert "status='queued', phase='deploy_lock_wait'" in db
    assert "[배포대기]" in db and "holder=runner-other" in db
    assert "status='error'" not in db
    assert "deploy_lock_fail" not in db
    assert "completed_at=NOW()" not in db
    # 획득 뒤에는 대기 표식에서만 deploying 으로 되돌린다
    assert "SET status='deploying', phase='deploying'" in db
    assert "AND status='queued' AND phase='deploy_lock_wait'" in db
    assert "deploy_lock_requeued" in events
    assert "job_terminal" not in events
    assert slept >= 150


def test_default_budget_survives_measured_max_deploy_time(fn_file, tmp_path):
    """기본 상한은 실측 최대 631s 동안 점유돼도 승인 작업을 죽이지 않아야 한다(예전 180s 는 죽였다)."""
    proc, db, events, slept = _run(fn_file, tmp_path, f"busy_until_sec:{MEASURED_MAX_DEPLOY_SEC}")

    assert proc.returncode == 0, proc.stderr
    assert "status='error'" not in db
    assert "job_terminal" not in events
    assert slept >= MEASURED_MAX_DEPLOY_SEC


def test_lock_busy_beyond_budget_is_terminal_with_wait_and_holder(fn_file, tmp_path):
    """② 상한 초과 → terminal, error_detail 에 누적대기와 holder 기록."""
    proc, db, events, slept = _run(fn_file, tmp_path, "busy", max_wait="700")

    assert proc.returncode == 1
    assert slept == 700  # 상한을 넘겨 자지 않는다
    # terminal 전에는 재큐잉 단계를 거쳤다
    assert db.index("status='queued', phase='deploy_lock_wait'") < db.index("status='error'")
    assert "phase='deploy_lock_fail'" in db
    detail = re.search(r"error_detail='([^']*)'", db).group(1)
    assert detail.startswith("deploy_lock_fail:")
    assert "waited=700s" in detail and "max_wait=700s" in detail
    assert "holder=runner-other" in detail
    assert "completed_at=NOW()" in db
    assert "job_terminal error deploy_lock_fail" in events
    assert '"waited_sec":700' in events
    assert "RELEASE AADS runner-test" in events


def test_lock_api_unresponsive_proceeds_without_lock(fn_file, tmp_path):
    """③ API 무응답 → 기존대로 잠금 없이 진행, 대기/DB 변경 없음."""
    proc, db, events, slept = _run(fn_file, tmp_path, "down")

    assert proc.returncode == 0, proc.stderr
    assert "DEPLOY_LOCK_API_OK" in proc.stderr
    assert db == ""
    assert slept == 0
    assert (tmp_path / "calls").read_text().strip() == "1"


def test_lock_free_acquires_immediately_without_db_writes(fn_file, tmp_path):
    proc, db, events, slept = _run(fn_file, tmp_path, "free")

    assert proc.returncode == 0, proc.stderr
    assert db == "" and slept == 0
    # 이미 쥔 락을 다시 acquire 하지 않는다(재호출하면 holder=자기자신 으로 '점유' 응답)
    assert (tmp_path / "calls").read_text().strip() == "1"


def test_invalid_budget_env_falls_back_to_default(fn_file, tmp_path):
    proc, db, events, slept = _run(fn_file, tmp_path, "busy", max_wait="abc")

    assert proc.returncode == 1
    assert slept == 900


def test_budget_is_env_configurable_and_covers_measured_max():
    config = _config_lines()
    m = re.search(r'^DEPLOY_LOCK_MAX_WAIT_SEC="\$\{DEPLOY_LOCK_MAX_WAIT_SEC:-(\d+)\}"$', config, re.M)
    assert m, config
    assert int(m.group(1)) > MEASURED_MAX_DEPLOY_SEC


def test_deploy_job_uses_requeue_helper_and_old_180s_loop_is_gone():
    deploy = _function("deploy_job")
    assert 'acquire_deploy_lock_with_requeue "$job_id" "$project" "$session_id"' in deploy
    assert "_dl_try" not in SCRIPT
    assert "30 * _dl_try" not in SCRIPT


def test_shutdown_restores_lock_waiting_job_to_approved():
    """락 대기 중 러너가 종료되면 코딩 재실행이 아니라 approved 로 돌아가 배포만 재시도한다."""
    body = _function("_shutdown_finalize_job")
    assert "SET status='approved', phase='approved'" in body
    assert "status='queued' AND phase='${DEPLOY_LOCK_WAIT_PHASE}'" in body


def test_lock_wait_phase_is_not_claimable_as_coding_job():
    """claim_queued_job 이 대기 중 작업을 새 코딩 작업으로 집어가면 승인 산출물이 버려진다."""
    claim = _function("claim_queued_job")
    assert "p.phase IN ('queued','coding')" in claim
    assert "deploy_lock_wait" not in claim
