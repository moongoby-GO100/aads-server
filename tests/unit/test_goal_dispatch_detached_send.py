"""담당 한 명의 긴 응답이 뒤 마일스톤을 통째로 자르면 안 된다.

2026-09-17 실측. `dispatch_pending_milestones` 는 `LIMIT 20` 으로 뽑은
마일스톤을 순차 for 루프로 돌면서 각 건마다 `send_message_stream` 을
`async for` 로 **끝까지 소비**했다. 그런데 이 코루틴은 상위에서
`asyncio.wait_for(..., 120초)` 로 감싸여 돈다(main.py:666). 첫 담당의 LLM
응답이 120초를 넘기면 루프 전체가 `CancelledError` 로 끊기고 뒤에 남은
마일스톤은 그 사이클에 **한 건도** 못 나갔다 —
`goal_dispatch_cancelled` 과 `goal_dispatch_timeout` 이 같이 찍혔다.

그래서 스트림 소비를 사이클에서 떼어내 태스크로 띄운다. 떼어내면 상한이
사라지므로 두 가지를 같이 건다: 태스크 자체 시간 상한
(`AADS_GOAL_DISPATCH_SEND_TIMEOUT_SECONDS`, R-BG "시간 상한을 건다") 과
동시 실행 상한(`AADS_GOAL_DISPATCH_CONCURRENCY`) — 한 사이클 20건이
동시에 LLM 을 때리면 안 된다.
"""
from __future__ import annotations

import asyncio
import inspect

import pytest

from app.services import goal_dispatch


class _Conn:
    """마일스톤 조회만 진짜처럼 굴고 나머지는 삼킨다."""

    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    async def execute(self, *_args, **_kwargs) -> str:
        return "INSERT 0 0"

    async def fetch(self, *_args, **_kwargs) -> list[dict]:
        return self.rows

    async def fetchval(self, *_args, **_kwargs):
        return False


class _Pool:
    def __init__(self, conn: _Conn) -> None:
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *_exc):
                return False

        return _Ctx()


def _row(n: int) -> dict:
    return {
        "milestone_id": f"{n:08d}-0000-0000-0000-000000000000",
        "milestone_title": f"M{n}",
        "description": "설명",
        "completion_criteria": "기준",
        "dispatch_count": 0,
        "dispatched_at": None,
        "load_deferred_since": None,
        "goal_title": "목표",
        "project": "AADS",
        "goal_id": "g",
        "owner_role_key": f"role{n}",
        "goal_lead_session_id": "lead",
        "session_id": f"session-{n}",
    }


@pytest.fixture
def harness(monkeypatch):
    """게이트는 전부 통과시키고 발송만 관찰한다."""
    from app.core import db_pool
    from app.services import chat_service, orchestration_limits

    async def _open(*_a, **_k):
        return (False, "")

    async def _ok(*_a, **_k):
        return (True, "")

    monkeypatch.setattr(orchestration_limits, "owner_paused", _open)
    monkeypatch.setattr(orchestration_limits, "cost_gate", _ok)
    monkeypatch.setattr(orchestration_limits, "load_gate", _ok)
    # 한 사이클 발송 상한이 기본 2 라 3건을 볼 수 없다.
    monkeypatch.setattr(goal_dispatch, "_MAX_PER_CYCLE", 10)
    monkeypatch.setattr(goal_dispatch, "_ENABLED", True)

    def _install(rows, stream):
        monkeypatch.setattr(db_pool, "get_pool", lambda: _Pool(_Conn(rows)))
        monkeypatch.setattr(chat_service, "send_message_stream", stream)

    return _install


def test_a_slow_owner_does_not_block_the_milestones_behind_it(harness) -> None:
    """느린 담당이 1건 있어도 뒤의 마일스톤이 **같은 호출에서** 나간다."""
    started: list[str] = []
    release = asyncio.Event()

    async def _stream(*, session_id: str, **_kwargs):
        started.append(session_id)
        if session_id == "session-1":
            # 사이클 상한(120초)보다 오래 걸리는 담당.
            await release.wait()
        return
        yield  # pragma: no cover - 제너레이터로 만들기 위한 것

    async def _run() -> dict[str, int]:
        harness([_row(1), _row(2), _row(3)], _stream)
        # 사이클 자체는 상한 안에서 끝나야 한다 — 태스크만 띄우므로.
        result = await asyncio.wait_for(
            goal_dispatch.dispatch_pending_milestones(None), 5,
        )
        # 띄운 태스크가 스트림에 들어갈 틈을 준다.
        for _ in range(20):
            await asyncio.sleep(0)
        release.set()
        await asyncio.gather(*list(goal_dispatch._send_tasks))
        return result

    result = asyncio.run(_run())

    assert result["sent"] == 3, "발송 착수 수가 3건이 아니다"
    assert started == ["session-1", "session-2", "session-3"], (
        f"느린 담당이 뒤를 막았다 — 실제로 시작된 것: {started}"
    )


def test_concurrency_never_exceeds_the_limit(harness) -> None:
    """한 사이클 20건이 동시에 LLM 을 때리면 안 된다."""
    live = 0
    peak = 0
    release = asyncio.Event()

    async def _stream(**_kwargs):
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        try:
            await release.wait()
        finally:
            live -= 1
        return
        yield  # pragma: no cover

    async def _run() -> None:
        harness([_row(n) for n in range(1, 8)], _stream)
        await goal_dispatch.dispatch_pending_milestones(None)
        for _ in range(50):
            await asyncio.sleep(0)
        release.set()
        await asyncio.gather(*list(goal_dispatch._send_tasks))

    asyncio.run(_run())

    assert peak <= goal_dispatch._SEND_CONCURRENCY, (
        f"동시 실행 {peak} 건 — 상한 {goal_dispatch._SEND_CONCURRENCY} 초과"
    )
    assert peak > 1, "세마포어가 직렬화해 버리면 태스크로 뗀 의미가 없다"


def test_a_stuck_send_gives_up_at_its_own_limit(harness, monkeypatch) -> None:
    """상한 없는 백그라운드는 만들지 않는다 (R-BG)."""
    monkeypatch.setattr(goal_dispatch, "_SEND_TIMEOUT", 0.05)
    never = asyncio.Event()

    async def _stream(**_kwargs):
        await never.wait()
        return
        yield  # pragma: no cover

    async def _run() -> None:
        harness([_row(1)], _stream)
        await goal_dispatch.dispatch_pending_milestones(None)
        tasks = list(goal_dispatch._send_tasks)
        assert tasks, "태스크가 안 떴다"
        # 상한에 걸려 **스스로** 끝나야 한다 — 밖에서 끊지 않는다.
        await asyncio.wait_for(asyncio.gather(*tasks), 3)

    asyncio.run(_run())


def test_tasks_are_held_by_a_strong_reference() -> None:
    """참조를 안 들면 GC 가 실행 중인 태스크를 거둬간다."""
    src = inspect.getsource(goal_dispatch._spawn_send)

    assert "_send_tasks.add(task)" in src
    assert "add_done_callback(_send_tasks.discard)" in src, (
        "끝난 태스크를 안 지우면 set 이 무한히 자란다"
    )


def test_the_timeout_leaves_a_log() -> None:
    src = inspect.getsource(goal_dispatch._send_milestone)

    assert "goal_dispatch_send_timeout" in src
    assert "asyncio.wait_for(" in src
    # 세마포어 대기까지 상한 안에 들어가야 상한 없는 대기가 안 생긴다.
    assert src.index("asyncio.wait_for(") > src.index("_send_semaphore()")


def test_limits_come_from_the_documented_env_names() -> None:
    src = inspect.getsource(goal_dispatch)

    assert "AADS_GOAL_DISPATCH_SEND_TIMEOUT_SECONDS" in src
    assert "AADS_GOAL_DISPATCH_CONCURRENCY" in src
