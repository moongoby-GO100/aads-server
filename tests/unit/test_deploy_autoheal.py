"""AADS-191 배포 자가치유 계층 단위 테스트.

분류기/정책은 bash 함수라 subprocess 로 source 해서 검증한다.
표본 문자열은 deploy_runs.error_summary 에 실제로 쌓인 값이다
(2026-09-13 07:20 KST 조회).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
AUTOHEAL_LIB = REPO_ROOT / "scripts" / "deploy_autoheal.sh"
DEPLOY_SH = REPO_ROOT / "deploy.sh"


def _call(func: str, *args: str, env_prefix: str = "") -> str:
    """autoheal 라이브러리를 source 한 뒤 함수 하나를 호출한다."""
    quoted = " ".join(f"'{a}'" for a in args)
    script = f'set -uo pipefail\n{env_prefix}source "{AUTOHEAL_LIB}"\n{func} {quoted}\n'
    proc = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"bash failed: {proc.stderr}"
    return proc.stdout.strip()


def test_library_and_hooks_exist():
    assert AUTOHEAL_LIB.is_file(), "scripts/deploy_autoheal.sh 가 없다"
    deploy_src = DEPLOY_SH.read_text(encoding="utf-8", errors="ignore")
    # 실패 경로에서 자가치유가 반드시 호출되어야 한다 (AADS-191 의 핵심)
    assert "deploy_autoheal_on_exit" in deploy_src
    assert "source \"${COMPOSE_DIR}/scripts/deploy_autoheal.sh\"" in deploy_src
    assert "DEPLOY_LAST_FAIL_ERROR" in deploy_src


def test_syntax_is_valid():
    for target in (AUTOHEAL_LIB, DEPLOY_SH):
        proc = subprocess.run(["bash", "-n", str(target)], capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, f"{target.name} 구문 오류: {proc.stderr}"


@pytest.mark.parametrize(
    "phase,err,expected",
    [
        ("preflight", "insufficient build disk", "disk_full"),
        ("preflight", "dirty worktree blocks release", "dirty_worktree"),
        ("build_candidate_image_stale_auto", "auto-reconciled: heartbeat exceeded 1200s", "stale_heartbeat"),
        ("build_candidate_image", "stale deploy reconciled before new deploy: pid=142610", "stale_heartbeat"),
        ("interrupted_post_switch", "deploy interrupted by TERM; certification incomplete", "signal_interrupt"),
        ("build_candidate_image", "deploy interrupted by INT", "signal_interrupt"),
        # HUP 중단은 최근 14일 deploy_runs 에 13건 쌓여 있다(2026-09-13 07:45 KST 조회).
        ("standby_same_digest_sync", "deploy interrupted by HUP", "signal_interrupt"),
        ("p0p1_monitoring", "deploy interrupted by HUP", "signal_interrupt"),
        ("standby_same_digest_sync", "standby same-digest sync failed for aads-server:8100", "standby_sync_fail"),
        ("build_candidate_image", "aads-server-green memory limit mismatch", "mem_limit_mismatch"),
        ("standby_same_digest_sync", "unexpected error exit=1 line=2211: echo ...", "unexpected_exit"),
        ("nginx_cutover", "something nobody has seen before", "other"),
        ("", "", "unknown"),
    ],
)
def test_classify_deploy_failure(phase: str, err: str, expected: str):
    assert _call("classify_deploy_failure", phase, err) == expected


@pytest.mark.parametrize(
    "cause,expected",
    [
        ("disk_full", "retry"),
        ("dirty_worktree", "retry"),
        ("stale_heartbeat", "retry"),
        ("standby_sync_fail", "retry"),
        ("lock_wait_timeout", "retry"),
        ("source_dir_missing", "retry"),
        ("mem_limit_mismatch", "manual"),
        ("unexpected_exit", "manual"),
        ("other", "manual"),
        ("unknown", "manual"),
    ],
)
def test_autoheal_policy(cause: str, expected: str):
    assert _call("autoheal_policy", cause) == expected


def test_signal_interrupt_policy_depends_on_cutover():
    """컷오버 후 중단은 서비스가 이미 새 슬롯에 살아 있으므로 자동 재배포 금지."""
    before = _call("autoheal_policy", "signal_interrupt", env_prefix="DEPLOY_UPSTREAM_SWITCHED=false\n")
    after = _call("autoheal_policy", "signal_interrupt", env_prefix="DEPLOY_UPSTREAM_SWITCHED=true\n")
    assert before == "retry"
    assert after == "manual"


@pytest.mark.parametrize(
    "phase,expected",
    [
        ("build_candidate_image", "retry"),
        ("target_slot_drain", "retry"),
        ("standby_same_digest_sync", "manual"),
        ("post_switch_health", "manual"),
        ("p0p1_monitoring", "manual"),
        ("llm_health_check", "manual"),
    ],
)
def test_signal_interrupt_policy_uses_phase_when_flag_lost(phase: str, expected: str):
    """DEPLOY_UPSTREAM_SWITCHED 가 유실돼도 컷오버 이후 phase 면 재배포하지 않는다."""
    out = _call(
        "autoheal_policy",
        "signal_interrupt",
        phase,
        env_prefix="unset DEPLOY_UPSTREAM_SWITCHED 2>/dev/null || true\n",
    )
    assert out == expected


def test_autoheal_disabled_is_noop():
    """킬스위치가 0이면 아무 복구도 시도하지 않고 조용히 반환한다."""
    out = _call(
        "deploy_autoheal_on_exit",
        "1",
        env_prefix="AADS_DEPLOY_AUTOHEAL=0\n",
    )
    assert "비활성화" in out


def test_retry_budget_blocks_second_attempt(tmp_path):
    """동일 SHA·원인의 재시도 예산이 소진되면 재개하지 않고 에스컬레이션한다."""
    state_dir = tmp_path / "autoheal"
    state_dir.mkdir()
    (state_dir / "testsha.disk_full.attempts").write_text("1\n", encoding="utf-8")
    env_prefix = (
        f'export AADS_DEPLOY_AUTOHEAL_STATE_DIR="{state_dir}"\n'
        'export AADS_RELEASE_SHA="testsha"\n'
        'export AADS_DEPLOY_AUTOHEAL_DRYRUN=1\n'
        'export DEPLOY_CURRENT_PHASE="preflight"\n'
        'export DEPLOY_LAST_FAIL_ERROR="insufficient build disk"\n'
        'audit_control() { :; }\n'
        'deploy_db_available() { return 1; }\n'
    )
    out = _call("deploy_autoheal_on_exit", "1", env_prefix=env_prefix)
    assert "cause=disk_full" in out
    assert "재시도 예산 소진" in out
    assert "재개 기동 완료" not in out


def test_successful_exit_skips_autoheal():
    assert _call("deploy_autoheal_on_exit", "0") == ""


def _disk_full_env(tmp_path, db_ok: bool, run_id: str = "4242") -> str:
    """disk_full 실패를 재현하되 DB/도커는 스텁으로 막는 공통 환경."""
    state_dir = tmp_path / "autoheal"
    state_dir.mkdir(exist_ok=True)
    return (
        f'export AADS_DEPLOY_AUTOHEAL_STATE_DIR="{state_dir}"\n'
        'export AADS_RELEASE_SHA="qsha"\n'
        'export AADS_DEPLOY_AUTOHEAL_DRYRUN=1\n'
        'export AADS_DEPLOY_AUTOHEAL_COOLDOWN_SEC=0\n'
        'export DEPLOY_CURRENT_PHASE="preflight"\n'
        'export DEPLOY_LAST_FAIL_ERROR="insufficient build disk"\n'
        'COMPOSE_DIR="' + str(REPO_ROOT) + '"\n'
        'STATE_DIR="' + str(REPO_ROOT) + '"\n'
        'audit_control() { :; }\n'
        'sql_escape() { echo "$1"; }\n'
        'prune_old_release_images() { :; }\n'
        'require_build_disk_free() { return 0; }\n'
        f'deploy_db_available() {{ return {0 if db_ok else 1}; }}\n'
        f'deploy_db_exec() {{ echo "{run_id if db_ok else ""}"; }}\n'
    )


def test_queue_registration_failure_blocks_retry(tmp_path):
    """큐 등록이 실패하면 워커를 띄워도 집을 릴리스가 없다 — 재개 대신 에스컬레이션."""
    out = _call("deploy_autoheal_on_exit", "1", env_prefix=_disk_full_env(tmp_path, db_ok=False))
    assert "재개 큐 등록 실패" in out
    assert "재개 기동 완료" not in out


def test_queue_registration_success_launches_retry(tmp_path):
    """큐 등록이 성공하면 교정 후 재개까지 이어진다(DRYRUN 이므로 실제 기동은 없음)."""
    out = _call("deploy_autoheal_on_exit", "1", env_prefix=_disk_full_env(tmp_path, db_ok=True))
    assert "재개 큐 등록: deploy_run_id=4242" in out
    assert "재개 기동 완료" in out


def test_worktree_is_never_mutated():
    """작업 트리를 건드리는 교정(stash/checkout/clean/reset)은 금지된다."""
    src = AUTOHEAL_LIB.read_text(encoding="utf-8", errors="ignore")
    for forbidden in ("git stash", "git checkout", "git clean", "git reset"):
        assert forbidden not in src, f"자가치유가 작업 트리를 변경한다: {forbidden}"


def _flaky_disk_stub(counter_path, fail_times: int) -> str:
    """require_build_disk_free 가 fail_times 회 실패한 뒤 성공하도록 흉내낸다."""
    return (
        f'COUNTER="{counter_path}"\n'
        'require_build_disk_free() {\n'
        '    local n=0\n'
        '    if [[ -f "$COUNTER" ]]; then n="$(cat "$COUNTER")"; fi\n'
        '    n=$(( n + 1 ))\n'
        '    echo "$n" > "$COUNTER"\n'
        f'    if (( n <= {fail_times} )); then return 1; fi\n'
        '    return 0\n'
        '}\n'
        'export AADS_DEPLOY_AUTOHEAL_DISK_RECHECK_SEC=0\n'
    )


def test_disk_recheck_waits_for_prune_to_settle(tmp_path):
    """prune 은 비동기라 즉시 재확인하면 실패한다 — 정착을 기다린 뒤 복귀를 인정한다."""
    counter = tmp_path / "calls"
    out = _call("autoheal_wait_disk_recovery", env_prefix=_flaky_disk_stub(counter, 2))
    assert "빌드 디스크 임계 복귀 확인 (3/6회차)" in out
    assert counter.read_text().strip() == "3"


def test_disk_recheck_gives_up_after_budget(tmp_path):
    """계속 임계 미달이면 무한 대기하지 않고 실패로 끝낸다."""
    counter = tmp_path / "calls"
    env_prefix = _flaky_disk_stub(counter, 99) + "export AADS_DEPLOY_AUTOHEAL_DISK_RECHECKS=3\n"
    out = _call('autoheal_wait_disk_recovery || echo "GAVE_UP"', env_prefix=env_prefix)
    assert "GAVE_UP" in out
    assert counter.read_text().strip() == "3"


def _missing_source_env(tmp_path, compose_dir: str) -> str:
    """릴리스 소스 판정만 보기 위해 DB·감사 로그를 막은 공통 환경."""
    state_dir = tmp_path / "autoheal"
    state_dir.mkdir(exist_ok=True)
    return (
        f'export AADS_DEPLOY_AUTOHEAL_STATE_DIR="{state_dir}"\n'
        'export AADS_RELEASE_SHA="srcsha"\n'
        'export AADS_DEPLOY_AUTOHEAL_DRYRUN=1\n'
        'export AADS_DEPLOY_AUTOHEAL_COOLDOWN_SEC=0\n'
        'export DEPLOY_CURRENT_PHASE="build_candidate_image"\n'
        'export DEPLOY_LAST_FAIL_ERROR="unexpected error exit=1 line=1: docker compose up"\n'
        f'COMPOSE_DIR="{compose_dir}"\n'
        'audit_control() { :; }\n'
        'deploy_db_available() { return 1; }\n'
    )


def test_missing_release_source_is_reclassified_and_retryable(tmp_path):
    """사라진 릴리스 worktree 는 unexpected_exit(manual) 이 아니라 자동복구 대상이다."""
    out = _call(
        "deploy_autoheal_on_exit",
        "1",
        env_prefix=_missing_source_env(tmp_path, str(tmp_path / "gone")),
    )
    assert "분류를 source_dir_missing 으로 보정" in out
    assert "cause=source_dir_missing" in out
    assert "policy=retry" in out


def test_present_release_source_keeps_unexpected_exit(tmp_path):
    """소스가 멀쩡하면 기존 분류를 바꾸지 않는다(오탐 방지)."""
    out = _call(
        "deploy_autoheal_on_exit",
        "1",
        env_prefix=_missing_source_env(tmp_path, str(REPO_ROOT)),
    )
    assert "cause=unexpected_exit" in out
    assert "source_dir_missing" not in out


def test_recreate_release_worktree_refuses_operational_tree():
    """운영 트리(STATE_DIR)를 릴리스 소스로 다시 만드는 일은 절대 하지 않는다."""
    env_prefix = (
        'export AADS_RELEASE_SHA="srcsha"\n'
        f'COMPOSE_DIR="{REPO_ROOT}"\n'
        f'STATE_DIR="{REPO_ROOT}"\n'
    )
    out = _call('autoheal_recreate_release_worktree || echo "REFUSED"', env_prefix=env_prefix)
    assert "REFUSED" in out


def test_error_trap_preserves_specific_failure_reason():
    """ERR 트랩의 일반 메시지가 preflight 의 구체 사유를 덮어쓰면 안 된다."""
    src = DEPLOY_SH.read_text(encoding="utf-8", errors="ignore")
    assert 'local last_fail="${DEPLOY_LAST_FAIL_ERROR:-}"' in src
    assert 'record_deploy "failed" "$MODE" "$detail"' in src
