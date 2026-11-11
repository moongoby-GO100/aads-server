"""AADS-REVIEW-PRESERVATION-NESTED-LOCAL-DEF-20261112

함수 본문 안의 지역 def/class 삭제를 public 심볼 삭제로 판정하지 않는 예외 고정.

GO100 runner-aafa0d97(2026-11-12) 실제 사례:
tests/go100/test_memory_wave_priority_rotation.py 의
def test_live_main_heartbeats_and_ack_checkpoint_with_long_interval(...) 본문 안의
["def slow_record", "class FixedHost", "def __init__", "def _compute", "def sleep"] 삭제가
PRESERVATION_HARD_GATE (FLAG 0.3) 로 오판되어 차단됨.
지역 def/class 는 외부에서 import 하거나 호출할 수 없으므로 public 계약이 아니다.
"""

import app.services.code_reviewer as cr


def _diff(path: str, body: str) -> str:
    return (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        f"{body}"
    )


def test_runner_aafa0d97_nested_local_symbols_are_exempt_when_test_rewritten():
    """실제 사례(runner-aafa0d97 축약): 최상위 test 함수 유지 + 본문의 지역 def/class/메서드 삭제.

    deleted_symbols 는 0개여야 하며, 5개 심볼 모두 preservation_exempted_local_symbols 에 기록되어야 한다.
    """
    path = "tests/go100/test_memory_wave_priority_rotation.py"
    body = (
        "@@ -10,35 +10,35 @@\n"
        "-def test_live_main_heartbeats_and_ack_checkpoint_with_long_interval(old_param):\n"
        "+def test_live_main_heartbeats_and_ack_checkpoint_with_long_interval(new_param):\n"
        "-    def slow_record(item):\n"
        "-        return item\n"
        "-    class FixedHost:\n"
        "-        def __init__(self):\n"
        "-            self.state = 0\n"
        "-        def _compute(self):\n"
        "-            return 42\n"
        "-        def sleep(self):\n"
        "-            pass\n"
        "+    # rewritten test body without local helpers\n"
        "+    result = new_param.execute()\n"
        "+    assert result.status == 'ok'\n"
        "+    assert result.heartbeats > 0\n"
        "+    assert result.checkpoint_saved is True\n"
        "+    assert result.interval_ack is True\n"
        "+    assert result.rotation_count >= 1\n"
        "+    assert result.latency_ms < 100\n"
        "+    assert result.error is None\n"
        "+    assert result.is_active is True\n"
        "+    assert result.priority == 'high'\n"
    )
    diff = _diff(path, body)

    notes: dict = {}
    removed = cr._removed_preservation_symbols(diff, notes_out=notes)
    assert removed == []

    expected_exempted = [
        "def slow_record",
        "class FixedHost",
        "def __init__",
        "def _compute",
        "def sleep",
    ]
    assert notes.get("preservation_exempted_local_symbols") == expected_exempted

    gate_out: dict = {}
    verdict = cr._precheck_preservation_gate(diff, "", None, gate_out)
    assert verdict is None
    assert gate_out.get("preservation_exempted_local_symbols") == expected_exempted


def test_nested_local_symbols_exempt_via_hunk_header_context():
    """hunk 헤더만으로 함수 스코프가 드러나는 경우(본문 첫 줄이 hunk 밖) 동일하게 면제."""
    path = "tests/unit/test_worker_engine.py"
    body = (
        "@@ -40,15 +40,20 @@ def test_live_main_heartbeats_and_ack_checkpoint_with_long_interval():\n"
        "-    def slow_record():\n"
        "-        return None\n"
        "-    class FixedHost:\n"
        "-        def __init__(self):\n"
        "-            pass\n"
        "-        def sleep(self):\n"
        "-            pass\n"
        "+    # rewritten body\n"
        "+    res = run_check()\n"
        "+    assert res.ok\n"
        "+    assert res.score == 100\n"
        "+    assert res.duration < 10\n"
        "+    assert res.passed\n"
        "+    assert res.retries == 0\n"
        "+    assert res.completed\n"
    )
    diff = _diff(path, body)

    notes: dict = {}
    removed = cr._removed_preservation_symbols(diff, notes_out=notes)
    assert removed == []
    assert notes.get("preservation_exempted_local_symbols") == [
        "def slow_record",
        "class FixedHost",
        "def __init__",
        "def sleep",
    ]

    gate_out: dict = {}
    verdict = cr._precheck_preservation_gate(diff, "", None, gate_out)
    assert verdict is None
    assert gate_out.get("preservation_exempted_local_symbols") == [
        "def slow_record",
        "class FixedHost",
        "def __init__",
        "def sleep",
    ]


def test_nested_local_symbols_exempt_in_async_func_and_production_code():
    """운영 코드·비동기 함수에서도 동일하게 지역 def/class 삭제는 면제된다."""
    path = "app/services/pipeline_runner.py"
    body = (
        "@@ -100,12 +100,3 @@ async def execute_pipeline_step(step_id: str):\n"
        "-    async def _local_runner():\n"
        "-        return True\n"
        "-    class _LocalConfig:\n"
        "-        pass\n"
        "+    pass\n"
    )
    diff = _diff(path, body)

    notes: dict = {}
    removed = cr._removed_preservation_symbols(diff, notes_out=notes)
    assert removed == []
    assert notes.get("preservation_exempted_local_symbols") == [
        "async def _local_runner",
        "class _LocalConfig",
    ]


def test_nested_helper_in_class_method_is_exempt():
    """최상위 클래스의 메서드 본문 안의 지역 함수 삭제도 면제된다."""
    path = "app/services/executor.py"
    body = (
        "@@ -10,12 +10,4 @@\n"
        " class TaskExecutor:\n"
        "     def run(self):\n"
        "-        def step_helper():\n"
        "-            return 1\n"
        "+        pass\n"
    )
    diff = _diff(path, body)

    notes: dict = {}
    removed = cr._removed_preservation_symbols(diff, notes_out=notes)
    assert removed == []
    assert notes.get("preservation_exempted_local_symbols") == ["def step_helper"]


def test_regression_top_level_public_def_deletion_stays_flagged():
    """회귀: 모듈 최상위 public def 삭제는 계속 FLAG."""
    path = "app/services/worker.py"
    body = (
        "@@ -1,5 +1,1 @@\n"
        "-def process_incoming_event(event: dict) -> bool:\n"
        "-    return True\n"
        "+x = 1\n"
    )
    diff = _diff(path, body)

    removed = cr._removed_preservation_symbols(diff)
    assert "def process_incoming_event" in removed

    verdict = cr._precheck_preservation_gate(diff, "", None)
    assert verdict is not None
    assert verdict.flag_category == "PRESERVATION_HARD_GATE"
    assert "def process_incoming_event" in verdict.feedback["deleted_symbols"]


def test_regression_top_level_class_public_method_deletion_stays_flagged():
    """회귀: 최상위 class 의 public 메서드 삭제는 계속 FLAG."""
    path = "app/services/worker.py"
    body = (
        "@@ -1,6 +1,2 @@\n"
        " class WorkerService:\n"
        "-    def execute_job(self, job_id: str) -> None:\n"
        "-        pass\n"
        "+    pass\n"
    )
    diff = _diff(path, body)

    removed = cr._removed_preservation_symbols(diff)
    assert "def execute_job" in removed

    verdict = cr._precheck_preservation_gate(diff, "", None)
    assert verdict is not None
    assert verdict.flag_category == "PRESERVATION_HARD_GATE"
    assert "def execute_job" in verdict.feedback["deleted_symbols"]


def test_regression_top_level_class_method_with_hunk_header_stays_flagged():
    """회귀: hunk 헤더가 최상위 class 일 때 public 메서드 삭제는 계속 FLAG."""
    path = "app/services/worker.py"
    body = (
        "@@ -10,5 +10,1 @@ class WorkerService:\n"
        "-    def execute_job(self, job_id: str) -> None:\n"
        "-        pass\n"
        "+    pass\n"
    )
    diff = _diff(path, body)

    removed = cr._removed_preservation_symbols(diff)
    assert "def execute_job" in removed

    verdict = cr._precheck_preservation_gate(diff, "", None)
    assert verdict is not None
    assert verdict.flag_category == "PRESERVATION_HARD_GATE"
    assert "def execute_job" in verdict.feedback["deleted_symbols"]


def test_regression_top_level_class_deletion_stays_flagged():
    """회귀: 모듈 최상위 class 삭제는 계속 FLAG."""
    path = "app/services/models.py"
    body = (
        "@@ -1,4 +1,1 @@\n"
        "-class MemoryProfile:\n"
        "-    pass\n"
        "+# deleted\n"
    )
    diff = _diff(path, body)

    removed = cr._removed_preservation_symbols(diff)
    assert "class MemoryProfile" in removed

    verdict = cr._precheck_preservation_gate(diff, "", None)
    assert verdict is not None
    assert "class MemoryProfile" in verdict.feedback["deleted_symbols"]


def test_regression_router_deletion_stays_flagged():
    """회귀: @router 삭제는 계속 FLAG."""
    path = "app/api/endpoints.py"
    body = (
        "@@ -1,4 +1,1 @@\n"
        "-@router.get('/health')\n"
        "-def health():\n"
        "-    return {'ok': True}\n"
        "+# removed\n"
    )
    diff = _diff(path, body)

    removed = cr._removed_preservation_symbols(diff)
    assert "@router.get" in removed

    verdict = cr._precheck_preservation_gate(diff, "", None)
    assert verdict is not None
    assert "@router.get" in verdict.feedback["deleted_symbols"]
