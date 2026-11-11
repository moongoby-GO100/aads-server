"""Codex 모델 목록 자동 수집 — relay `codex debug models` 파싱, 신규 모델 비실행 등록, 조회 실패 시 codex 행 불변, DB 속성 우선."""
from __future__ import annotations

import json
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from app.services import cli_model_autoreg
from app.services import model_registry as registry
from tests.unit.test_agy_model_autodiscovery import _Resp, sync_conn  # noqa: F401  (fixture 재사용)

CATALOG = {"models": [
    {"slug": "gpt-6.1-sol", "display_name": "GPT-6.1 Sol", "visibility": "list"},
    {"slug": "gpt-6.2-sol", "display_name": "GPT-6.2 Sol", "visibility": "list"},
    {"slug": "gpt-6.2-sol", "display_name": "dup", "visibility": "list"},
    {"slug": "gpt-reserve", "visibility": "hide"},
    {"slug": "", "visibility": "list"},
]}


def _patch_http(monkeypatch, response=None, exc=None):
    class Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            assert url.endswith("/codex-models")
            if exc:
                raise exc
            return response

    monkeypatch.setattr(registry.httpx, "AsyncClient", Client)
    from app.services import model_selector as ms
    monkeypatch.setattr(ms, "_load_relay_shared_secret", lambda: "s")


def _no_cache(monkeypatch):
    monkeypatch.setattr(
        cli_model_autoreg, "read_codex_models_cache",
        lambda paths=None: ([], {"status": "unavailable", "count": 0, "error": "not_found"}),
    )


def test_codex_is_auto_discovery_supported_without_key():
    assert registry._provider_auto_discovery_supported("codex", {}) is True


@pytest.mark.asyncio
async def test_relay_catalog_is_parsed_visible_only_and_deduplicated(monkeypatch):
    _patch_http(monkeypatch, _Resp(200, {"output": json.dumps(CATALOG)}))
    rows, meta = await registry._fetch_codex_cli_models()
    assert [r["model_id"] for r in rows] == ["gpt-6.1-sol", "gpt-6.2-sol"]
    assert meta["status"] == "ok" and meta["source"] == "relay_codex_debug_models"
    assert meta["runtime_executable"] is False  # 발견만 — 실행 가능은 probe 몫


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["unreachable", "http_502", "bad_json", "bad_catalog", "empty"])
async def test_relay_failure_without_cache_is_unavailable_not_empty_success(monkeypatch, kind):
    _no_cache(monkeypatch)
    if kind == "unreachable":
        _patch_http(monkeypatch, exc=OSError("down"))
    elif kind == "http_502":
        _patch_http(monkeypatch, _Resp(502, {}))
    elif kind == "bad_json":
        _patch_http(monkeypatch, _Resp(200, bad_json=True))
    elif kind == "bad_catalog":
        _patch_http(monkeypatch, _Resp(200, {"output": "not json"}))
    else:
        _patch_http(monkeypatch, _Resp(200, {"output": json.dumps({"models": [{"slug": "x", "visibility": "hide"}]})}))
    rows, meta = await registry._fetch_codex_cli_models()
    assert rows == [] and meta["status"] == "unavailable" and meta["relay_error"]


@pytest.mark.asyncio
async def test_relay_failure_falls_back_to_cache_file(monkeypatch):
    _patch_http(monkeypatch, exc=OSError("down"))
    cache_rows = [{"model_id": "gpt-6-sol", "display_name": "x", "raw": {}}]
    monkeypatch.setattr(
        cli_model_autoreg, "read_codex_models_cache", lambda paths=None: (cache_rows, {"status": "ok", "count": 1}),
    )
    rows, meta = await registry._fetch_codex_cli_models()
    assert rows == cache_rows and meta["source"] == "models_cache" and meta["relay_error"].startswith("relay_unreachable")


def _discovered(model_id):
    return registry._discovered_model_row(
        provider="codex", model_id=model_id, display_name=model_id, key_state={}, raw={},
        source=cli_model_autoreg.DISCOVERY_SOURCE,
    )


def test_new_codex_model_is_registered_non_executable():
    row = _discovered("gpt-6.2-sol")
    assert row["is_executable"] is False and row["is_active"] is False and row["is_selectable"] is False
    assert row["metadata"]["execution_backend"] == "codex_cli"


def test_unlisted_codex_model_gets_same_family_defaults():
    row = _discovered("gpt-7-sol")
    assert row["supports_thinking"] is True and row["capabilities"]["thinking"] is True
    assert row["input_cost"] == Decimal("2.0") and row["output_cost"] == Decimal("10.0")  # gpt-6.1-sol 단가
    assert _discovered("gpt-7-luna")["input_cost"] == Decimal("0.1")
    assert _discovered("mystery-model")["input_cost"] is None  # 계열을 모르면 값을 지어내지 않는다


def test_db_values_override_code_table_but_null_keeps_seed():
    rows = [_discovered("gpt-6.1-sol"), _discovered("gpt-6-luna"), dict(_discovered("gpt-6.1-sol"), provider="openai")]
    db = [
        {"provider": "codex", "model_id": "gpt-6.1-sol", "input_cost": Decimal("3.5"),
         "output_cost": None, "supports_thinking": False},
        {"provider": "codex", "model_id": "gpt-6-luna", "input_cost": None, "output_cost": None, "supports_thinking": None},
        {"provider": "openai", "model_id": "gpt-6.1-sol", "input_cost": Decimal("9"), "output_cost": None, "supports_thinking": False},
    ]
    assert registry.apply_db_attribute_priority(rows, db) == 1
    sol, luna, other = rows
    assert sol["input_cost"] == Decimal("3.5") and sol["output_cost"] == Decimal("10.0")  # NULL 열은 시드 유지
    assert sol["supports_thinking"] is False and sol["capabilities"]["thinking"] is False
    assert sol["pricing"]["input_cost"] == "3.5"
    assert luna["input_cost"] == Decimal("0.1") and luna["supports_thinking"] is True
    assert other["input_cost"] == Decimal("2.0")  # codex/antigravity 외 provider 는 건드리지 않는다


async def _sync_with_codex_run(conn, monkeypatch, status, rows=()):
    run = {"provider": "codex", "status": status, "count": 0}
    monkeypatch.setattr(registry, "discover_provider_model_rows", AsyncMock(return_value=([], [run])))
    monkeypatch.setattr(registry, "_merge_model_rows", lambda *a, **k: list(rows))
    await registry._sync_model_registry_locked()
    retire = [c.args for c in conn.execute.await_args_list if 'retired": true' in c.args[0]]
    assert len(retire) == 1
    return retire[0]


@pytest.mark.asyncio
async def test_lookup_failure_spares_all_codex_rows_from_retirement(sync_conn, monkeypatch):  # noqa: F811
    args = await _sync_with_codex_run(sync_conn, monkeypatch, "unavailable")
    assert "provider = 'codex'" in args[0] and args[2] is True


@pytest.mark.asyncio
async def test_successful_lookup_allows_retirement_of_vanished_codex_rows(sync_conn, monkeypatch):  # noqa: F811
    args = await _sync_with_codex_run(sync_conn, monkeypatch, "ok")
    assert args[2] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["unavailable", "failed"])
async def test_lookup_failure_does_not_upsert_any_codex_row(sync_conn, monkeypatch, status):  # noqa: F811
    def mk(provider, model_id, source):
        return {
            "provider": provider, "model_id": model_id, "display_name": model_id, "family": "gpt",
            "category": "coding", "supports_tools": True, "supports_thinking": True, "supports_vision": False,
            "supports_coding": True, "input_cost": None, "output_cost": None, "is_active": True,
            "activation_source": "auto", "linked_key_name": None, "metadata": {}, "discovery_source": source,
            "is_executable": True, "is_selectable": True,
        }
    rows = [mk("codex", "gpt-6-sol", "template"), mk("codex", "gpt-6.1-sol", cli_model_autoreg.DISCOVERY_SOURCE),
            mk("openai", "gpt-x", "api")]
    await _sync_with_codex_run(sync_conn, monkeypatch, status, rows)
    upserts = [c.args for c in sync_conn.execute.await_args_list if "INSERT INTO llm_models" in c.args[0]]
    providers = {a[1] for a in upserts}
    assert "codex" not in providers and "openai" in providers


@pytest.mark.asyncio
async def test_relay_failure_with_empty_cache_is_unavailable_and_spares_codex_rows(sync_conn, monkeypatch):  # noqa: F811
    _patch_http(monkeypatch, exc=RuntimeError("down"))
    monkeypatch.setattr(
        cli_model_autoreg, "read_codex_models_cache",
        lambda paths=None: ([], {"status": "ok", "count": 0}),  # {"models": []} 캐시
    )
    rows, meta = await registry._fetch_codex_cli_models()
    assert rows == [] and meta["status"] == "unavailable" and meta["error"] == "empty_cache"
    row = _discovered("gpt-6.1-sol")
    row["discovery_source"] = cli_model_autoreg.DISCOVERY_SOURCE
    monkeypatch.setattr(registry, "discover_provider_model_rows", AsyncMock(return_value=([], [dict(meta, provider="codex")])))
    monkeypatch.setattr(registry, "_merge_model_rows", lambda *a, **k: [row])
    await registry._sync_model_registry_locked()
    calls = [c.args for c in sync_conn.execute.await_args_list]
    assert not [a for a in calls if "INSERT INTO llm_models" in a[0] and a[1] == "codex"]
    retire = [a for a in calls if 'retired": true' in a[0]]
    assert len(retire) == 1 and retire[0][2] is True and "provider = 'codex'" in retire[0][0]


def test_unlisted_codex_model_gets_highest_known_generation_tier():
    for grade in ("sol", "terra", "luna"):
        known = [k for (p, k) in registry._MODEL_TIER_CATALOG if p == "codex" and k.endswith("-" + grade)]
        top = max(known, key=lambda k: float(k.split("-")[1]))
        assert registry._catalog_tier("codex", f"gpt-7-{grade}") == registry._MODEL_TIER_CATALOG[("codex", top)]
    assert registry._catalog_tier("codex", "gpt-7-sol") != (None, None)
    assert registry._catalog_tier("codex", "mystery-model") == (None, None)
    assert registry._catalog_tier("openai", "gpt-7-sol") == (None, None)


def test_db_tier_wins_and_metadata_keys_are_preserved():
    row = _discovered("gpt-6.1-sol")
    row["metadata"] = {"execution_backend": "codex_cli", "new_key": 1}
    db = [{"provider": "codex", "model_id": "gpt-6.1-sol", "input_cost": None, "output_cost": None,
           "supports_thinking": None, "tier": "S", "fallback_group": "custom",
           "metadata": json.dumps({"operator_note": "keep", "new_key": 0, "release_date": "2026-01-01"})}]
    assert registry.apply_db_attribute_priority([row], db) == 1
    assert row["tier"] == "S" and row["fallback_group"] == "custom"
    assert row["metadata"]["operator_note"] == "keep" and row["metadata"]["new_key"] == 1
    assert row["metadata"]["release_date"] == "2026-01-01" and row["metadata"]["execution_backend"] == "codex_cli"
