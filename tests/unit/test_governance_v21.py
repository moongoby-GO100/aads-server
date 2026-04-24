from __future__ import annotations

from decimal import Decimal

import pytest

from app.api import governance
from app.core import feature_flags
from app.services import intent_router


class FakeConn:
    def __init__(self, value):
        self.value = value
        self.fetchval_calls: list[tuple[str, tuple[object, ...]]] = []

    async def fetchval(self, query, *args):
        self.fetchval_calls.append((query, args))
        return self.value


class FakeAcquire:
    def __init__(self, conn: FakeConn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakePool:
    def __init__(self, conn: FakeConn):
        self.conn = conn

    def acquire(self):
        return FakeAcquire(self.conn)


class FakeFetchConn:
    def __init__(self, columns=None, rows=None):
        self.columns = columns or []
        self.rows = rows or []
        self.fetch_calls: list[tuple[str, tuple[object, ...]]] = []

    async def fetch(self, query, *args):
        self.fetch_calls.append((query, args))
        if "information_schema.columns" in query:
            return [{"column_name": value} for value in self.columns]
        if "FROM role_profiles" in query:
            return self.rows
        return []


@pytest.mark.asyncio
async def test_governance_enabled_reads_feature_flag(monkeypatch):
    conn = FakeConn(False)
    feature_flags.invalidate_flag_cache()
    monkeypatch.setattr(feature_flags, "get_pool", lambda: FakePool(conn))

    assert await feature_flags.governance_enabled(default=True) is False
    assert conn.fetchval_calls == [
        ("SELECT enabled FROM feature_flags WHERE flag_key = $1", ("governance_enabled",))
    ]


@pytest.mark.asyncio
async def test_resolve_intent_temperature_reads_intent_policies_table(monkeypatch):
    conn = FakeConn(Decimal("0.55"))

    async def _enabled():
        return True

    monkeypatch.setattr(feature_flags, "governance_enabled", _enabled)

    import app.core.db_pool as db_pool

    monkeypatch.setattr(db_pool, "get_pool", lambda: FakePool(conn))

    assert await intent_router.resolve_intent_temperature("search") == pytest.approx(0.55)
    assert conn.fetchval_calls == [
        ("SELECT temperature FROM intent_policies WHERE intent = $1", ("search",))
    ]


@pytest.mark.asyncio
async def test_resolve_intent_temperature_skips_db_when_governance_disabled(monkeypatch):
    async def _disabled():
        return False

    monkeypatch.setattr(feature_flags, "governance_enabled", _disabled)

    import app.core.db_pool as db_pool

    monkeypatch.setattr(db_pool, "get_pool", lambda: (_ for _ in ()).throw(AssertionError("DB should not be used")))

    assert await intent_router.resolve_intent_temperature("casual") == pytest.approx(0.2)


@pytest.mark.asyncio
async def test_get_role_profiles_includes_project_scope(monkeypatch):
    conn = FakeFetchConn(
        columns=[
            "role",
            "system_prompt_ref",
            "tool_allowlist",
            "max_turns",
            "budget_usd",
            "escalation_rules",
            "project_scope",
            "updated_at",
        ],
        rows=[
            {
                "role": "CEO",
                "system_prompt_ref": "app/core/prompts/system_prompt_v2.py",
                "tool_allowlist": ["run_remote_command", "query_database"],
                "max_turns": 200,
                "budget_usd": Decimal("200.00"),
                "escalation_rules": {"approval_scope": "global"},
                "project_scope": ["AADS", "KIS"],
                "updated_at": None,
            }
        ],
    )

    monkeypatch.setattr(governance, "get_pool", lambda: FakePool(conn))

    result = await governance.get_role_profiles()

    assert result["total"] == 1
    assert result["profiles"][0]["role"] == "CEO"
    assert result["profiles"][0]["project_scope"] == ["AADS", "KIS"]
    assert result["profiles"][0]["tool_allowlist"] == ["run_remote_command", "query_database"]
