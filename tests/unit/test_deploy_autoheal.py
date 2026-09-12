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
