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
        # 2026-09-21 24시간 실측: 실패·차단 48건 중 14건이 이 사유였는데 분류가
        # 없어 other → manual 로 빠졌다. 한 건도 자동 재개되지 않았다.
        ("target_slot_drain", "target slot aads-server-green:8102 active streams=1", "target_drain_busy"),
        ("target_slot_drain", "target slot aads-server:8100 active streams=2", "target_drain_busy"),
        # 2026-09-21 24시간 실측: build_candidate_image 실패 17건 전부가 이 사유였다.
        # deploy.sh 가 DEPLOY_LAST_FAIL_ERROR 로 올려 주기 전에는 error_summary 에
        # "return 1" 만 남아 unexpected_exit(manual) 로 빠졌다.
        ("build_candidate_image", "dependency image missing: aads-server-deps:de49c580f19a; warm-deps required", "dependency_image_missing"),
        ("build_candidate_image", "immutable dependency image mismatch: image=aads-server-deps:de49c580f19a; expected=de49c580f19a; actual=missing", "dependency_image_mismatch"),
        ("build_candidate_image", "dependency image verification failed: aads-server-deps:de49c580f19a", "dependency_image_mismatch"),
        ("build_candidate_image", "immutable image tag mismatch: tag=3ab5c899 label=missing", "release_image_tag_mismatch"),
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
        # 고칠 것이 없는 실패다 — 몇 분 뒤면 같은 배포가 그대로 성공한다.
        ("target_drain_busy", "retry"),
        # warm-deps 로 의존성 이미지를 만들면 같은 릴리스가 그대로 통과한다.
        ("dependency_image_missing", "retry"),
        ("dependency_image_mismatch", "retry"),
        # 같은 태그가 다른 revision 을 가리키는 무결성 위반 — 덮어쓰지 않는다.
        ("release_image_tag_mismatch", "manual"),
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
    assert "재개 큐/실행 가능한 후속 run 확인: deploy_run_id=4242" in out
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


def test_present_release_source_keeps_original_cause(tmp_path):
    """소스가 멀쩡하면 source_dir_missing 보정을 걸지 않는다(오탐 방지).

    2026-09-13 분류 세분화(D6) 이후 "unexpected error exit=... docker compose ..."
    는 unexpected_exit 이 아니라 container_recreate_fail 로 잡힌다. 컷오버
    이전이면 재시도할 값이 있는 실패라 별도 분류로 뺀 것이다. 이 테스트가
    지키려는 것은 분류 이름이 아니라 "소스가 멀쩡하면 보정이 끼어들지 않는다"
    이므로, 기대값만 현재 분류에 맞춘다.
    """
    out = _call(
        "deploy_autoheal_on_exit",
        "1",
        env_prefix=_missing_source_env(tmp_path, str(REPO_ROOT)),
    )
    assert "cause=container_recreate_fail" in out
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


def test_disk_preflight_records_measured_numbers():
    """디스크 차단 사유에 실측 수치(available/required/short)가 들어가야 한다.

    #376(2026-09-13 08:50 KST)은 error_summary 가 'insufficient build disk'
    한 줄뿐이어서 얼마나 모자란지 로그 파일을 열어야만 알 수 있었다.
    """
    src = DEPLOY_SH.read_text(encoding="utf-8", errors="ignore")
    assert "DEPLOY_DISK_FAIL_DETAIL=" in src
    assert "short=$(((min_free_kb - avail_kb) / 1024))MB" in src
    assert 'disk_reason="insufficient build disk (${DEPLOY_DISK_FAIL_DETAIL})"' in src
    assert 'record_deploy "blocked" "$MODE" "$disk_reason"' in src


def test_disk_fail_detail_is_populated_on_block():
    """require_build_disk_free 가 막을 때 실제로 detail 변수를 채우는지 실행 검증."""
    script = (
        'set -uo pipefail\n'
        'audit_control() { :; }\n'
        'build_disk_check_path() { echo "/"; }\n'
        f'source <(sed -n "/^require_build_disk_free()/,/^}}/p" "{DEPLOY_SH}")\n'
        'AADS_DEPLOY_MIN_FREE_GB=999999\n'
        'require_build_disk_free >/dev/null 2>&1\n'
        'echo "DETAIL=${DEPLOY_DISK_FAIL_DETAIL:-EMPTY}"\n'
    )
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert "available=" in proc.stdout
    assert "required=" in proc.stdout
    assert "short=" in proc.stdout


def test_autoheal_escalation_is_recorded_in_db():
    """에스컬레이션 결과가 deploy_runs.error_summary 에 남아야 한다.

    로그·audit_control·텔레그램에만 남기면 대시보드/DB 를 보는 쪽에서는
    '재개를 시도했는지' 자체를 알 수 없다.
    """
    src = AUTOHEAL_LIB.read_text(encoding="utf-8", errors="ignore")
    assert "autoheal_record_outcome()" in src
    assert 'autoheal_record_outcome "escalated"' in src
    assert "UPDATE deploy_runs" in src
    assert "CONCAT_WS(' | '" in src


def test_unknown_mode_exits_before_touching_deploy_ledger():
    """배포가 아닌 모드는 DB/큐를 건드리기 전에 즉시 끝나야 한다.

    2026-09-13 09:09 KST: `deploy.sh status` 호출이 모드 검사(code_validation)
    전에 preflight 를 통과하고 큐 릴리스를 claim 해, deploy_runs 에
    #377/#378 실패 2건을 남겼다.
    """
    proc = subprocess.run(
        ["bash", str(DEPLOY_SH), "status"],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(REPO_ROOT),
    )
    assert proc.returncode == 2, f"rc={proc.returncode} out={proc.stdout[-400:]}"
    combined = proc.stdout + proc.stderr
    assert "알 수 없는 모드" in combined
    # 큐 claim / preflight 까지 내려가면 안 된다.
    assert "claimed queued deploy_run_id" not in combined
    assert "phase=preflight" not in combined


def test_autoheal_retry_mode_is_sanitized():
    """재개 워커에 배포 모드가 아닌 MODE 가 그대로 전달되면 안 된다."""
    src = AUTOHEAL_LIB.read_text(encoding="utf-8", errors="ignore")
    assert 'local retry_mode="${MODE:-bluegreen}"' in src
    assert 'bash "$launcher" "$retry_mode" "autoheal_${cause}"' in src
    assert 'retry_mode="bluegreen"' in src


def test_autoheal_records_retry_launch_too():
    """재개 성공도 DB 에 남아야 '재시도했는지' 를 DB 만 보고 알 수 있다."""
    src = AUTOHEAL_LIB.read_text(encoding="utf-8", errors="ignore")
    assert 'autoheal_record_outcome "retry_launched"' in src


def test_autoheal_record_outcome_noop_without_run_id():
    """DEPLOY_RUN_ID 가 없으면 DB 를 건드리지 않고 조용히 끝나야 한다."""
    env_prefix = 'DEPLOY_RUN_ID=""\n'
    out = _call('autoheal_record_outcome "escalated" "disk_full" "x" && echo "NOOP_OK"', env_prefix=env_prefix)
    assert "NOOP_OK" in out


def _sql_capture_env(sql_log) -> str:
    """deploy_db_exec 가 실행한 SQL 을 파일로 받아 두는 공통 스텁."""
    return (
        'DEPLOY_RUN_ID="4242"\n'
        'audit_control() { :; }\n'
        'sql_escape() { echo "$1"; }\n'
        'deploy_db_available() { return 0; }\n'
        f'deploy_db_exec() {{ printf "%s\\n" "$1" >> "{sql_log}"; }}\n'
    )


def test_dirty_reroute_is_recorded_as_superseded_not_blocked(tmp_path):
    """dirty 게이트 중단은 실패가 아니라 경로 변경으로 원장에 남아야 한다.

    2026-09-16 11:20 KST 실측: 최근 3일 deploy_runs blocked 27건이 전부
    preflight/dirty_worktree 였고 후속 런은 모두 success 였다. 원본 행이
    blocked 로 남으면 대시보드에서 실패한 배포로 읽힌다.

    단, 원장을 바꾸는 것은 실행 가능한 후속 run 이 확인된 경우에만 해야
    한다(AI 리뷰 P1) — AUTOHEAL_SUCCESSOR_RUN_ID 가 비어 있으면 세탁하지
    않는다는 것은 아래 test_dirty_reroute_without_verified_successor_keeps_ledger
    가 별도로 검증한다.
    """
    sql_log = tmp_path / "sql.log"
    _call(
        'AUTOHEAL_LAST_REMEDIATION="route_clean_worktree"; '
        'AUTOHEAL_SUCCESSOR_RUN_ID=4243; AADS_RELEASE_SHA=abc1234; '
        'autoheal_record_outcome "retry_launched" "dirty_worktree" "release=abc1234"',
        env_prefix=_sql_capture_env(sql_log),
    )
    sql = sql_log.read_text(encoding="utf-8")
    assert "SET status = 'superseded'" in sql
    assert "superseded_by_autoheal_reroute" in sql
    # 진짜로 막힌 배포를 덮지 않도록 조건이 좁혀져 있어야 한다.
    assert "AND status = 'blocked'" in sql
    assert "AND phase = 'preflight'" in sql
    # 후속 run 확인 조건은 워커의 실제 claim 조건과 같아야 한다(AI 리뷰 P1).
    assert "successor.id = 4243" in sql
    assert "successor.release_sha = 'abc1234'" in sql
    assert "successor.phase='queued_for_deploy'" in sql
    assert "COALESCE(successor.auto_start, FALSE) = TRUE" in sql
    assert "successor.status IN ('running','verifying','syncing_standby')" in sql
    assert "autoheal successor deploy_run_id=4243" in sql


def test_dirty_reroute_without_verified_successor_keeps_ledger(tmp_path):
    """후속 run 이 확인되지 않았으면(AUTOHEAL_SUCCESSOR_RUN_ID 미설정) 원장을 세탁하지 않는다.

    f19c3491 (반려됨)의 실패 원인 — 후속 run 존재를 확인하지 않고도 outcome
    문자열만으로 superseded 를 찍었다. queue-empty/미확인 성공을 그대로
    믿으면 실제로는 아무도 재개하지 않았는데 대시보드는 '전환됨'으로 읽는다.
    """
    sql_log = tmp_path / "sql.log"
    _call(
        'AUTOHEAL_LAST_REMEDIATION="route_clean_worktree"; '
        'autoheal_record_outcome "retry_launched" "dirty_worktree" "release=abc1234"',
        env_prefix=_sql_capture_env(sql_log),
    )
    sql = sql_log.read_text(encoding="utf-8")
    assert "superseded_by_autoheal_reroute" not in sql
    assert "SET status = 'superseded'" not in sql


def test_non_dirty_retry_keeps_original_status(tmp_path):
    """dirty 재라우팅이 아닌 재시도는 원장 status 를 바꾸면 안 된다."""
    sql_log = tmp_path / "sql.log"
    _call(
        'AUTOHEAL_LAST_REMEDIATION="disk_reclaim"; '
        'AUTOHEAL_SUCCESSOR_RUN_ID=4243; AADS_RELEASE_SHA=abc1234; '
        'autoheal_record_outcome "retry_launched" "disk_full" "release=abc1234"',
        env_prefix=_sql_capture_env(sql_log),
    )
    sql = sql_log.read_text(encoding="utf-8")
    assert "superseded_by_autoheal_reroute" not in sql
    assert "CONCAT_WS" in sql


def test_drain_ledger_changes_only_with_verified_successor(tmp_path):
    """target_drain_busy 재개도 dirty_worktree 와 같은 검증 없이는 원장을 바꾸면 안 된다."""
    sql_log = tmp_path / "sql.log"
    env = _sql_capture_env(sql_log) + 'AADS_RELEASE_SHA="abc1234"\n'
    _call(
        'autoheal_record_outcome "retry_launched" "target_drain_busy" "release=abc1234"',
        env_prefix=env,
    )
    assert "superseded_by_autoheal_drain_retry" not in sql_log.read_text()
    _call(
        'AUTOHEAL_SUCCESSOR_RUN_ID=4243; '
        'autoheal_record_outcome "retry_launched" "target_drain_busy" "release=abc1234"',
        env_prefix=env,
    )
    sql = sql_log.read_text()
    assert "superseded_by_autoheal_drain_retry" in sql
    assert "AND phase = 'target_slot_drain'" in sql
    assert "successor.release_sha = 'abc1234'" in sql
    assert "successor.id = 4243" in sql
    assert "COALESCE(successor.auto_start, FALSE) = TRUE" in sql


def test_queue_successor_lookup_matches_worker_claim_conditions(tmp_path):
    """후속 run 인정 조건은 scripts/start_aads_deploy_queue_worker.sh 의 실제 claim 조건과 같아야 한다.

    f19c3491 (반려됨)은 status IN ('queued','running','verifying','syncing_standby')
    만으로 후속을 인정했다. 그 워커는 queued 행 중 auto_start=TRUE AND
    phase='queued_for_deploy' 인 것만 집는다 — auto_start=false 수동 대기 행은
    아무도 자동으로 집지 않으므로 successor 로 인정하면 원장이 거짓말을 한다.
    """
    sql_log = tmp_path / "sql.log"
    env = (
        'AADS_RELEASE_SHA=abc1234\nDEPLOY_RUN_ID=4242\n'
        'sql_escape() { echo "$1"; }\n'
        'deploy_db_available() { return 0; }\n'
        f'deploy_db_exec() {{ printf "%s\\n" "$1" >> "{sql_log}"; echo 4243; }}\n'
    )
    _call('queue_autoheal_retry_request target_drain_busy', env_prefix=env)
    sql = sql_log.read_text()
    assert "phase='queued_for_deploy' AND COALESCE(auto_start, FALSE) = TRUE" in sql
    assert "status IN ('running','verifying','syncing_standby')" in sql
    # 워커가 claim 하지 않는 조건(auto_start 무관 queued 전부)이 섞여 들어가면
    # 안 된다 — 이것이 반려된 버전의 정확한 결함이었다.
    assert "status IN ('queued','running','verifying','syncing_standby')" not in sql


def test_queue_requires_insert_or_active_successor():
    env = (
        'AADS_RELEASE_SHA=abc1234\nDEPLOY_RUN_ID=4242\n'
        'audit_control() { :; }\n'
        'sql_escape() { echo "$1"; }\n'
        'deploy_db_available() { return 0; }\n'
        'deploy_db_exec() { :; }\n'
    )
    out = _call('queue_autoheal_retry_request target_drain_busy || echo QUEUE_FAILED; '
                'echo "SUCCESSOR=${AUTOHEAL_SUCCESSOR_RUN_ID:-none}"', env_prefix=env)
    assert "QUEUE_FAILED" in out
    assert "SUCCESSOR=none" in out


def test_queue_accepts_verified_active_successor():
    env = (
        'AADS_RELEASE_SHA=abc1234\nDEPLOY_RUN_ID=4242\n'
        'sql_escape() { echo "$1"; }\n'
        'deploy_db_available() { return 0; }\n'
        'deploy_db_exec() { echo 4243; }\n'
    )
    out = _call('queue_autoheal_retry_request target_drain_busy; '
                'echo "SUCCESSOR=$AUTOHEAL_SUCCESSOR_RUN_ID"', env_prefix=env)
    assert "SUCCESSOR=4243" in out


def test_queue_rejects_original_run_id():
    env = (
        'AADS_RELEASE_SHA=abc1234\nDEPLOY_RUN_ID=4242\n'
        'sql_escape() { echo "$1"; }\n'
        'deploy_db_available() { return 0; }\n'
        'deploy_db_exec() { echo 4242; }\n'
    )
    out = _call(
        'queue_autoheal_retry_request target_drain_busy || echo QUEUE_FAILED',
        env_prefix=env,
    )
    assert "QUEUE_FAILED" in out


def test_worker_failure_does_not_supersede_blocked_run(tmp_path):
    """워커 launcher 를 찾지 못하면(재개 실패) blocked 행을 superseded 로 세탁하면 안 된다."""
    sql_log = tmp_path / "sql.log"
    env = (
        _sql_capture_env(sql_log)
        + 'AADS_RELEASE_SHA=abc1234\nAUTOHEAL_SUCCESSOR_RUN_ID=4243\n'
        + 'COMPOSE_DIR=/no/such/dir\nSTATE_DIR=/no/such/dir\n'
    )
    out = _call(
        'launch_autoheal_worker target_drain_busy || '
        'autoheal_record_outcome "escalated" "target_drain_busy" "worker failed"',
        env_prefix=env,
    )
    assert "큐 워커 런처를 찾을 수 없다" in out
    assert "escalated/target_drain_busy" in out
    assert "superseded_by_autoheal_drain_retry" not in sql_log.read_text()


def test_drain_worker_failure_preserves_original_ledger(tmp_path):
    """전체 경로: 큐 등록은 성공(후속 확인)해도 워커 기동 자체가 실패하면 원장은 그대로다."""
    sql_log = tmp_path / "sql.log"
    env = (
        f'export AADS_DEPLOY_AUTOHEAL_STATE_DIR="{tmp_path}"\n'
        'AADS_RELEASE_SHA=abc1234\nDEPLOY_RUN_ID=4242\n'
        'AADS_DEPLOY_AUTOHEAL_DRAIN_WAIT=0\n'
        'DEPLOY_CURRENT_PHASE=target_slot_drain\n'
        'DEPLOY_LAST_FAIL_ERROR="target slot active streams=1"\n'
        'COMPOSE_DIR=/no/such/dir\nSTATE_DIR=/no/such/dir\n'
        'audit_control() { :; }\n'
        'sql_escape() { echo "$1"; }\n'
        'deploy_db_available() { return 0; }\n'
        f'deploy_db_exec() {{ printf "%s\\n" "$1" >> "{sql_log}"; echo 4243; }}\n'
    )
    out = _call('deploy_autoheal_on_exit 1', env_prefix=env)
    assert "재개 워커 기동 실패" in out
    sql = sql_log.read_text()
    assert "superseded_by_autoheal_drain_retry" not in sql
    assert "autoheal escalated" in sql


def test_worker_queue_empty_success_is_not_treated_as_launch(tmp_path):
    """워커가 exit 0 으로 끝나도 'deploy queue empty' 를 출력하면 재개로 인정하지 않는다.

    scripts/start_aads_deploy_queue_worker.sh 는 claim 할 행이 없어도 성공
    종료한다(host drain 이 아니라 로컬에서 아무 것도 하지 않았다는 뜻). exit
    code 만 보면 이것도 성공으로 오인해, 아무도 재개하지 않았는데
    retry_launched 를 기록하는 상태 세탁이 된다(AI 리뷰 P1).
    """
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    launcher = scripts_dir / "start_aads_deploy_queue_worker.sh"
    launcher.write_text("#!/bin/bash\necho 'deploy queue empty'\nexit 0\n")
    launcher.chmod(0o755)
    env = (
        f'COMPOSE_DIR="{tmp_path}"\nSTATE_DIR="{tmp_path}"\nAADS_DEPLOY_AUTOHEAL_DRYRUN=0\n'
        f'AADS_RELEASE_SHA=abc1234\nexport AADS_DEPLOY_AUTOHEAL_STATE_DIR="{tmp_path / "autoheal"}"\n'
    )
    out = _call('launch_autoheal_worker target_drain_busy || echo LAUNCH_FAILED', env_prefix=env)
    assert "LAUNCH_FAILED" in out
    assert "queue empty" in out
    assert "재개로 인정하지 않는다" in out


def test_queue_empty_worker_success_does_not_supersede_ledger(tmp_path):
    """전체 경로: DB 상 후속을 확인했어도 워커가 실제로는 아무것도 못 집으면(queue empty) 원장은 그대로다.

    큐 등록과 워커 기동 사이의 경합(배치 병합 등)으로 확인했던 후속 run 이
    사라질 수 있다. queue_autoheal_retry_request 의 사전 확인만 믿지 않고
    워커의 실제 출력까지 확인해야 한다.
    """
    sql_log = tmp_path / "sql.log"
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    launcher = scripts_dir / "start_aads_deploy_queue_worker.sh"
    launcher.write_text("#!/bin/bash\necho 'deploy queue empty'\nexit 0\n")
    launcher.chmod(0o755)
    env = (
        f'export AADS_DEPLOY_AUTOHEAL_STATE_DIR="{tmp_path / "autoheal"}"\n'
        'export AADS_RELEASE_SHA=abc1234\nexport DEPLOY_RUN_ID=4242\n'
        'export AADS_DEPLOY_AUTOHEAL_DRYRUN=0\nexport AADS_DEPLOY_AUTOHEAL_COOLDOWN_SEC=0\n'
        'export DEPLOY_CURRENT_PHASE=target_slot_drain\n'
        'export DEPLOY_LAST_FAIL_ERROR="target slot active streams=1"\n'
        'export AADS_DEPLOY_AUTOHEAL_DRAIN_WAIT=0\n'
        f'COMPOSE_DIR="{tmp_path}"\nSTATE_DIR="{tmp_path}"\n'
        'audit_control() { :; }\n'
        'sql_escape() { echo "$1"; }\n'
        'deploy_db_available() { return 0; }\n'
        f'deploy_db_exec() {{ printf "%s\\n" "$1" >> "{sql_log}"; echo 4243; }}\n'
    )
    out = _call("deploy_autoheal_on_exit", "1", env_prefix=env)
    assert "queue empty" in out
    assert "재개 워커 기동 실패" in out
    sql = sql_log.read_text()
    assert "superseded_by_autoheal_drain_retry" not in sql
    assert "autoheal escalated" in sql


def test_cooldown_is_scoped_to_release_and_cause(tmp_path):
    """쿨다운은 릴리스 SHA·원인 조합별로 격리된다 — 다른 조합은 서로 막지 않는다."""
    env = f'export AADS_DEPLOY_AUTOHEAL_STATE_DIR="{tmp_path}"\n'
    out = _call(
        'AADS_RELEASE_SHA=abc1234; autoheal_cooldown_stamp target_drain_busy; '
        'autoheal_cooldown_ok target_drain_busy || echo SAME_BLOCKED; '
        'autoheal_cooldown_ok dirty_worktree && echo OTHER_CAUSE_OK; '
        'AADS_RELEASE_SHA=def5678; autoheal_cooldown_ok target_drain_busy && echo OTHER_SHA_OK',
        env_prefix=env,
    )
    assert "SAME_BLOCKED" in out
    assert "OTHER_CAUSE_OK" in out
    assert "OTHER_SHA_OK" in out
    assert (tmp_path / "abc1234.target_drain_busy.last_launch_epoch").exists()


def test_autoheal_key_rejects_unsafe_filename_inputs(tmp_path):
    """SHA/원인이 파일명·SQL 로 그대로 쓰이므로 경로 이탈 문자는 거부해야 한다."""
    env = f'export AADS_DEPLOY_AUTOHEAL_STATE_DIR="{tmp_path}"\n'
    out = _call(
        'AADS_RELEASE_SHA="../escape"; autoheal_cooldown_stamp target_drain_busy || echo BAD_SHA; '
        'AADS_RELEASE_SHA=abc1234; autoheal_cooldown_stamp "../cause" || echo BAD_CAUSE; '
        'autoheal_cooldown_stamp target_drain_busy && echo SAFE',
        env_prefix=env,
    )
    assert all(item in out for item in ("BAD_SHA", "BAD_CAUSE", "SAFE"))
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "abc1234.target_drain_busy.last_launch_epoch"
    ]


def test_invalid_release_key_escalates_instead_of_retrying(tmp_path):
    """AADS_RELEASE_SHA 가 유효하지 않으면(예: unknown) 재시도 대신 즉시 에스컬레이션한다."""
    state_dir = tmp_path / "autoheal"
    state_dir.mkdir()
    env_prefix = (
        f'export AADS_DEPLOY_AUTOHEAL_STATE_DIR="{state_dir}"\n'
        'export AADS_RELEASE_SHA="unknown"\n'
        'export AADS_DEPLOY_AUTOHEAL_DRYRUN=1\n'
        'export DEPLOY_CURRENT_PHASE="preflight"\n'
        'export DEPLOY_LAST_FAIL_ERROR="insufficient build disk"\n'
        'audit_control() { :; }\n'
        'deploy_db_available() { return 1; }\n'
    )
    out = _call("deploy_autoheal_on_exit", "1", env_prefix=env_prefix)
    assert "유효하지 않은 릴리스 SHA 또는 원인 키" in out
    assert "재개 기동 완료" not in out


# ── 원인별 재시도 예산 (2026-09-21 #4988/#4990 회귀) ──────────────────────────
# #4988(19:57 KST) 이 target_drain_busy 로 재시도를 띄웠는데 120초 뒤 #4990(20:08)
# 에서 스트림이 아직 3건이었고, 전역 예산 1/1 이 소진돼 사람에게 넘어갔다.
# 기다리는 것이 교정인 원인은 한 번으로 끝나지 않는다.
def test_drain_busy_has_larger_retry_budget_than_default():
    assert _call("autoheal_max_attempts", "target_drain_busy") == "5"
    assert _call("autoheal_max_attempts", "disk_full") == "1"
    assert _call("autoheal_max_attempts", "unknown") == "1"


def test_explicit_global_budget_overrides_per_cause_budget():
    """긴급 차단 경로 보존 — 운영자가 전역 예산을 명시하면 그것이 이긴다."""
    env = "export AADS_DEPLOY_AUTOHEAL_MAX_ATTEMPTS=1\n"
    assert _call("autoheal_max_attempts", "target_drain_busy", env_prefix=env) == "1"


def test_drain_budget_is_env_tunable_and_rejects_garbage():
    good = "export AADS_DEPLOY_AUTOHEAL_DRAIN_MAX_ATTEMPTS=3\n"
    assert _call("autoheal_max_attempts", "target_drain_busy", env_prefix=good) == "3"
    junk = "export AADS_DEPLOY_AUTOHEAL_DRAIN_MAX_ATTEMPTS=many\n"
    assert _call("autoheal_max_attempts", "target_drain_busy", env_prefix=junk) == "1"


def test_exit_handler_uses_per_cause_budget_not_global_constant():
    src = AUTOHEAL_LIB.read_text(encoding="utf-8")
    assert 'budget="$(autoheal_max_attempts "$cause")"' in src
    assert "if (( attempts >= budget )); then" in src
    # 전역 상수를 직접 비교하던 옛 경로가 남아 있으면 예산 확장이 무효가 된다.
    assert "attempts >= AUTOHEAL_MAX_ATTEMPTS" not in src


def test_drain_wait_polls_and_exits_early_without_cutting_streams():
    """고정 120초 대기는 스트림이 먼저 끝나도 배포 레인을 붙잡는다."""
    src = AUTOHEAL_LIB.read_text(encoding="utf-8")
    assert 'stream_count_for_port "$NEW_PORT"' in src
    assert "drain 조기 완료" in src
    # 스트림을 끊는 교정은 절대 들어오면 안 된다(생성 중 답변 손실).
    assert "docker kill" not in src
    assert "pkill" not in src


# ── 디스크 회수 단계적 강화 (2026-09-21 #4993/#4994 회귀) ────────────────────
# until=48h 필터가 빌드 캐시 56건 중 55건을 active 로 걸러내 363MB 만 회수됐고,
# 878MB 부족을 못 메워 배포 2건이 연속 차단됐다.
def test_disk_reclaim_escalates_through_three_tiers():
    src = AUTOHEAL_LIB.read_text(encoding="utf-8")
    assert 'until=${AUTOHEAL_BUILDER_PRUNE_UNTIL}' in src
    assert 'until=${AUTOHEAL_BUILDER_PRUNE_TIGHT}' in src
    assert "docker builder prune -af" in src
    # 각 단계는 임계 미달일 때만 다음으로 넘어가야 한다(불필요한 캐시 파기 방지).
    assert src.count("! require_build_disk_free >/dev/null 2>&1") >= 2


def test_tight_prune_window_is_narrower_than_default():
    assert _call('echo "$AUTOHEAL_BUILDER_PRUNE_UNTIL"') == "48h"
    assert _call('echo "$AUTOHEAL_BUILDER_PRUNE_TIGHT"') == "6h"
    env = "export AADS_DEPLOY_AUTOHEAL_BUILDER_PRUNE_TIGHT=12h\n"
    assert _call('echo "$AUTOHEAL_BUILDER_PRUNE_TIGHT"', env_prefix=env) == "12h"


def test_hard_prune_is_gateable_and_touches_only_build_cache():
    src = AUTOHEAL_LIB.read_text(encoding="utf-8")
    assert "AADS_DEPLOY_AUTOHEAL_DISK_HARD_PRUNE:-1" in src
    # 실행 중 서비스를 건드리는 회수는 들어오면 안 된다.
    assert "docker volume prune" not in src
    assert "docker system prune" not in src
    assert "docker container prune" not in src
    assert "docker image prune -a" not in src
