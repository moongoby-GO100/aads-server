"""키는 있으나 수집기가 없던 회사의 공통 모델 목록 수집기 회귀 테스트."""
from __future__ import annotations

import json
import logging
from unittest.mock import AsyncMock

import httpx
import pytest

from app.services import model_registry

SECRET = "sk-test-secret-value-9f3a"
PROVIDERS = sorted(model_registry._OPENAI_COMPAT_DISCOVERY_BASE_URLS)
LISTED = {"object": "list", "data": [{"id": "model-a", "object": "model"}, {"id": "model-b", "object": "model"}]}


class _Client:
    calls: list[tuple[str, dict]] = []
    status = 200
    payload: object = LISTED
    error: Exception | None = None

    def __init__(self, **_kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def get(self, url, headers=None, params=None):
        type(self).calls.append((url, dict(headers or {})))
        if self.error:
            raise self.error
        return httpx.Response(self.status, json=self.payload, request=httpx.Request("GET", url))


def _install(monkeypatch, *, status=200, payload=LISTED, error=None, key=SECRET):
    client = type("Client", (_Client,), {"calls": [], "status": status, "payload": payload, "error": error})
    monkeypatch.setattr(model_registry.httpx, "AsyncClient", client)
    monkeypatch.setattr(model_registry, "_get_first_provider_key", AsyncMock(return_value=key))
    return client


def _key_rows(provider: str) -> list[dict]:
    return [{
        "id": 1, "provider": provider, "key_name": f"{provider.upper()}_API_KEY", "priority": 1,
        "is_active": True, "rate_limited_until": None, "last_used_at": None, "last_verified_at": None,
    }]


def test_discovery_table_covers_requested_providers_and_is_wired():
    assert set(PROVIDERS) == {"groq", "mistral", "openrouter", "minimax", "together", "cerebras", "sambanova"}
    for provider in PROVIDERS:
        assert provider in model_registry._DISCOVERY_PROVIDERS
        assert provider in model_registry._DISCOVERY_REQUIREMENTS
        assert model_registry._OPENAI_COMPAT_DISCOVERY_BASE_URLS[provider].startswith("https://")


def test_discovery_base_url_matches_execution_path_except_documented_minimax():
    for provider, base in model_registry._OPENAI_COMPAT_DISCOVERY_BASE_URLS.items():
        execution = model_registry._DIRECT_PROVIDER_BASE_URLS.get(provider)
        if execution and provider != "minimax":
            assert base == execution


def test_unconfirmed_providers_are_not_given_guessed_urls():
    assert "qwen" not in model_registry._OPENAI_COMPAT_DISCOVERY_BASE_URLS
    assert "nvidia" not in model_registry._OPENAI_COMPAT_DISCOVERY_BASE_URLS


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", PROVIDERS)
async def test_success_lists_models_with_bearer_key(monkeypatch, provider):
    client = _install(monkeypatch)
    rows, result = await model_registry._fetch_openai_compatible_models(provider)
    assert [row["model_id"] for row in rows] == ["model-a", "model-b"]
    assert result["status"] == "ok" and result["count"] == 2
    assert result["discovery_source"] == f"{provider}_api"
    url, headers = client.calls[0]
    assert url == f"{model_registry._OPENAI_COMPAT_DISCOVERY_BASE_URLS[provider]}/models"
    assert headers == {"Authorization": f"Bearer {SECRET}"}
    assert SECRET not in json.dumps(result)


@pytest.mark.asyncio
async def test_bare_array_response_is_accepted_and_duplicates_collapsed(monkeypatch):
    _install(monkeypatch, payload=[{"id": "x"}, {"id": "x"}, {"id": "y"}, {"nope": 1}, "junk"])
    rows, result = await model_registry._fetch_openai_compatible_models("together")
    assert [row["model_id"] for row in rows] == ["x", "y"]
    assert result["status"] == "ok"


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("status,reason", [(401, "auth_rejected"), (403, "auth_rejected"), (429, "rate_limited"), (500, "fetch_failed")])
async def test_http_error_is_failed_without_key_leak(monkeypatch, caplog, provider, status, reason):
    _install(monkeypatch, status=status, payload={})
    with caplog.at_level(logging.DEBUG):
        rows, result = await model_registry._fetch_openai_compatible_models(provider)
    assert rows == []
    assert result["status"] == "failed" and result["error"] == f"http_{status}"
    assert SECRET not in json.dumps(result) and SECRET not in caplog.text
    run = {"provider": provider, **result, "observed_at": "2026-11-05T00:00:00+00:00"}
    classified = model_registry.classify_discovery_run(run)
    assert classified["reason_class"] == reason and classified["latest_confirmed"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", PROVIDERS)
async def test_timeout_is_failed_without_key_leak(monkeypatch, caplog, provider):
    _install(monkeypatch, error=httpx.ReadTimeout(f"timed out Authorization: Bearer {SECRET}"))
    with caplog.at_level(logging.DEBUG):
        rows, result = await model_registry._fetch_openai_compatible_models(provider)
    assert rows == []
    assert result["status"] == "failed" and result["error"] == "fetch_error:ReadTimeout"
    assert SECRET not in json.dumps(result) and SECRET not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", PROVIDERS)
async def test_empty_list_is_valid_success(monkeypatch, provider):
    _install(monkeypatch, payload={"object": "list", "data": []})
    rows, result = await model_registry._fetch_openai_compatible_models(provider)
    assert rows == [] and result["status"] == "ok" and result["count"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [{}, {"data": None}, {"data": "oops"}, {"error": {"message": "bad"}}, "x", 3,
                                     {"data": [], "error": {"code": "x"}}])
async def test_unexpected_body_is_failure_not_empty_success(monkeypatch, payload):
    _install(monkeypatch, payload=payload)
    rows, result = await model_registry._fetch_openai_compatible_models("groq")
    assert rows == [] and result["status"] == "failed" and result["error"] == "schema_invalid"


@pytest.mark.asyncio
async def test_non_json_body_is_schema_invalid(monkeypatch):
    class _Html(_Client):
        async def get(self, url, headers=None, params=None):
            return httpx.Response(200, content=b"<html>", request=httpx.Request("GET", url))

    monkeypatch.setattr(model_registry.httpx, "AsyncClient", _Html)
    monkeypatch.setattr(model_registry, "_get_first_provider_key", AsyncMock(return_value=SECRET))
    rows, result = await model_registry._fetch_openai_compatible_models("cerebras")
    assert rows == [] and result["error"] == "schema_invalid"


@pytest.mark.asyncio
async def test_missing_key_is_skipped_and_makes_no_request(monkeypatch):
    client = _install(monkeypatch, key="")
    rows, result = await model_registry._fetch_openai_compatible_models("sambanova")
    assert rows == [] and result["status"] == "skipped" and result["error"] == "missing_key"
    assert result["auto_discovery_supported"] is False
    assert client.calls == []


@pytest.mark.asyncio
async def test_one_provider_failure_does_not_stop_others(monkeypatch):
    skipped = AsyncMock(return_value=([], {"status": "skipped", "error": "missing_key"}))
    for name in ("_fetch_openai_models", "_fetch_anthropic_models", "_fetch_gemini_models", "_fetch_litellm_models",
                 "_fetch_kimi_models", "_fetch_deepseek_models", "_fetch_codex_cli_models"):
        monkeypatch.setattr(model_registry, name, skipped)
    _install(monkeypatch)

    class _Mixed(_Client):
        async def get(self, url, headers=None, params=None):
            if "groq" in url:
                raise RuntimeError("boom")
            if "mistral" in url:
                return httpx.Response(401, json={}, request=httpx.Request("GET", url))
            return httpx.Response(200, json=LISTED, request=httpx.Request("GET", url))

    monkeypatch.setattr(model_registry.httpx, "AsyncClient", _Mixed)
    rows, runs = await model_registry.discover_provider_model_rows([], enabled=True)
    by_provider = {run["provider"]: run for run in runs}
    assert by_provider["groq"]["status"] == "failed"
    assert by_provider["mistral"]["status"] == "failed" and by_provider["mistral"]["error"] == "http_401"
    for provider in set(PROVIDERS) - {"groq", "mistral"}:
        assert by_provider[provider]["status"] == "ok" and by_provider[provider]["count"] == 2
        assert by_provider[provider]["latest_confirmed"] is True
    assert {row["provider"] for row in rows} == set(PROVIDERS) - {"groq", "mistral"}
    assert SECRET not in json.dumps(runs, default=str)


@pytest.mark.parametrize("provider", PROVIDERS)
def test_discovered_models_ride_existing_activation_judgement(provider):
    key_state = model_registry._build_key_state(_key_rows(provider))
    row = model_registry._discovered_model_row(
        provider=provider, model_id="model-a", display_name="model-a", key_state=key_state,
        raw={}, source=f"{provider}_api",
    )
    assert row["is_active"] is False and row["is_executable"] is False
    assert row["verification_status"] == "review_required"
    assert row["metadata"]["auto_discovery_supported"] is True
    assert row["metadata"]["discovery_mode"] == "discovery"


@pytest.mark.parametrize("provider", PROVIDERS)
def test_auto_discovery_supported_requires_available_key(provider):
    key_state = model_registry._build_key_state(_key_rows(provider))
    assert model_registry._provider_auto_discovery_supported(provider, key_state[provider]) is True
    assert model_registry._provider_auto_discovery_supported(provider, {}) is False


def test_providers_without_collector_stay_unsupported():
    for provider in ("qwen", "nvidia"):
        key_state = model_registry._build_key_state(_key_rows(provider))
        assert model_registry._provider_auto_discovery_supported(provider, key_state[provider]) is False


def test_provider_outside_meta_reports_discovery_support_in_snapshot():
    _, provider_rows = model_registry.build_registry_snapshots(_key_rows("together") + _key_rows("nvidia"))
    summary = {row["provider"]: row for row in provider_rows}
    assert summary["together"]["auto_discovery_supported"] is True
    assert summary["nvidia"]["auto_discovery_supported"] is False


@pytest.mark.asyncio
async def test_disabled_discovery_records_every_new_provider():
    _, runs = await model_registry.discover_provider_model_rows([], enabled=False)
    assert set(PROVIDERS) <= {run["provider"] for run in runs}
