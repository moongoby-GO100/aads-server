"""AGY(Antigravity) 모델 목록 자동 수집 — `agy models` 파싱, 신규 모델 비실행 등록 → 검증 후 실행 가능, 조회 실패 시 불변."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock

import pytest

from app.services import model_registry as registry
from app.services import model_selector as ms
from app.services.model_registry import ProbeOutcome

NOW = datetime(2026, 11, 11, 6, 0, tzinfo=timezone.utc)

# `agy models` 1.5.5 실측 형태(탭 구분) + 잡음 줄.
AGY_OUTPUT = (
    "Available models:\n"
    "gemini-4-argon-high\tGemini 4 Argon High\n"
    "gemini-4-carbon-low\tGemini 4 Carbon Low\n"
    "gemini-4-carbon-medium\tGemini 4 Carbon Medium\n"
    "gemini-4-carbon-high\tGemini 4 Carbon High\n"
    "gemini-4-carbon-high\tduplicate\n"
    "claude-sonnet-5-5-low\tClaude Sonnet 5.5 Low\n"
    "gpt-oss-120b-medium\tGPT-OSS 120B Medium\n"
    "Bad Id With Space\tnope\n"
    "\n"
)


def test_parse_agy_models_output_keeps_only_tab_rows_with_valid_ids():
    rows = registry.parse_agy_models_output(AGY_OUTPUT)
    assert [r["model_id"] for r in rows] == [
        "gemini-4-argon-high", "gemini-4-carbon-low", "gemini-4-carbon-medium", "gemini-4-carbon-high",
        "claude-sonnet-5-5-low", "gpt-oss-120b-medium",
    ]
    assert rows[1]["display_name"] == "Gemini 4 Carbon Low"
    assert registry.parse_agy_models_output(None) == []
    assert registry.parse_agy_models_output("no tabs here\n") == []


def _discovered_carbon():
    return registry._discovered_model_row(
        provider="antigravity", model_id="gemini-4-carbon-high", display_name="Gemini 4 Carbon High",
        key_state={}, raw={}, source=registry.AGY_DISCOVERY_SOURCE,
    )


def test_new_carbon_row_is_registered_non_executable_with_family_defaults():
    row = _discovered_carbon()
    assert row["is_executable"] is False and row["is_active"] is False and row["is_selectable"] is False
    assert row["metadata"]["model_source"] == "discovery"
    assert row["metadata"]["discovery_mode"] == "discovery"
    assert row["metadata"]["activation_candidate"] is True
    assert row["metadata"]["execution_backend"] == "antigravity_cli"
    assert row["supports_thinking"] is True and row["capabilities"]["thinking"] is True
    assert row["input_cost"] == 0 and row["output_cost"] == 0
    assert row["pricing"]["unit"] == "usd_per_1m_tokens"


def test_unlisted_future_agy_model_still_gets_defaults():
    row = registry._discovered_model_row(
        provider="antigravity", model_id="gemini-5-future-medium", display_name="x", key_state={}, raw={},
        source=registry.AGY_DISCOVERY_SOURCE,
    )
    assert row["supports_thinking"] is True
    assert row["input_cost"] == 0 and row["pricing"]["input_cost"] == "0.0"
    # 다른 provider 의 미등록 모델에는 기본값을 주지 않는다.
    assert registry._pricing_for("some-unknown-model", "openai") == {}


@pytest.mark.asyncio
async def test_carbon_becomes_executable_only_after_probe_success():
    row = _discovered_carbon()
    probe = AsyncMock(return_value=ProbeOutcome(True))
    summary = await registry.apply_activation_probes([row], {}, now=NOW, prober=probe)
    probe.assert_awaited_once()
    assert summary["activated"] == ["antigravity:gemini-4-carbon-high"]
    assert row["is_executable"] is True and row["is_active"] is True and row["verification_status"] == "verified"


@pytest.mark.asyncio
async def test_carbon_stays_non_executable_when_probe_fails():
    row = _discovered_carbon()
    probe = AsyncMock(return_value=ProbeOutcome(False, "cli_error", False))
    await registry.apply_activation_probes([row], {}, now=NOW, prober=probe)
    assert row["is_executable"] is False and row["is_active"] is False
    assert row["verification_status"] == "review_required"


def _relay_events(events):
    async def fake(model, system_prompt, messages, tools=None, session_id=None):
        assert model == "gemini-4-carbon-high"
        for ev in events:
            yield ev
    return fake


@pytest.mark.asyncio
async def test_probe_antigravity_uses_relay_and_requires_text(monkeypatch):
    monkeypatch.setattr(ms, "_stream_antigravity_relay_once", _relay_events(
        [{"type": "delta", "content": "OK"}, {"type": "done"}]))
    assert (await registry.probe_model_execution(_discovered_carbon())).ok is True

    monkeypatch.setattr(ms, "_stream_antigravity_relay_once", _relay_events([{"type": "done"}]))
    out = await registry.probe_model_execution(_discovered_carbon())
    assert out.ok is False and out.error == "empty_result"

    monkeypatch.setattr(ms, "_stream_antigravity_relay_once", _relay_events(
        [{"type": "error", "content": "Antigravity Relay 400: unknown_antigravity_model"}]))
    out = await registry.probe_model_execution(_discovered_carbon())
    assert out.ok is False and out.transient is False


class _Resp:
    def __init__(self, status=200, payload=None, bad_json=False):
        self.status_code, self._payload, self._bad = status, payload, bad_json

    def json(self):
        if self._bad:
            raise ValueError("bad")
        return self._payload


def _patch_http(monkeypatch, response=None, exc=None):
    class Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            assert url.endswith("/antigravity-models")
            if exc:
                raise exc
            return response

    monkeypatch.setattr(registry.httpx, "AsyncClient", Client)
    monkeypatch.setattr(ms, "_load_relay_shared_secret", lambda: "s")


@pytest.mark.asyncio
async def test_fetch_success_returns_rows_and_supported_flag(monkeypatch):
    _patch_http(monkeypatch, _Resp(200, {"output": AGY_OUTPUT}))
    rows, meta = await registry._fetch_antigravity_models()
    assert meta["status"] == "ok" and meta["auto_discovery_supported"] is True
    assert "gemini-4-carbon-high" in [r["model_id"] for r in rows]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["unreachable", "http_502", "bad_json", "empty_output"])
async def test_fetch_failure_is_unavailable_not_empty_success(monkeypatch, kind):
    if kind == "unreachable":
        _patch_http(monkeypatch, exc=OSError("down"))
    elif kind == "http_502":
        _patch_http(monkeypatch, _Resp(502, {}))
    elif kind == "bad_json":
        _patch_http(monkeypatch, _Resp(200, bad_json=True))
    else:
        _patch_http(monkeypatch, _Resp(200, {"output": "header only\n"}))
    rows, meta = await registry._fetch_antigravity_models()
    assert rows == [] and meta["status"] == "unavailable" and meta["error"]


@pytest.fixture
def sync_conn(monkeypatch):
    conn = Mock()
    conn.fetch = AsyncMock(return_value=[])
    conn.execute = AsyncMock()
    tx = Mock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = Mock(return_value=tx)
    pool = Mock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(registry, "get_pool", lambda: pool)
    for name, val in (("_fetch_key_rows", []), ("_fetch_last_discovery_success", {}), ("_fetch_activation_state", {})):
        monkeypatch.setattr(registry, name, AsyncMock(return_value=val))
    monkeypatch.setattr(registry.cli_model_autoreg, "fetch_verified_codex_ids", AsyncMock(return_value=set()))
    monkeypatch.setattr(registry.cli_model_autoreg, "autoregister_after_sync", AsyncMock(return_value={}))
    monkeypatch.setattr(registry, "refresh_model_freshness_cache", AsyncMock())
    monkeypatch.setattr(registry, "build_registry_snapshots", lambda k: ([], []))
    monkeypatch.setattr(registry, "apply_activation_probes", AsyncMock(return_value={}))
    return conn


async def _sync_with_agy_run(conn, monkeypatch, status):
    run = {"provider": "antigravity", "status": status, "count": 0}
    monkeypatch.setattr(registry, "discover_provider_model_rows", AsyncMock(return_value=([], [run])))
    monkeypatch.setattr(registry, "_merge_model_rows", lambda *a, **k: [])
    await registry._sync_model_registry_locked()
    retire = [c.args for c in conn.execute.await_args_list if 'retired": true' in c.args[0]]
    assert len(retire) == 1
    return retire[0]


@pytest.mark.asyncio
async def test_lookup_failure_spares_existing_agy_rows_from_retirement(sync_conn, monkeypatch):
    args = await _sync_with_agy_run(sync_conn, monkeypatch, "unavailable")
    assert "provider = 'antigravity'" in args[0] and args[5] is True  # preserve_agy_cli_rows: antigravity 전체 제외


@pytest.mark.asyncio
async def test_successful_lookup_allows_retirement_of_vanished_agy_rows(sync_conn, monkeypatch):
    args = await _sync_with_agy_run(sync_conn, monkeypatch, "ok")
    assert args[5] is False


def test_relay_accepts_live_listed_model_but_never_retired_or_unknown(monkeypatch):
    from scripts import claude_relay_server as relay

    monkeypatch.setattr(relay, "_ANTIGRAVITY_LIVE_MODELS", {})
    assert relay._resolve_antigravity_model("gemini-4-carbon-high") is None  # 고정 표에 없고 아직 목록도 못 봤다
    assert relay._resolve_antigravity_model("gemini-4-argon-high") == "gemini-4-argon-high"  # 고정 표는 그대로
    ids = relay._parse_antigravity_models_output(AGY_OUTPUT + "gemini-5-new-medium\tNew\ngemini-3.7-flash-low\tOld\n")
    relay._remember_antigravity_live_models(ids)
    assert relay._resolve_antigravity_model("gemini-4-carbon-high") == "gemini-4-carbon-high"
    assert relay._resolve_antigravity_model("gemini-5-new-medium") == "gemini-5-new-medium"
    assert relay._resolve_antigravity_model("gemini-3.7-flash-low") is None  # 폐기 목록 우선
    assert relay._antigravity_model_rejection("gemini-5-new-medium") is None
    monkeypatch.setattr(relay.time, "monotonic", lambda: 10 ** 9)  # TTL 만료되어도 판정은 마지막 성공 목록 유지
    assert relay._resolve_antigravity_model("gemini-5-new-medium") == "gemini-5-new-medium"
    assert relay._antigravity_live_model_fresh("gemini-5-new-medium") is False  # TTL 은 갱신 시점 판단에만 사용


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["unavailable", "failed"])
async def test_lookup_failure_does_not_upsert_any_antigravity_row(sync_conn, monkeypatch, status):
    """조회 실패 회차: 템플릿·cli 행 모두 UPSERT 하지 않는다 → discovery_source·metadata·is_executable 불변."""
    def mk(model_id, source):
        return {
            "provider": "antigravity", "model_id": model_id, "display_name": model_id, "family": "gemini",
            "category": "chat", "supports_tools": True, "supports_thinking": True, "supports_vision": True,
            "supports_coding": True, "input_cost": None, "output_cost": None, "is_active": True,
            "activation_source": "auto", "linked_key_name": None, "metadata": {}, "discovery_source": source,
            "is_executable": True, "is_selectable": True,
        }
    other = dict(mk("gpt-x", "api"), provider="openai")
    rows = [mk("gemini-4-argon-high", "template"), mk("gemini-4-carbon-high", registry.AGY_DISCOVERY_SOURCE), other]
    run = {"provider": "antigravity", "status": status, "count": 0}
    monkeypatch.setattr(registry, "discover_provider_model_rows", AsyncMock(return_value=([], [run])))
    monkeypatch.setattr(registry, "_merge_model_rows", lambda *a, **k: rows)
    await registry._sync_model_registry_locked()
    upserts = [c.args for c in sync_conn.execute.await_args_list if "INSERT INTO llm_models" in c.args[0]]
    providers = {a[1] for a in upserts}
    assert "antigravity" not in providers
    assert "openai" in providers  # 다른 공급자는 평소대로


def _relay_models_text(ids):
    return "".join("%s\tName\n" % i for i in ids)


@pytest.mark.asyncio
async def test_relay_refreshes_expired_cache_then_accepts_new_model(monkeypatch):
    from scripts import claude_relay_server as relay

    monkeypatch.setattr(relay, "_ANTIGRAVITY_LIVE_MODELS", {})
    monkeypatch.setattr(relay, "_ANTIGRAVITY_REFRESH_LOCK", None)
    monkeypatch.setattr(relay, "_ANTIGRAVITY_LAST_REFRESH", 0.0)
    calls = []

    async def fake_fetch():
        calls.append(1)
        relay._remember_antigravity_live_models(["gemini-5-new-medium"])
        return "", ["gemini-5-new-medium"], None, 0

    monkeypatch.setattr(relay, "_fetch_antigravity_models_text", fake_fetch)
    assert relay._antigravity_model_rejection("gemini-5-new-medium") is not None  # 갱신 전
    await relay._refresh_antigravity_live_models_if_needed("gemini-5-new-medium")
    assert relay._antigravity_model_rejection("gemini-5-new-medium") is None
    assert len(calls) == 1
    await relay._refresh_antigravity_live_models_if_needed("gemini-5-new-medium")  # 캐시 유효 → 재갱신 없음
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_relay_still_rejects_unlisted_model_after_refresh(monkeypatch):
    from scripts import claude_relay_server as relay

    monkeypatch.setattr(relay, "_ANTIGRAVITY_LIVE_MODELS", {})
    monkeypatch.setattr(relay, "_ANTIGRAVITY_REFRESH_LOCK", None)
    monkeypatch.setattr(relay, "_ANTIGRAVITY_LAST_REFRESH", 0.0)
    calls = []

    async def fake_fetch():
        calls.append(1)
        relay._remember_antigravity_live_models(["gemini-5-new-medium"])
        return "", ["gemini-5-new-medium"], None, 0

    monkeypatch.setattr(relay, "_fetch_antigravity_models_text", fake_fetch)
    await relay._refresh_antigravity_live_models_if_needed("gemini-9-ghost-high")
    assert len(calls) == 1
    assert relay._antigravity_model_rejection("gemini-9-ghost-high") is not None


@pytest.mark.asyncio
async def test_relay_refresh_failure_keeps_last_good_cache(monkeypatch):
    import time

    from scripts import claude_relay_server as relay

    monkeypatch.setattr(relay, "_ANTIGRAVITY_LIVE_MODELS", {})
    monkeypatch.setattr(relay, "_ANTIGRAVITY_REFRESH_LOCK", None)
    monkeypatch.setattr(relay, "_ANTIGRAVITY_LAST_REFRESH", 0.0)

    # 1. 초기 캐시를 만료 상태(TTL 900초 초과)로 적재
    expired_time = time.monotonic() - 1000.0
    relay._remember_antigravity_live_models(["gemini-5-new-medium"], now=expired_time)

    fetch_calls = []

    async def boom():
        fetch_calls.append("boom")
        return "", [], "agy_models_timeout", None

    monkeypatch.setattr(relay, "_fetch_antigravity_models_text", boom)

    # ① 캐시 만료 + 갱신 실패 → 마지막 성공 목록에 있는 모델 허용
    await relay._refresh_antigravity_live_models_if_needed("gemini-5-new-medium")
    assert len(fetch_calls) == 1  # 만료되었으므로 갱신 시도
    assert relay._antigravity_model_rejection("gemini-5-new-medium") is None
    assert relay._resolve_antigravity_model("gemini-5-new-medium") == "gemini-5-new-medium"

    # ② 같은 상황에서 목록에 없는 모델 거절
    await relay._refresh_antigravity_live_models_if_needed("gemini-9-ghost-high")
    assert relay._antigravity_model_rejection("gemini-9-ghost-high") is not None
    assert relay._resolve_antigravity_model("gemini-9-ghost-high") is None
    # 마지막 성공 목록에 있는 모델은 계속 허용 유지
    assert relay._antigravity_model_rejection("gemini-5-new-medium") is None
    assert relay._resolve_antigravity_model("gemini-5-new-medium") == "gemini-5-new-medium"

    # ③ 캐시 만료 + 갱신 성공 → 새 목록 기준
    async def success():
        fetch_calls.append("success")
        relay._remember_antigravity_live_models(["gemini-6-new-high"])
        return "", ["gemini-6-new-high"], None, 0

    monkeypatch.setattr(relay, "_fetch_antigravity_models_text", success)
    await relay._refresh_antigravity_live_models_if_needed("gemini-6-new-high")
    assert "success" in fetch_calls
    # 새 목록 기준: 새 모델 허용
    assert relay._antigravity_model_rejection("gemini-6-new-high") is None
    assert relay._resolve_antigravity_model("gemini-6-new-high") == "gemini-6-new-high"
    # 옛 모델은 새 목록에 없으므로 거절
    assert relay._antigravity_model_rejection("gemini-5-new-medium") is not None
    assert relay._resolve_antigravity_model("gemini-5-new-medium") is None


@pytest.mark.asyncio
async def test_relay_concurrent_requests_refresh_once(monkeypatch):
    import asyncio
    from scripts import claude_relay_server as relay

    monkeypatch.setattr(relay, "_ANTIGRAVITY_LIVE_MODELS", {})
    monkeypatch.setattr(relay, "_ANTIGRAVITY_REFRESH_LOCK", None)
    monkeypatch.setattr(relay, "_ANTIGRAVITY_LAST_REFRESH", 0.0)
    calls = []

    async def slow_fetch():
        calls.append(1)
        await asyncio.sleep(0.05)
        relay._remember_antigravity_live_models(["gemini-5-new-medium"])
        return "", ["gemini-5-new-medium"], None, 0

    monkeypatch.setattr(relay, "_fetch_antigravity_models_text", slow_fetch)
    await asyncio.gather(*[relay._refresh_antigravity_live_models_if_needed("gemini-5-new-medium") for _ in range(4)])
    assert len(calls) == 1
