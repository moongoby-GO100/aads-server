"""골 비용 게이트가 **오케스트레이션 비용만** 세는지 지킨다.

2026-09-17 실측. GO100 의 활성 목표 두 개가 비용 상한에 걸려 마일스톤
지시가 통째로 멈춰 있었다. 사이클은 60초마다 정상으로 돌았고 로그에는
`goal_dispatch_cost_gated` 만 쌓였다.

    #119  123.85 / 20.00 USD   (이 중 system_trigger 몫은 8.41)
    #310   85.54 / 20.00 USD   (이 중 system_trigger 몫은 2.72)

원인은 `refresh_goal_cost` 가 담당 세션의 **모든** 대화를 목표 비용으로
셌던 것이다. 담당 세션은 대표님이 직접 쓰시는 창이라 첫 지시 뒤에도
대표님 대화가 계속 쌓인다. 누적값은 리셋되지 않으므로 한 번 상한에 닿으면
게이트는 **영영 닫힌 채로** 남는다.

여기서 두 가지를 묶어 둔다. 한쪽만 지키면 조용히 다시 깨진다.

1. 비용 합산이 `intent` 로 오케스트레이션 턴만 고른다.
2. 지시를 보내는 쪽 세 곳이 실제로 그 `intent` 를 붙인다.

2번이 깨지면 비용이 0 으로 세어져 상한이 **한 번도 안 걸린다** — 반대
방향 사고다. 그래서 같은 파일에서 함께 본다.
"""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]

LIMITS = ROOT / "app" / "services" / "orchestration_limits.py"

# 오케스트레이션이 담당에게 말을 거는 경로 전부.
SENDERS = (
    ROOT / "app" / "services" / "goal_dispatch.py",
    ROOT / "app" / "services" / "milestone_review.py",
    ROOT / "app" / "services" / "goal_intervene.py",
)


def test_cost_meter_counts_only_orchestration_turns() -> None:
    source = LIMITS.read_text(encoding="utf-8")

    assert "_ORCH_INTENTS" in source, "오케스트레이션 intent 목록이 없다"
    assert 'os.getenv("GOAL_COST_INTENTS", "system_trigger")' in source

    # 합산 쿼리가 실제로 그 목록으로 거른다. 상수만 있고 안 쓰면 의미가 없다.
    assert "AND m.intent = ANY($3::text[])" in source
    assert "goal_id, started, _ORCH_INTENTS," in source


def test_cost_meter_keeps_first_dispatch_baseline() -> None:
    """첫 지시 이전은 대표님 작업이다 — 이 기준선을 잃으면 안 된다."""
    source = LIMITS.read_text(encoding="utf-8")

    assert "SELECT MIN(dispatched_at) FROM milestones WHERE goal_id = $1::uuid" in source
    assert "if started is None:" in source


def test_cost_gate_still_blocks_and_warns() -> None:
    """게이트 자체가 사라지지는 않았는가. 좁힌 것은 세는 대상뿐이다."""
    source = LIMITS.read_text(encoding="utf-8")

    assert "async def cost_gate(" in source
    assert "if spent >= lim:" in source
    assert "spent >= lim * 0.8" in source


def test_every_dispatch_path_tags_the_intent_the_meter_counts() -> None:
    """보내는 쪽이 표시를 안 붙이면 비용이 0 으로 세어진다."""
    for path in SENDERS:
        source = path.read_text(encoding="utf-8")
        assert 'intent_override="system_trigger"' in source, (
            f"{path.name} 이 system_trigger 를 붙이지 않는다 — "
            "골 비용이 0 으로 집계되어 상한이 한 번도 걸리지 않는다"
        )
