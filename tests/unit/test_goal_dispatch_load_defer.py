"""부하로 미루는 데에는 상한이 있어야 한다 — 미루기와 정지는 다르다.

2026-09-17 실측. contabo14 는 장중(09:00~15:30)에 매매 엔진 둘과
postgres·러너가 같이 돌아 부하가 3.6~4.2배로 유지된다. 그런데
`orchestration_limits._MAX_LOAD_RATIO` 는 2.0 이다. 그래서 GO100 은
09:09 KST 마지막 발송 이후 사이클마다 19건이 통째로
`goal_dispatch_load_gated` 로 밀렸다 — `sent=0, skipped=19` 가 60초마다
반복됐고, 장이 열려 있는 동안 목표 오케스트레이션이 통째로 멎었다.

같은 결함을 그날 오전에 비용 게이트에서도 봤다. 누적 비용이 상한에 닿으면
다시는 열리지 않아 목표 둘이 영구히 잠겼다. **게이트에는 빠져나갈 문이
있어야 한다.** 부하 게이트의 문이 여기서 지키는 상한이다.

밀린 시각은 DB 에 남긴다. 메모리에 두면 배포·재기동마다 0 으로 돌아가고,
하루에 몇 번씩 배포하는 서버에서는 상한이 영원히 오지 않는다.
"""
from __future__ import annotations

import inspect
from pathlib import Path

from app.services import goal_dispatch

ROOT = Path(__file__).resolve().parents[2]
SRC = (ROOT / "app" / "services" / "goal_dispatch.py").read_text(encoding="utf-8")


def test_load_defer_has_an_upper_bound() -> None:
    assert hasattr(goal_dispatch, "_LOAD_DEFER_MAX_MIN"), (
        "부하 게이트에 상한이 없다 — 부하가 계속 높은 서버에서는 지시가 "
        "영원히 안 나간다"
    )
    assert goal_dispatch._LOAD_DEFER_MAX_MIN > 0


def test_gate_does_not_skip_forever() -> None:
    body = inspect.getsource(goal_dispatch.dispatch_pending_milestones)

    gated = body.index("goal_dispatch_load_gated")
    expired = body.index("goal_dispatch_load_defer_expired")

    assert gated < expired, "상한 초과 경로가 게이트 앞에 있다"
    # 상한을 넘긴 경로는 `continue` 로 빠지지 않고 발송으로 이어져야 한다.
    tail = body[expired:]
    assert "_spawn_send(" in tail, (
        "상한을 넘겼는데도 발송 경로로 이어지지 않는다"
    )


def test_defer_start_is_persisted_not_in_memory() -> None:
    src = inspect.getsource(goal_dispatch._load_defer_minutes)

    assert "UPDATE milestones SET load_deferred_since = NOW()" in src, (
        "밀리기 시작한 시각을 DB 에 남기지 않는다 — 배포마다 상한이 "
        "0 으로 돌아간다"
    )


def test_defer_is_cleared_when_load_recovers() -> None:
    body = inspect.getsource(goal_dispatch.dispatch_pending_milestones)
    clear = inspect.getsource(goal_dispatch._clear_load_defer)

    assert "_clear_load_defer(" in body, "부하가 풀려도 시계가 안 멈춘다"
    assert "load_deferred_since = NULL" in clear


def test_successful_dispatch_resets_the_clock() -> None:
    assert "dispatch_note = NULL, load_deferred_since = NULL, " in SRC, (
        "발송하고도 밀린 시각이 남으면 다음 번엔 처음부터 상한 초과로 "
        "취급된다"
    )


def test_column_is_ensured_before_select() -> None:
    body = inspect.getsource(goal_dispatch.dispatch_pending_milestones)

    assert "ADD COLUMN IF NOT EXISTS" in body
    assert body.index("ADD COLUMN IF NOT EXISTS") < body.index("rows = await conn.fetch("), (
        "칸을 만들기 전에 조회한다 — 마이그레이션이 안 돈 서버에서 터진다"
    )
