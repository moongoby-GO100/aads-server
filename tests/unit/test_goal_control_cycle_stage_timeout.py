"""골 제어 사이클이 한 단계에 갇히지 않는지 지킨다.

2026-09-17 실측. 23:49:21(UTC) 에 시작한 사이클이 담당 세션으로 지시를
넣는 `send_message_stream` 에서 돌아오지 않았다. 이 잡은 `max_instances=1`
이라 이후 5분 넘게 매 분 이것만 남았다:

    Execution of job ... skipped: maximum number of running instances reached (1)

멈춘 것은 그 담당 하나가 아니라 **전 프로젝트의 목표 진행 전체**였다.
그 사이 마일스톤 하나가 `confirm` 으로 completed 가 됐는데도
`goals.progress` 는 0.0 인 채였고 다음 마일스톤도 열리지 않았다 — 그
갱신이 이 사이클 안의 `advance_active_goals` 에서만 일어나기 때문이다.

바깥(LLM 스트림)을 기다리는 단계는 셋이다. 셋 다 상한이 있어야 한다.
하나라도 맨몸으로 `await` 하면 같은 사고가 그 경로로 다시 난다.
"""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]

MAIN = ROOT / "app" / "main.py"

# 바깥을 기다리는 단계와, 상한에 걸렸을 때 남겨야 할 표시.
OUTBOUND_STAGES = (
    ("dispatch_pending_milestones(None)", "goal_dispatch_timeout"),
    ("ask_pending_reviews(None)", "milestone_review_timeout"),
    ("report_goal_events(None)", "goal_report_timeout"),
)


def test_cycle_defines_a_configurable_stage_timeout() -> None:
    source = MAIN.read_text(encoding="utf-8")

    assert "_GOAL_STAGE_TIMEOUT" in source
    assert 'os.getenv("AADS_GOAL_CONTROL_STAGE_TIMEOUT_SECONDS", "120")' in source


def test_every_outbound_stage_is_bounded_and_leaves_a_trace() -> None:
    source = MAIN.read_text(encoding="utf-8")

    for call, event in OUTBOUND_STAGES:
        assert f"asyncio.wait_for(\n                        {call}" in source, (
            f"{call} 이 시간 상한 없이 await 된다 — 이 단계가 멎으면 "
            "사이클 전체가 멎고 목표 진행이 멈춘다"
        )
        assert event in source, f"{call} 의 상한 초과가 조용히 넘어간다 ({event} 없음)"


def test_timeout_is_caught_before_the_generic_handler() -> None:
    """`TimeoutError` 는 `Exception` 의 하위다 — 순서가 뒤집히면 안 잡힌다."""
    source = MAIN.read_text(encoding="utf-8")

    for _call, event in OUTBOUND_STAGES:
        timeout_at = source.index(f'logger.warning("{event}"')
        # 같은 try 블록의 포괄 except 는 상한 처리보다 **뒤**에 와야 한다.
        generic_at = source.index("except Exception", timeout_at)
        assert timeout_at < generic_at, f"{event} 가 포괄 except 뒤에 있다"


def test_scheduler_still_refuses_overlapping_cycles() -> None:
    """상한을 걸었다고 중복 실행을 허용하면 안 된다 — 같은 목표를 두 번 민다."""
    source = MAIN.read_text(encoding="utf-8")

    assert 'id="goal_control_cycle"' in source
    assert "max_instances=1" in source
    assert "pg_try_advisory_lock(hashtext('aads_goal_control_cycle'))" in source
