from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from app.services import model_registry

_COALESCE_TIER = "tier = COALESCE(llm_models.tier, EXCLUDED.tier)"
_COALESCE_FB = "fallback_group = COALESCE(llm_models.fallback_group, EXCLUDED.fallback_group)"
_ARG_SELECTABLE, _ARG_TIER, _ARG_FALLBACK = 21, 23, 24


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Acquire(_Tx):
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _Acquire(self.conn)


class _TableConn:
    """llm_models 를 메모리로 흉내 낸다. 업서트 SQL 이 COALESCE 규칙을 담고 있을 때만 그 규칙을 적용한다."""

    def __init__(self, table):
        self.table = table

    def transaction(self):
        return _Tx()

    async def fetch(self, query, *args):
        return []

    async def execute(self, query, *args):
        if "INSERT INTO llm_models" not in query:
            return "OK"
        assert _COALESCE_TIER in query and _COALESCE_FB in query
        key = (args[0], args[1])
        new = {"tier": args[_ARG_TIER], "fallback_group": args[_ARG_FALLBACK]}
        old = self.table.get(key, {})
        self.table[key] = {
            "tier": old.get("tier") or new["tier"],
            "fallback_group": old.get("fallback_group") or new["fallback_group"],
            "is_selectable": args[_ARG_SELECTABLE],
        }
        return "OK"


def _template_rows():
    now = datetime.now(timezone.utc)
    rows, providers = model_registry.build_registry_snapshots([
        {
            "id": 1, "provider": "anthropic", "key_name": "ANTHROPIC_AUTH_TOKEN",
            "priority": 1, "is_active": True, "rate_limited_until": None,
            "last_used_at": now, "last_verified_at": now,
        }
    ])
    return rows, providers


async def _run_sync(monkeypatch, table):
    rows, providers = _template_rows()
    monkeypatch.setattr(model_registry, "get_pool", lambda: _Pool(_TableConn(table)))
    monkeypatch.setattr(model_registry, "_fetch_key_rows", AsyncMock(return_value=[]))
    monkeypatch.setattr(model_registry, "build_registry_snapshots", lambda _k: (rows, providers))
    monkeypatch.setattr(model_registry, "discover_provider_model_rows", AsyncMock(return_value=([], [])))
    monkeypatch.setattr(model_registry, "append_key_audit_log", AsyncMock())
    result = await model_registry.sync_model_registry(triggered_by="test")
    return rows, result


@pytest.mark.asyncio
async def test_resync_fills_null_tier_and_fallback_group_from_catalog(monkeypatch):
    table = {
        ("anthropic", "claude-opus-5-5"): {"tier": None, "fallback_group": None, "is_selectable": True},
        ("anthropic", "claude-opus-5"): {"tier": "S", "fallback_group": None, "is_selectable": True},
        ("anthropic", "claude-sonnet-5"): {"tier": "A", "fallback_group": None, "is_selectable": True},
    }
    await _run_sync(monkeypatch, table)

    assert (table[("anthropic", "claude-opus-5-5")]["tier"], table[("anthropic", "claude-opus-5-5")]["fallback_group"]) == ("S", "premium")
    assert table[("anthropic", "claude-opus-5")]["fallback_group"] == "premium"
    assert table[("anthropic", "claude-sonnet-5")]["fallback_group"] == "standard"


@pytest.mark.asyncio
async def test_resync_keeps_existing_values_when_catalog_has_no_value(monkeypatch):
    rows, _ = _template_rows()
    uncatalogued = [
        (r["provider"], r["model_id"]) for r in rows
        if (r["provider"], r["model_id"]) not in model_registry._MODEL_TIER_CATALOG
    ]
    assert len(uncatalogued) >= 2
    null_key, set_key = uncatalogued[0], uncatalogued[1]
    hand_set = ("anthropic", "claude-opus-5")
    table = {
        null_key: {"tier": None, "fallback_group": None, "is_selectable": False},
        set_key: {"tier": "B", "fallback_group": "fast", "is_selectable": False},
        hand_set: {"tier": "A", "fallback_group": "custom", "is_selectable": True},
    }
    await _run_sync(monkeypatch, table)

    assert (table[null_key]["tier"], table[null_key]["fallback_group"]) == (None, None)
    assert (table[set_key]["tier"], table[set_key]["fallback_group"]) == ("B", "fast")
    assert (table[hand_set]["tier"], table[hand_set]["fallback_group"]) == ("A", "custom")


@pytest.mark.asyncio
async def test_resync_does_not_demote_selectable_rows(monkeypatch):
    rows, _ = _template_rows()
    table = {
        (r["provider"], r["model_id"]): {
            "tier": None, "fallback_group": None,
            "is_selectable": bool(r.get("is_selectable", r.get("is_active", False))),
        }
        for r in rows
    }
    selectable_before = {k for k, v in table.items() if v["is_selectable"]}
    assert selectable_before

    await _run_sync(monkeypatch, table)

    selectable_after = {k for k, v in table.items() if v["is_selectable"]}
    assert selectable_before <= selectable_after
