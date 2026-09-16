import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SYNC_SCRIPT = ROOT / "scripts" / "sync_pipeline_runner_remote.sh"


def test_remote_runner_sync_script_has_fail_closed_install_flow():
    script = (ROOT / "scripts" / "sync_pipeline_runner_remote.sh").read_text(encoding="utf-8")

    assert "set -euo pipefail" in script
    assert "bash -n \"$CANONICAL_RUNNER\"" in script
    assert "bash -n '$tmp'" in script
    assert "installed hash mismatch" in script
    assert "flock -n 9" in script
    assert 'ssh -n "${SSH_OPTS[@]}" "$host" "$@"' in script
    assert "cp -p '$remote_runner'" in script
    assert "systemctl restart '$service'" in script
    assert "systemctl is-active '$service'" in script
    assert ' "$changed" == "1" || "$unit_changed" == "1" ' in script
    assert 'read -r target || [[ -n "$target" ]]' in script
    assert "return 0\n}" in script


def test_remote_runner_sync_targets_cover_all_remote_runner_hosts():
    script = (ROOT / "scripts" / "sync_pipeline_runner_remote.sh").read_text(encoding="utf-8")

    assert "contabo14|contabo14|/root/scripts/pipeline-runner.sh|aads-pipeline-runner.service" in script
    assert "cafe24_114|server-114|/root/scripts/pipeline-runner.sh|aads-pipeline-litellm-runner.service" in script
    assert "aads-pipeline-litellm-runner.211.service" in script
    assert "aads-pipeline-litellm-runner.114.service" in script


def test_remote_runner_sync_timer_runs_periodically():
    timer = (ROOT / "scripts" / "aads-pipeline-runner-sync.timer").read_text(encoding="utf-8")
    service = (ROOT / "scripts" / "aads-pipeline-runner-sync.service").read_text(encoding="utf-8")

    assert "OnBootSec=2min" in timer
    assert "OnUnitActiveSec=5min" in timer
    assert "Persistent=true" in timer
    assert "ExecStart=/root/aads/aads-server/scripts/sync_pipeline_runner_remote.sh" in service


# ── 실행중 러너 보호 (AADS-RUNNER-SYNC-BUSY-DEFER, 2026-09-16) ──────────
# 배경: 10:28:51 KST 동기화 재시작이 GO100 runner-1791da41(P0-CRITICAL) 을
# runner_shutdown_requeued 로 되돌렸고, 작업은 10:29:33 에 처음부터 다시 시작했다.
# 6분 16초치 LLM 작업과 비용이 버려졌다.


def _sync_script() -> str:
    return SYNC_SCRIPT.read_text(encoding="utf-8")


def _extract_function(script: str, name: str) -> str:
    start = script.index(f"{name}() {{")
    end = script.index("\n}\n", start) + len("\n}\n")
    return script[start:end]


def _busy_lib() -> str:
    return (ROOT / "scripts" / "runner_busy_lib.sh").read_text(encoding="utf-8")


def test_busy_rule_lives_in_one_place_shared_by_both_restart_paths():
    """원격 동기화와 로컬 재시작이 다른 답을 내면 한쪽이 반드시 사고를 낸다."""
    lib = _busy_lib()
    sync = _sync_script()
    local = (ROOT / "scripts" / "restart_local_runner.sh").read_text(encoding="utf-8")

    assert "should_defer_for_busy() {" in lib
    assert "db_active_job_count() {" in lib
    # 규칙은 라이브러리에만 있어야 한다(복사본 금지)
    assert "should_defer_for_busy() {" not in sync
    assert "should_defer_for_busy() {" not in local
    for caller in (sync, local):
        assert 'source "${SCRIPT_DIR}/runner_busy_lib.sh"' in caller


def test_busy_gate_exists_and_is_configurable():
    script = _sync_script()

    assert "remote_runner_host_name() {" in script
    assert "remote_service_active() {" in script
    assert 'IGNORE_BUSY="${AADS_RUNNER_SYNC_IGNORE_BUSY:-0}"' in script
    assert "--ignore-busy)" in script
    assert "deferred=${DEFERRED}" in script


def test_busy_gate_blocks_install_not_only_restart():
    """실행 중 스크립트 파일 교체도 위험하다(bash 지연 읽기). 게이트는 설치 앞에 와야 한다."""
    script = _sync_script()

    gate = script.index("if should_defer_for_busy")
    local_sha = script.index('local_sha=$(sha256_file "$CANONICAL_RUNNER")')
    install = script.index('install -m 0755')
    restart = script.index("systemctl restart '$service'")

    assert gate < local_sha < install < restart


def test_runner_host_name_is_resolved_at_runtime_not_hardcoded():
    """cafe24_114 의 runner_host 는 rfree-0009 다 — 타깃 이름으로 추정하면 게이트가 영영 안 걸린다."""
    script = _sync_script()

    assert "AADS_RUNNER_HOST_NAME=" in script
    assert "hostname -s" in script
    # 하드코딩 금지 — 주석의 실측 근거 기록은 허용하고, 실행되는 코드에만 없으면 된다.
    code_only = "\n".join(
        line for line in script.splitlines() if not line.lstrip().startswith("#")
    )
    assert "rfree-0009" not in code_only
    # SQL 주입 방지: 원격에서 받은 이름은 검증 후에만 쿼리에 들어간다
    assert '[[ "$name" =~ ^[A-Za-z0-9._-]+$ ]] || name=""' in script


def test_active_job_query_counts_only_in_flight_states():
    lib = _busy_lib()

    assert "status IN ('claimed','running','deploying')" in lib
    assert "runner_host='${host_name}'" in lib
    # 원격에서 받은 호스트 이름은 검증 후에만 쿼리에 들어간다(SQL 주입 방지)
    assert '[[ "$host_name" =~ ^[A-Za-z0-9._-]+$ ]]' in lib


@pytest.fixture(scope="module")
def defer_fn(tmp_path_factory):
    if shutil.which("bash") is None:
        pytest.skip("bash 미설치 환경")
    fn_dir = tmp_path_factory.mktemp("defer_fn")
    fn_file = fn_dir / "fn.sh"
    fn_file.write_text(
        "set -eo pipefail\n" + _extract_function(_busy_lib(), "should_defer_for_busy"),
        encoding="utf-8",
    )
    return fn_file


def _decide(fn_file: Path, busy_count: str, ignore_busy: str = "0", service_state: str = "") -> str:
    proc = subprocess.run(
        [
            "bash",
            "-c",
            f'source "{fn_file}"; if should_defer_for_busy "{busy_count}" "{ignore_busy}" "{service_state}"; '
            f"then echo DEFER; else echo PROCEED; fi",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


@pytest.mark.parametrize(
    "busy_count,expected",
    [
        ("0", "PROCEED"),   # 유휴 → 동기화 진행
        ("1", "DEFER"),     # 작업 1건 실행 중 → 미룸
        ("3", "DEFER"),
        ("", "DEFER"),      # 판별 불가 → fail-closed
        ("x", "DEFER"),     # 비정상 출력 → fail-closed
    ],
)
def test_should_defer_for_busy_decisions(defer_fn, busy_count, expected):
    assert _decide(defer_fn, busy_count) == expected


def test_ignore_busy_overrides_the_gate(defer_fn):
    """러너가 멈춰 작업이 영원히 running 으로 남는 경우의 탈출구."""
    assert _decide(defer_fn, "5", ignore_busy="1") == "PROCEED"
    assert _decide(defer_fn, "", ignore_busy="1") == "PROCEED"


# ── 좀비 무한 defer 방지 (리스크2) ──────────────────────────────────────
# 러너 서비스가 죽으면 그 호스트의 running job 은 회수 주체가 없어 영구 잔존한다.
# 그 상태에서 busy 로만 판정하면 그 호스트는 5분 타이머마다 영원히 defer 된다.


@pytest.mark.parametrize("dead_state", ["inactive", "failed"])
def test_dead_runner_service_does_not_block_sync(defer_fn, dead_state):
    """러너가 죽어 있으면 남은 job 은 좀비다 — 재시작이 곧 복구다."""
    assert _decide(defer_fn, "3", service_state=dead_state) == "PROCEED"
    assert _decide(defer_fn, "", service_state=dead_state) == "PROCEED"


def test_live_runner_with_jobs_still_defers(defer_fn):
    assert _decide(defer_fn, "1", service_state="active") == "DEFER"
    assert _decide(defer_fn, "0", service_state="active") == "PROCEED"


def test_busy_rule_avoids_time_based_stale_heuristic():
    """updated_at 은 status='deploying' 구간에서만 갱신된다 — 시간 기반 판정은 오판한다."""
    lib = _busy_lib()

    assert "updated_at" not in lib.split("should_defer_for_busy")[1]
    assert "INTERVAL" not in lib


# ── 116 로컬 러너 래퍼 (리스크1) ───────────────────────────────────────


def test_local_restart_wrapper_is_gated_and_reversible():
    local = (ROOT / "scripts" / "restart_local_runner.sh").read_text(encoding="utf-8")

    assert "set -euo pipefail" in local
    assert "AADS_RUNNER_HOST_NAME=//p" in local   # 러너와 같은 이름 해석
    assert "hostname -s" in local
    assert "if should_defer_for_busy" in local
    assert "exit 3" in local                       # 미룸은 성공(0)이 아니다
    assert "--ignore-busy" in local
    assert 'systemctl restart "$SERVICE"' in local


def test_local_restart_wrapper_checks_before_restarting():
    local = (ROOT / "scripts" / "restart_local_runner.sh").read_text(encoding="utf-8")

    gate = local.index("if should_defer_for_busy")
    restart = local.index('systemctl restart "$SERVICE"')
    assert gate < restart
