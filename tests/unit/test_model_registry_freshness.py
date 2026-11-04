"""회사별 발견 신선도 판정 + DeepSeek 공식 목록 발견 회귀 테스트."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import httpx
import pytest

from app.services import model_registry

NOW = datetime(2026, 11, 4, 0, 0, tzinfo=timezone.utc)

# 공식 GET https://api.deepseek.com/models 응답 형태 (OpenAI 호환 목록)
DEEPSEEK_OFFICIAL = {
    "object": "list",
    "data": [
        {"id": "deepseek-flash", "object": "model", "owned_by": "deepseek"},
        {"id": "deepseek-v4-pro", "object": "model", "owned_by": "deepseek"},
        {"id": "deepseek-v4-flash", "object": "model", "owned_by": "deepseek"},
        {"id": "deepseek-v9-brandnew", "object": "model", "owned_by": "deepseek"},
    ],
}


class _FakeAsyncClient:
    def __init__(self, response=None, error=None, **_kwargs):
        self._response = response
        self._error = error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def get(self, url, headers=None):
        assert "api.deepseek.com" in url
        assert headers["Authorization"].startswith("Bearer ")
        if self._error:
            raise self._error
        return self._response


def _patch_deepseek_http(monkeypatch, *, status=200, payload=None, error=None):
    request = httpx.Request("GET", "https://api.deepseek.com/models")
    response = httpx.Response(status, json=payload if payload is not None else {}, request=request)
    monkeypatch.setattr(model_registry, "_get_first_provider_key", AsyncMock(return_value="sk-test-not-real"))
    monkeypatch.setattr(
        model_registry.httpx, "AsyncClient", lambda **kw: _FakeAsyncClient(response=response, error=error, **kw)
    )


def test_classify_deepseek_catalog_separates_canonical_alias_and_new():
    result = model_registry.classify_deepseek_catalog(
        [item["id"] for item in DEEPSEEK_OFFICIAL["data"]]
    )
    assert result["canonical_ids"] == ["deepseek-flash", "deepseek-v4-pro"]
    assert result["alias_ids"] == ["deepseek-v4-flash"]
    assert result["unverified_new_ids"] == ["deepseek-v9-brandnew"]
    assert result["missing_canonical_ids"] == []


def test_deepseek_canonical_served_via_alias_is_not_missing():
    result = model_registry.classify_deepseek_catalog(["deepseek-chat", "deepseek-v4-pro"])
    assert "deepseek-flash" not in result["missing_canonical_ids"]
    assert result["alias_ids"] == ["deepseek-chat"]


def test_deepseek_absent_canonical_is_transient_missing_not_retired():
    result = model_registry.classify_deepseek_catalog(["deepseek-v4-pro"])
    assert result["missing_canonical_ids"] == ["deepseek-flash"]
    assert ("deepseek", "deepseek-flash") not in model_registry._FORCED_RETIRED_MODELS
    assert "transient" in result["absence_policy"]


@pytest.mark.asyncio
async def test_fetch_deepseek_models_official_fixture(monkeypatch):
    _patch_deepseek_http(monkeypatch, payload=DEEPSEEK_OFFICIAL)
    rows, result = await model_registry._fetch_deepseek_models()
    assert [row["model_id"] for row in rows] == [item["id"] for item in DEEPSEEK_OFFICIAL["data"]]
    assert result["status"] == "ok"
    assert result["discovery_source"] == "deepseek_api"
    assert result["unverified_new_ids"] == ["deepseek-v9-brandnew"]
    assert "sk-test-not-real" not in json.dumps(result)


@pytest.mark.asyncio
async def test_fetch_deepseek_models_without_key_is_skipped(monkeypatch):
    monkeypatch.setattr(model_registry, "_get_first_provider_key", AsyncMock(return_value=""))
    rows, result = await model_registry._fetch_deepseek_models()
    assert rows == []
    assert result["status"] == "skipped" and result["error"] == "missing_key"
    assert result["auto_discovery_supported"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("status,reason", [(401, "auth_rejected"), (403, "auth_rejected"), (429, "rate_limited"), (503, "fetch_failed")])
async def test_fetch_deepseek_http_failure_is_error_without_key_leak(monkeypatch, status, reason):
    _patch_deepseek_http(monkeypatch, status=status)
    rows, result = await model_registry._fetch_deepseek_models()
    assert rows == []
    assert result["status"] == "failed" and result["error"] == f"http_{status}"
    assert "sk-test-not-real" not in json.dumps(result)
    run = {"provider": "deepseek", "observed_at": NOW.isoformat(), **result}
    assert model_registry.classify_discovery_run(run, now=NOW)["reason_class"] == reason


@pytest.mark.asyncio
async def test_fetch_deepseek_network_error_is_error(monkeypatch):
    _patch_deepseek_http(monkeypatch, error=httpx.ConnectTimeout("t"))
    rows, result = await model_registry._fetch_deepseek_models()
    assert rows == [] and result["status"] == "failed"
    assert result["error"] == "fetch_error:ConnectTimeout"


def test_classify_skipped_reasons_never_latest_confirmed():
    oauth = model_registry.classify_discovery_run(
        {"status": "skipped", "error": "oauth_runtime_only_models_api_unavailable", "observed_at": NOW.isoformat()},
        now=NOW,
    )
    google = model_registry.classify_discovery_run(
        {"status": "skipped", "error": "google_routes_disabled", "observed_at": NOW.isoformat()}, now=NOW
    )
    assert oauth["state"] == "skipped" and oauth["reason_class"] == "oauth_runtime_only"
    assert oauth["manual_registration_required"] is True and oauth["latest_confirmed"] is False
    assert google["state"] == "skipped" and google["reason_class"] == "route_policy_disabled"
    assert google["latest_confirmed"] is False and google["manual_registration_required"] is False


def test_failure_does_not_advance_last_success():
    prior = (NOW - timedelta(hours=5)).isoformat()
    failed = model_registry.classify_discovery_run(
        {"status": "failed", "error": "http_500", "observed_at": NOW.isoformat()},
        now=NOW,
        prior_success_at=prior,
    )
    assert failed["state"] == "error"
    assert failed["last_success_at"] == prior
    assert failed["last_attempt_at"] == NOW.isoformat()
    never = model_registry.classify_discovery_run(
        {"status": "skipped", "error": "missing_key", "observed_at": NOW.isoformat()}, now=NOW
    )
    assert never["last_success_at"] is None


def test_ok_run_with_old_source_cache_is_stale():
    old = (NOW - timedelta(days=30)).isoformat()
    stale = model_registry.classify_discovery_run(
        {"status": "ok", "observed_at": NOW.isoformat(), "fetched_at": old}, now=NOW
    )
    assert stale["state"] == "stale" and stale["latest_confirmed"] is False
    assert stale["source_fetched_at"] == old
    fresh = model_registry.classify_discovery_run(
        {"status": "ok", "observed_at": NOW.isoformat(), "fetched_at": (NOW - timedelta(hours=1)).isoformat()},
        now=NOW,
    )
    assert fresh["state"] == "ok" and fresh["latest_confirmed"] is True


def test_old_ok_run_becomes_stale_at_read_time():
    run = {"status": "ok", "observed_at": (NOW - timedelta(days=10)).isoformat()}
    assert model_registry.classify_discovery_run(run, now=NOW)["state"] == "stale"


def test_unavailable_source_is_error():
    run = model_registry.classify_discovery_run(
        {"status": "unavailable", "error": "not_found:/x", "observed_at": NOW.isoformat()}, now=NOW
    )
    assert run["state"] == "error" and run["reason_class"] == "source_unavailable"


def test_summary_partial_when_any_provider_not_ok():
    runs = [
        {"provider": "openai", "state": "ok"},
        {"provider": "anthropic", "state": "skipped"},
        {"provider": "kimi", "state": "error"},
    ]
    summary = model_registry.summarize_discovery_states(runs)
    assert summary["status"] == "partial" and summary["discovery_complete"] is False
    assert summary["skipped"] == ["anthropic"] and summary["error"] == ["kimi"]
    assert model_registry.summarize_discovery_states([{"provider": "a", "state": "ok"}])["status"] == "ok"
    assert model_registry.summarize_discovery_states([{"provider": "a", "state": "skipped"}])["status"] == "error"


@pytest.mark.asyncio
async def test_discovery_stamps_confirmation_only_on_ok_providers(monkeypatch):
    async def ok_deepseek():
        return (
            [{"model_id": "deepseek-v9-brandnew", "display_name": "x", "raw": {}}],
            {"status": "ok", "discovery_source": "deepseek_api", "auto_discovery_supported": True},
        )

    async def skipped():
        return [], {"status": "skipped", "error": "google_routes_disabled"}

    for name in ("_fetch_openai_models", "_fetch_anthropic_models", "_fetch_gemini_models",
                 "_fetch_litellm_models", "_fetch_kimi_models", "_fetch_codex_cli_models"):
        monkeypatch.setattr(model_registry, name, skipped)
    monkeypatch.setattr(model_registry, "_fetch_deepseek_models", ok_deepseek)
    rows, runs = await model_registry.discover_provider_model_rows([], enabled=True)
    by_provider = {run["provider"]: run for run in runs}
    assert by_provider["deepseek"]["state"] == "ok" and by_provider["deepseek"]["latest_confirmed"] is True
    assert by_provider["gemini"]["state"] == "skipped" and by_provider["gemini"]["latest_confirmed"] is False
    assert by_provider["deepseek"]["last_success_at"] == by_provider["deepseek"]["observed_at"]
    assert rows[0]["metadata"]["catalog_confirmed_at"] == by_provider["deepseek"]["observed_at"]


def test_disabled_discovery_run_is_classified_skipped():
    import asyncio

    _, runs = asyncio.run(model_registry.discover_provider_model_rows([], enabled=False))
    assert runs[0]["state"] == "skipped" and runs[0]["reason_class"] == "discovery_disabled"
    assert runs[0]["latest_confirmed"] is False


def _deepseek_key_rows():
    return [{
        "id": 1, "provider": "deepseek", "key_name": "DEEPSEEK_API_KEY", "priority": 1, "is_active": True,
        "rate_limited_until": None, "last_used_at": None, "last_verified_at": None,
    }]


def test_new_deepseek_model_is_not_exposed_before_verification_and_current_models_kept():
    key_state = model_registry._build_key_state(_deepseek_key_rows())
    templates, _ = model_registry.build_registry_snapshots(_deepseek_key_rows())
    discovered = [
        model_registry._discovered_model_row(
            provider="deepseek", model_id=model_id, display_name=model_id, key_state=key_state,
            raw={}, source="deepseek_api",
        )
        for model_id in ("deepseek-v4-pro", "deepseek-v4-flash", "deepseek-v9-brandnew")
    ]
    merged = {
        row["model_id"]: row
        for row in model_registry._merge_model_rows(templates, discovered)
        if row["provider"] == "deepseek"
    }
    assert merged["deepseek-v9-brandnew"]["is_active"] is False
    assert merged["deepseek-v9-brandnew"]["is_selectable"] is False
    assert merged["deepseek-v9-brandnew"]["is_executable"] is False
    assert merged["deepseek-v9-brandnew"]["verification_status"] == "review_required"
    assert "deepseek-v4-flash" not in merged
    # 이번 목록에 없는 deepseek-flash 는 템플릿 행으로 유지된다 (일시 누락 != 폐기)
    assert merged["deepseek-flash"]["is_active"] is True
    assert merged["deepseek-v4-pro"]["is_active"] is True


def test_deepseek_discovery_counts_as_auto_discovery_when_key_present():
    key_state = model_registry._build_key_state(_deepseek_key_rows())
    assert model_registry._provider_auto_discovery_supported("deepseek", key_state["deepseek"]) is True
    assert model_registry._provider_auto_discovery_supported("deepseek", {}) is False


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


class _SyncConn:
    def __init__(self):
        self.execute_calls = []

    def transaction(self):
        return _Tx()

    async def fetch(self, *_args):
        return []

    async def execute(self, query, *args):
        self.execute_calls.append((query, args))
        return "OK"


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class _Acquire:
            async def __aenter__(self_inner):
                return conn

            async def __aexit__(self_inner, *_exc):
                return False

        return _Acquire()


@pytest.mark.asyncio
async def test_sync_keeps_write_ok_but_reports_partial_and_records_provider_status(monkeypatch):
    conn = _SyncConn()
    prior = "2026-11-03T06:25:00+00:00"
    runs = [
        {"provider": "openai", "status": "ok", "count": 3, "active_count": 3, "error": "",
         "observed_at": NOW.isoformat(), **model_registry.classify_discovery_run(
             {"status": "ok", "observed_at": NOW.isoformat()}, now=NOW)},
        {"provider": "gemini", "status": "skipped", "count": 0, "active_count": 0, "error": "google_routes_disabled",
         "observed_at": NOW.isoformat(), **model_registry.classify_discovery_run(
             {"status": "skipped", "error": "google_routes_disabled", "observed_at": NOW.isoformat()},
             now=NOW)},
    ]
    audit = AsyncMock()
    monkeypatch.setattr(model_registry, "get_pool", lambda: _Pool(conn))
    monkeypatch.setattr(model_registry, "_fetch_key_rows", AsyncMock(return_value=[]))
    monkeypatch.setattr(model_registry, "_fetch_last_discovery_success", AsyncMock(return_value={"gemini": prior}))
    monkeypatch.setattr(model_registry, "discover_provider_model_rows", AsyncMock(return_value=([], runs)))
    monkeypatch.setattr(model_registry, "append_key_audit_log", audit)
    monkeypatch.setattr(model_registry.cli_model_autoreg, "fetch_verified_codex_ids", AsyncMock(return_value=set()))
    monkeypatch.setattr(model_registry.cli_model_autoreg, "autoregister_after_sync", AsyncMock(return_value={}))

    result = await model_registry.sync_model_registry(triggered_by="test", reason="unit")

    assert result["ok"] is True
    assert result["discovery_status"] == "partial"
    assert result["discovery_complete"] is False
    status = {item["provider"]: item for item in result["provider_status"]}
    assert status["openai"]["latest_confirmed"] is True
    assert status["gemini"]["state"] == "skipped" and status["gemini"]["latest_confirmed"] is False
    assert status["gemini"]["last_success_at"] == prior
    assert status["gemini"]["reason_class"] == "route_policy_disabled"
    details = audit.await_args.kwargs["details"]
    assert details["discovery_summary"]["skipped"] == ["gemini"]
    assert details["provider_status"] == result["provider_status"]
    stored = [json.loads(args[5]) for query, args in conn.execute_calls if "llm_model_discovery_runs" in query]
    assert {item["provider"]: item["state"] for item in stored} == {"openai": "ok", "gemini": "skipped"}


@pytest.mark.asyncio
async def test_provider_summary_api_exposes_freshness_without_mutating_cache(monkeypatch):
    from app.api import llm_models

    cached = [{"provider": "anthropic", "active_model_count": 1}, {"provider": "openai", "active_model_count": 2}]
    freshness = {
        "anthropic": model_registry.classify_discovery_run(
            {"status": "skipped", "error": "oauth_runtime_only_models_api_unavailable", "observed_at": NOW.isoformat()},
            now=NOW),
        "openai": model_registry.classify_discovery_run({"status": "ok", "observed_at": NOW.isoformat()}, now=NOW),
    }
    monkeypatch.setattr(llm_models, "list_provider_summaries", AsyncMock(return_value=cached))
    monkeypatch.setattr(llm_models, "_fetch_last_registry_sync", AsyncMock(return_value=None))
    monkeypatch.setattr(llm_models, "list_provider_discovery_freshness", AsyncMock(return_value=freshness))

    result = await llm_models.get_provider_summary()

    assert result["latest_confirmed_providers"] == ["openai"]
    assert result["not_latest_confirmed_providers"] == ["anthropic"]
    anthropic = next(row for row in result["providers"] if row["provider"] == "anthropic")
    assert anthropic["discovery_freshness"]["manual_registration_required"] is True
    assert "discovery_freshness" not in cached[0]


# ── runner-f29de7d2 리뷰 지적 회귀 ─────────────────────────────────────────────

CACHE_RUN = {"status": "ok", "discovery_source": "codex_cli_cache", "source_path": "/x/models_cache.json"}


@pytest.mark.parametrize("fetched_at,reason", [(None, "cache_timestamp_missing"), ("", "cache_timestamp_missing"),
                                               ("not-a-date", "cache_timestamp_invalid")])
def test_cache_run_without_valid_fetched_at_is_not_latest_confirmed(fetched_at, reason):
    run = {**CACHE_RUN, "observed_at": NOW.isoformat(), "fetched_at": fetched_at}
    result = model_registry.classify_discovery_run(run, now=NOW)
    assert result["latest_confirmed"] is False
    assert result["state"] == "stale"
    assert result["reason_class"] == reason
    assert result["source_fetched_at"] == fetched_at


def test_cache_run_with_valid_fetched_at_stays_confirmed_and_api_run_needs_none():
    ok = model_registry.classify_discovery_run(
        {**CACHE_RUN, "observed_at": NOW.isoformat(), "fetched_at": (NOW - timedelta(hours=1)).isoformat()}, now=NOW
    )
    assert ok["latest_confirmed"] is True and ok["reason_class"] == ""
    api = model_registry.classify_discovery_run({"status": "ok", "observed_at": NOW.isoformat()}, now=NOW)
    assert api["latest_confirmed"] is True


class _FreshnessConn:
    def __init__(self, latest, success):
        self._latest, self._success = latest, success

    async def fetch(self, query, *_args):
        return self._latest if "DISTINCT ON" in query else self._success


def _patch_pool(monkeypatch, module, conn):
    monkeypatch.setattr(module, "get_pool", lambda: _Pool(conn))


def _db_row(provider, status, created_at, error="", details=None):
    return {"provider": provider, "status": status, "error": error, "details": json.dumps(details or {}),
            "created_at": created_at}


@pytest.mark.asyncio
async def test_global_disable_after_success_clears_latest_confirmed_and_hides_all(monkeypatch):
    now = datetime.now(timezone.utc)
    success_at = now - timedelta(hours=10)
    latest = [
        _db_row("all", "skipped", now - timedelta(hours=1), "disabled"),
        _db_row("openai", "ok", success_at),
        _db_row("kimi", "ok", now - timedelta(minutes=5)),
    ]
    success = [{"provider": "openai", "last_success_at": success_at}, {"provider": "kimi", "last_success_at": latest[2]["created_at"]}]
    _patch_pool(monkeypatch, model_registry, _FreshnessConn(latest, success))
    result = await model_registry.list_provider_discovery_freshness()
    assert "all" not in result
    assert result["openai"]["latest_confirmed"] is False
    assert result["openai"]["reason_class"] == "discovery_disabled"
    assert result["openai"]["last_success_at"] == success_at.isoformat()
    assert result["kimi"]["latest_confirmed"] is True


@pytest.mark.asyncio
async def test_discovery_disabled_emits_per_provider_runs_not_virtual_all():
    _, runs = await model_registry.discover_provider_model_rows([], enabled=False)
    providers = {run["provider"] for run in runs}
    assert "all" not in providers
    assert {"openai", "anthropic", "gemini", "litellm", "kimi", "deepseek", "codex"} <= providers
    assert all(run["state"] == "skipped" and run["reason_class"] == "discovery_disabled"
               and run["latest_confirmed"] is False for run in runs)
    assert model_registry.summarize_discovery_states(runs)["discovery_complete"] is False


@pytest.mark.asyncio
async def test_discovery_runs_api_passes_prior_success_to_freshness(monkeypatch):
    from app.api import llm_models

    prior = (NOW - timedelta(hours=7)).isoformat()
    row = {"id": 1, "provider": "openai", "status": "failed", "discovered_count": 0, "active_count": 0,
           "error": "http_500", "details": json.dumps({"last_success_at": prior}), "triggered_by": "t",
           "reason": "r", "created_at": NOW}
    class _RunsConn:
        async def fetch(self, *_args):
            return [row]

    _patch_pool(monkeypatch, llm_models, _RunsConn())
    result = await llm_models.list_model_discovery_runs(limit=20)
    freshness = result["runs"][0]["freshness"]
    assert freshness["state"] == "error"
    assert freshness["last_success_at"] == prior


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [{}, {"error": {"message": "bad key"}}, {"data": "oops"}, {"data": None}, [], "x",
                                     {"object": "list", "data": [], "error": {"code": "x"}}])
async def test_fetch_deepseek_200_with_invalid_schema_is_failure(monkeypatch, payload):
    _patch_deepseek_http(monkeypatch, payload=payload)
    rows, result = await model_registry._fetch_deepseek_models()
    assert rows == []
    assert result["status"] == "failed" and result["error"] == "schema_invalid"
    run = {"provider": "deepseek", "observed_at": NOW.isoformat(), **result}
    assert model_registry.classify_discovery_run(run, now=NOW)["latest_confirmed"] is False


@pytest.mark.asyncio
async def test_fetch_deepseek_empty_list_is_valid(monkeypatch):
    _patch_deepseek_http(monkeypatch, payload={"object": "list", "data": []})
    rows, result = await model_registry._fetch_deepseek_models()
    assert rows == [] and result["status"] == "ok"


class _AnyHttpClient:
    status = 401

    def __init__(self, **_kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def get(self, url, headers=None, params=None):
        full = httpx.URL(url, params=params) if params else httpx.URL(url)
        return httpx.Response(self.status, json={}, request=httpx.Request("GET", full))


@pytest.mark.asyncio
@pytest.mark.parametrize("status,reason", [(401, "auth_rejected"), (403, "auth_rejected"),
                                           (429, "rate_limited"), (500, "fetch_failed")])
@pytest.mark.parametrize("provider", ["openai", "anthropic", "gemini", "litellm", "kimi", "deepseek"])
async def test_every_provider_http_error_is_normalized(monkeypatch, provider, status, reason):
    skipped = AsyncMock(return_value=([], {"status": "skipped", "error": "missing_key"}))
    for name in ("_fetch_openai_models", "_fetch_anthropic_models", "_fetch_gemini_models", "_fetch_litellm_models",
                 "_fetch_kimi_models", "_fetch_deepseek_models", "_fetch_codex_cli_models"):
        if name != f"_fetch_{provider}_models":
            monkeypatch.setattr(model_registry, name, skipped)
    monkeypatch.setattr(model_registry, "_get_first_provider_key", AsyncMock(return_value="sk-test-not-real"))
    monkeypatch.setattr(model_registry, "_google_commercial_routes_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(model_registry, "_get_anthropic_models_api_key_and_runtime_state",
                        AsyncMock(return_value=("sk-test-not-real", True)))
    client = type("Client", (_AnyHttpClient,), {"status": status})
    monkeypatch.setattr(model_registry.httpx, "AsyncClient", client)
    _, runs = await model_registry.discover_provider_model_rows([], enabled=True)
    run = next(item for item in runs if item["provider"] == provider)
    assert run["status"] == "failed"
    assert run["error"] == f"http_{status}"
    assert run["reason_class"] == reason
    assert run["latest_confirmed"] is False
    assert "sk-test-not-real" not in json.dumps(run)
