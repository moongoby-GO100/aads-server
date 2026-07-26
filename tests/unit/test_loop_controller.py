"""OHVIS Loop Controller 단위 테스트 — resolve_max_cost 검증."""
import asyncio
import pytest
from unittest.mock import AsyncMock, patch, MagicMock


@pytest.fixture(autouse=True)
def mock_db_pool(monkeypatch):
    pool = AsyncMock()
    monkeypatch.setattr("app.services.loop_controller.get_db_pool", lambda: pool)
    return pool


class TestResolveMaxCost:
    """§6.3 모델별 자동 비용 조정 테스트."""

    @pytest.mark.asyncio
    async def test_ceo_override_respected(self, mock_db_pool):
        from app.services.loop_controller import resolve_max_cost
        result = await resolve_max_cost("task", "claude-opus-5", ceo_override=10.0)
        assert result == 10.0

    @pytest.mark.asyncio
    async def test_ceo_override_capped_at_30(self, mock_db_pool):
        from app.services.loop_controller import resolve_max_cost
        result = await resolve_max_cost("task", "claude-opus-5", ceo_override=50.0)
        assert result == 30.0

    @pytest.mark.asyncio
    async def test_sonnet_returns_base_budget(self, mock_db_pool):
        from app.services.loop_controller import resolve_max_cost
        mock_db_pool.fetchrow.return_value = {"input_cost": 3.0, "output_cost": 15.0}
        result = await resolve_max_cost("task", "claude-sonnet-4-6")
        assert result == 3.0  # base budget for task

    @pytest.mark.asyncio
    async def test_opus5_returns_scaled_budget(self, mock_db_pool):
        from app.services.loop_controller import resolve_max_cost
        mock_db_pool.fetchrow.return_value = {"input_cost": 5.0, "output_cost": 25.0}
        result = await resolve_max_cost("task", "claude-opus-5")
        assert result == 5.0  # 3.0 * (30/18) = 5.0

    @pytest.mark.asyncio
    async def test_haiku_returns_min_budget(self, mock_db_pool):
        from app.services.loop_controller import resolve_max_cost
        mock_db_pool.fetchrow.return_value = {"input_cost": 1.0, "output_cost": 5.0}
        result = await resolve_max_cost("task", "claude-haiku-4-5-20251001")
        assert result == 1.0  # 3.0 * (6/18) = 1.0

    @pytest.mark.asyncio
    async def test_gpt56_sol_returns_scaled(self, mock_db_pool):
        from app.services.loop_controller import resolve_max_cost
        mock_db_pool.fetchrow.return_value = {"input_cost": 5.0, "output_cost": 30.0}
        result = await resolve_max_cost("task", "gpt-5.6-sol")
        assert result == 5.83  # 3.0 * (35/18) = 5.83

    @pytest.mark.asyncio
    async def test_gpt56_luna_returns_scaled(self, mock_db_pool):
        from app.services.loop_controller import resolve_max_cost
        mock_db_pool.fetchrow.return_value = {"input_cost": 1.0, "output_cost": 6.0}
        result = await resolve_max_cost("task", "gpt-5.6-luna")
        assert result == 1.17  # 3.0 * (7/18) = 1.17

    @pytest.mark.asyncio
    async def test_gpt54_mini_returns_min(self, mock_db_pool):
        from app.services.loop_controller import resolve_max_cost
        mock_db_pool.fetchrow.return_value = {"input_cost": 0.75, "output_cost": 4.5}
        result = await resolve_max_cost("task", "gpt-5.4-mini")
        assert result == 0.88  # 3.0 * (5.25/18) = 0.875 → round → 0.88

    @pytest.mark.asyncio
    async def test_monitor_type_base(self, mock_db_pool):
        from app.services.loop_controller import resolve_max_cost
        mock_db_pool.fetchrow.return_value = {"input_cost": 5.0, "output_cost": 25.0}
        result = await resolve_max_cost("monitor", "claude-opus-5")
        assert result == 0.83  # 0.50 * (30/18) = 0.833 → 0.83

    @pytest.mark.asyncio
    async def test_sequential_type_base(self, mock_db_pool):
        from app.services.loop_controller import resolve_max_cost
        mock_db_pool.fetchrow.return_value = {"input_cost": 5.0, "output_cost": 25.0}
        result = await resolve_max_cost("sequential", "claude-opus-5")
        assert result == 10.0  # 6.0 * (30/18) = 10.0

    @pytest.mark.asyncio
    async def test_unknown_model_returns_base(self, mock_db_pool):
        from app.services.loop_controller import resolve_max_cost
        mock_db_pool.fetchrow.return_value = None
        result = await resolve_max_cost("task", "unknown-model")
        assert result == 3.0

    @pytest.mark.asyncio
    async def test_null_cost_returns_base(self, mock_db_pool):
        from app.services.loop_controller import resolve_max_cost
        mock_db_pool.fetchrow.return_value = {"input_cost": None, "output_cost": None}
        result = await resolve_max_cost("task", "antigravity")
        assert result == 3.0

    @pytest.mark.asyncio
    async def test_no_model_returns_base(self, mock_db_pool):
        from app.services.loop_controller import resolve_max_cost
        result = await resolve_max_cost("task", None)
        assert result == 3.0

    @pytest.mark.asyncio
    async def test_min_budget_floor(self, mock_db_pool):
        from app.services.loop_controller import resolve_max_cost
        mock_db_pool.fetchrow.return_value = {"input_cost": 0.1, "output_cost": 0.1}
        result = await resolve_max_cost("monitor", "cheap-model")
        assert result == 0.50  # floor


class TestDefaultLimits:
    """루프 유형별 기본 제한값 테스트."""

    def test_valid_loop_types(self):
        from app.services.loop_controller import VALID_LOOP_TYPES
        assert VALID_LOOP_TYPES == {"monitor", "task", "sequential"}

    def test_monitor_defaults(self):
        from app.services.loop_controller import _DEFAULT_LIMITS
        m = _DEFAULT_LIMITS["monitor"]
        assert m["max_iterations"] == 100
        assert m["max_failures"] == 5
        assert m["min_interval"] == 60

    def test_task_defaults(self):
        from app.services.loop_controller import _DEFAULT_LIMITS
        t = _DEFAULT_LIMITS["task"]
        assert t["max_iterations"] == 10
        assert t["max_failures"] == 3

    def test_sequential_dynamic_iterations(self):
        from app.services.loop_controller import _DEFAULT_LIMITS
        s = _DEFAULT_LIMITS["sequential"]
        assert s["max_iterations"] is None  # task_count × 3
