"""최근 비용 창과 경고 재설정의 동작을 검증한다."""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from app.core import db_pool
from app.services import orchestration_limits as limits


@pytest.mark.asyncio
@pytest.mark.parametrize("age, days", [(30, 7), (2, 2)])
async def test_cost_window_respects_first_dispatch(monkeypatch, age, days):
    now = datetime.now(timezone.utc)
    started = now - timedelta(days=age)
    pool = AsyncMock()
    pool.fetchval.side_effect = [started, 12]
    monkeypatch.setattr(db_pool, "get_pool", lambda: pool)
    monkeypatch.setattr(limits, "_COST_WINDOW_DAYS", 7)

    assert await limits.refresh_goal_cost("goal") == 12
    actual = pool.fetchval.await_args_list[1].args[2]
    assert abs((actual - (now - timedelta(days=days))).total_seconds()) < 60
    if age == 2:
        assert actual == started


@pytest.mark.asyncio
async def test_no_dispatch_skips_messages(monkeypatch):
    pool = AsyncMock()
    pool.fetchval.return_value = None
    monkeypatch.setattr(db_pool, "get_pool", lambda: pool)

    assert await limits.refresh_goal_cost("goal") == 0
    assert pool.fetchval.await_count == 1
    assert pool.execute.await_args.args[2] == 0


@pytest.mark.asyncio
async def test_zero_window_uses_cumulative_cost(monkeypatch):
    started = datetime.now(timezone.utc) - timedelta(days=30)
    pool = AsyncMock()
    pool.fetchval.side_effect = [started, 1]
    monkeypatch.setattr(db_pool, "get_pool", lambda: pool)
    monkeypatch.setattr(limits, "_COST_WINDOW_DAYS", 0)

    await limits.refresh_goal_cost("goal")
    assert pool.fetchval.await_args_list[1].args[2] == started


@pytest.mark.asyncio
@pytest.mark.parametrize("spent, allowed", [(25, False), (10, True)])
async def test_cost_gate(monkeypatch, spent, allowed):
    pool = AsyncMock()
    pool.fetchrow.return_value = {"lim": 20, "cost_warned_at": None}
    monkeypatch.setattr(db_pool, "get_pool", lambda: pool)
    monkeypatch.setattr(limits, "_COST_WINDOW_DAYS", 7)
    monkeypatch.setattr(limits, "refresh_goal_cost", AsyncMock(return_value=spent))

    result = await limits.cost_gate("goal")
    assert result[0] is allowed
    assert "최근 7일" in result[1] if not allowed else result[1] == ""


@pytest.mark.asyncio
async def test_cost_gate_resets_warning_below_threshold(monkeypatch):
    pool = AsyncMock()
    pool.fetchrow.return_value = {"lim": 20, "cost_warned_at": datetime.now(timezone.utc)}
    monkeypatch.setattr(db_pool, "get_pool", lambda: pool)
    monkeypatch.setattr(limits, "refresh_goal_cost", AsyncMock(return_value=10))

    assert await limits.cost_gate("goal") == (True, "")
    pool.execute.assert_awaited_once_with(
        "UPDATE goals SET cost_warned_at = NULL WHERE id = $1::uuid", "goal"
    )
