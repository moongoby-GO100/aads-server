"""자동 부여된 의존성은 부모가 죽어도 자식을 죽이지 않는다 — 계약 테스트.

배경: 2026-09-16, 잡 하나가 error 로 끝나자 뒤에 줄 서 있던 잡 4개가 연쇄로
cancelled 됐다.

    runner-168bac82(error) → runner-5a2953e2 취소 → runner-863c0791 취소
    runner-1fd9ea23        → runner-97dcd84b(error) → runner-e7597fb8 취소

네 건 모두 사람이 건 의존성이 아니라 **같은 파일을 만진다는 이유로 시스템이 자동으로
건 것**이었다(`logs` 에 `file_conflict_auto_dependency` 이벤트). 서로 다른 세션의
작업이 남의 실패에 끌려 죽었고, 그 중 둘은 CEO 가 지시한 별개 과제였다.

자동 의존성의 목적은 같은 파일을 동시에 고치지 않게 줄을 세우는 것 하나뿐이다.
부모가 terminal 이면 그 파일을 더 건드리지 않으므로 줄 설 이유가 사라진다.
그래서 **취소가 아니라 의존성 해제**가 맞다.

사람이 명시한 depends_on 은 "저게 끝나야 이게 의미가 있다" 는 뜻이므로 종전대로 취소한다.
이 구분이 사라지면 같은 연쇄 사고가 다시 난다.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ("pipeline-runner.sh", "pipeline-runner.sh.local")
AUTO_MARKER = "file_conflict_auto_dependency"


def _read(name: str) -> str:
    return (ROOT / "scripts" / name).read_text(encoding="utf-8")


def _cleanup_fn(script: str) -> str:
    start = script.index("cleanup_blocked_dependencies() {")
    end = script.index("\n}\n", start)
    return script[start:end]


def _sql_blocks(fn: str) -> dict:
    """cleanup 함수 안의 db_exec SQL 을 변수명별로 잘라 온다."""
    blocks = {}
    for var in ("released", "blocked_existing", "blocked_missing"):
        m = re.search(rf'{var}=\$\(db_exec "(.*?)" 2>/dev/null\)', fn, re.S)
        assert m, f"{var} db_exec 블록을 찾지 못했다"
        blocks[var] = m.group(1)
    return blocks


def test_release_query_exists_in_both_runner_scripts():
    for name in SCRIPTS:
        fn = _cleanup_fn(_read(name))
        assert "released=$(db_exec" in fn, f"{name}: 자동 의존성 해제 경로가 없다"
        assert "AUTODEP_RELEASED" in fn, f"{name}: 해제 로그가 없다"


def test_release_query_clears_dependency_and_never_cancels():
    """해제는 대기열을 유지해야 한다. 여기서 status 를 건드리면 취소와 같아진다."""
    blocks = _sql_blocks(_cleanup_fn(_read("pipeline-runner.sh")))
    released = blocks["released"]

    assert "SET depends_on=NULL" in released
    assert "status='cancelled'" not in released, "해제 경로가 취소를 하고 있다"
    assert "completed_at=NOW()" not in released, "해제 경로가 작업을 종결시키고 있다"
    # 부모가 terminal 일 때만 푼다 — 아직 살아 있는 부모의 줄은 유지해야 한다
    assert "dep.status IN ('error','rejected','rejected_done','cancelled')" in released
    assert "p.status='queued'" in released


def test_release_only_applies_to_auto_assigned_dependencies():
    released = _sql_blocks(_cleanup_fn(_read("pipeline-runner.sh")))["released"]
    assert AUTO_MARKER in released, "자동 의존성 판별 없이 무조건 풀고 있다"
    assert "NOT (" not in released.split(AUTO_MARKER)[0][-40:], "해제 조건이 뒤집혀 있다"


def test_cancel_queries_skip_auto_assigned_dependencies():
    """취소 경로는 사람이 명시한 의존성만 대상으로 해야 한다."""
    blocks = _sql_blocks(_cleanup_fn(_read("pipeline-runner.sh")))
    for var in ("blocked_existing", "blocked_missing"):
        sql = blocks[var]
        assert "status='cancelled'" in sql, f"{var}: 취소 경로가 아니다"
        assert AUTO_MARKER in sql, f"{var}: 자동 의존성 제외 조건이 없다"
        assert f"NOT ({AUTO_MARKER}" in sql.replace("\n", " ").replace("  ", " ") or \
            re.search(rf"NOT\s*\(\s*p\.logs[^)]*{AUTO_MARKER}", sql, re.S), \
            f"{var}: 자동 의존성을 제외(NOT)하지 않는다"


def test_auto_dependency_marker_matches_what_the_submit_api_writes():
    """판별 문자열이 API 가 실제로 쓰는 이벤트명과 같아야 한다.

    한쪽만 바뀌면 해제가 조용히 멈추고 연쇄 취소가 되돌아온다.
    """
    api = (ROOT / "app" / "api" / "pipeline_runner.py").read_text(encoding="utf-8")
    assert f"'event', '{AUTO_MARKER}'" in api, "submit API 가 쓰는 이벤트명이 바뀌었다"
    assert AUTO_MARKER in _read("pipeline-runner.sh")


def test_runner_scripts_stay_byte_identical():
    assert _read("pipeline-runner.sh") == _read("pipeline-runner.sh.local")


def test_api_failure_cascade_requeues_file_lock_child_instead_of_cancelling_it():
    """API completion path must match the shell sweeper's auto-dependency rule."""
    api = (ROOT / "app" / "api" / "pipeline_runner.py").read_text(encoding="utf-8")
    cascade_start = api.index("async def _cascade_cleanup_orphans_with_ids")
    cascade_end = api.index("\n\nasync def promote_next_queued", cascade_start)
    cascade = api[cascade_start:cascade_end]

    assert "_has_auto_file_dependency" in cascade
    assert "_release_auto_file_dependency" in cascade
    assert "depends_on = NULL" in api
    assert "file_conflict_dependency_requeued" in api
    assert "logs @> '[{\"event\": \"file_conflict_dependency_requeued\"}]'::jsonb" in api
    # Only the explicit-dependency branch may enter the cancellation helper.
    assert cascade.index("_has_auto_file_dependency") < cascade.index("_cancel_explicit_orphan")


def test_explicit_dependency_still_cancels_with_parent_failure_context_and_alert():
    api = (ROOT / "app" / "api" / "pipeline_runner.py").read_text(encoding="utf-8")
    helper_start = api.index("async def _cancel_explicit_orphan")
    helper_end = api.index("\n\nasync def _cascade_cleanup_orphans", helper_start)
    helper = api[helper_start:helper_end]

    assert "status = 'cancelled'" in helper
    assert "orphaned_dependency: parent {parent_id} {parent_status}" in helper
    assert "parent_failure_reason" in helper
    assert "orphaned_dependency_alert" in helper
    assert "pg_notify('pipeline_orphaned_dependency'" in helper


def test_p0_instruction_is_promoted_ahead_of_implicit_file_lock_queue():
    """Keep the existing numeric priority order, with directive P0 as its top tier."""
    for name in SCRIPTS:
        script = _read(name)
        claim_start = script.index("_claim_queued_job()")
        claim_end = script.index("\n}\n", claim_start)
        claim = script[claim_start:claim_end]
        assert "PRIORITY:[[:space:]]*P0" in claim
        assert claim.index("PRIORITY:[[:space:]]*P0") < claim.index("COALESCE(p.priority, 0) DESC")
