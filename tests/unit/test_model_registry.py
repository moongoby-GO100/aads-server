from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.services import model_registry


def test_build_registry_snapshots_normalizes_provider_and_activates_models():
    now = datetime.now(timezone.utc)
    model_rows, provider_rows = model_registry.build_registry_snapshots(
        [
            {
                "id": 1,
                "provider": "alibaba",
                "key_name": "ALIBABA_API_KEY",
                "priority": 1,
                "is_active": True,
                "rate_limited_until": None,
                "last_used_at": now,
                "last_verified_at": now,
            }
        ]
    )

    qwen_summary = next(row for row in provider_rows if row["provider"] == "qwen")
    assert qwen_summary["status"] == "active"
    assert qwen_summary["active_key_count"] == 1
    assert qwen_summary["available_key_count"] == 1
    assert qwen_summary["active_model_count"] > 0

    active_qwen_models = [row for row in model_rows if row["provider"] == "qwen" and row["is_active"]]
    assert active_qwen_models
    assert all(row["linked_key_name"] == "ALIBABA_API_KEY" for row in active_qwen_models)


def test_build_registry_snapshots_registers_deepseek_v4_and_alias_metadata():
    now = datetime.now(timezone.utc)
    model_rows, provider_rows = model_registry.build_registry_snapshots(
        [
            {
                "id": 10,
                "provider": "deepseek",
                "key_name": "DEEPSEEK_API_KEY",
                "priority": 1,
                "is_active": True,
                "rate_limited_until": None,
                "last_used_at": now,
                "last_verified_at": now,
            }
        ]
    )

    deepseek_models = {row["model_id"]: row for row in model_rows if row["provider"] == "deepseek"}
    assert {"deepseek-v4-flash", "deepseek-v4-pro", "deepseek-chat", "deepseek-reasoner"} <= set(deepseek_models)
    assert deepseek_models["deepseek-v4-pro"]["supports_thinking"] is True
    assert deepseek_models["deepseek-v4-flash"]["metadata"]["execution_model_id"] == "deepseek-v4-flash"
    assert deepseek_models["deepseek-chat"]["metadata"]["canonical_model"] == "deepseek-v4-flash"
    assert deepseek_models["deepseek-chat"]["metadata"]["deprecation_date"] == "2026-07-24"
    assert deepseek_models["deepseek-chat"]["metadata"]["compatibility_alias"] is True
    assert deepseek_models["deepseek-chat"]["execution_model_id"] == "deepseek-v4-flash"

    deepseek_summary = next(row for row in provider_rows if row["provider"] == "deepseek")
    assert deepseek_summary["runtime_executable"] is True
    assert deepseek_summary["active_model_count"] == 4
    assert deepseek_summary["active_model_source"] == "template"


def test_build_registry_snapshots_marks_anthropic_oauth_as_runtime_only_discovery():
    now = datetime.now(timezone.utc)
    model_rows, provider_rows = model_registry.build_registry_snapshots(
        [
            {
                "id": 20,
                "provider": "anthropic",
                "key_name": "ANTHROPIC_AUTH_TOKEN",
                "priority": 1,
                "is_active": True,
                "rate_limited_until": None,
                "last_used_at": now,
                "last_verified_at": now,
            }
        ]
    )

    anthropic_summary = next(row for row in provider_rows if row["provider"] == "anthropic")
    assert anthropic_summary["runtime_executable"] is True
    assert anthropic_summary["auto_discovery_supported"] is False
    assert anthropic_summary["discovery_mode"] == "template_runtime_only"
    assert "x-api-key required" in anthropic_summary["discovery_requirement"]

    claude_row = next(row for row in model_rows if row["provider"] == "anthropic" and row["model_id"] == "claude-sonnet")
    assert claude_row["metadata"]["model_source"] == "template"
    assert claude_row["metadata"]["runtime_executable"] is True
    assert claude_row["metadata"]["auto_discovery_supported"] is False
    assert claude_row["metadata"]["accepted_aliases"] == [
        "claude-sonnet-4-6",
        "claude-sonnet-4-5",
        "claude-3-5-sonnet-20241022",
        "claude-3-sonnet-20240229",
        "claude-2.1",
    ]
    assert claude_row["execution_model_id"] == "claude-sonnet-4-6"


def test_build_registry_snapshots_marks_unknown_provider_for_review():
    _, provider_rows = model_registry.build_registry_snapshots(
        [
            {
                "id": 7,
                "provider": "custom-llm",
                "key_name": "CUSTOM_API_KEY",
                "priority": 1,
                "is_active": True,
                "rate_limited_until": datetime.now(timezone.utc) + timedelta(minutes=5),
                "last_used_at": None,
                "last_verified_at": None,
            }
        ]
    )

    custom_summary = next(row for row in provider_rows if row["provider"] == "custom-llm")
    assert custom_summary["requires_admin_review"] is True
    assert custom_summary["template_available"] is False
    assert custom_summary["status"] == "review_required"


@pytest.mark.asyncio
async def test_filter_executable_models_respects_registry(monkeypatch):
    async def _executable_ids():
        return {"gpt-4o"}

    monkeypatch.setattr(model_registry, "get_executable_model_ids", _executable_ids)
    models = await model_registry.filter_executable_models(["gpt-4o", "deepseek-chat"])
    assert models == ["gpt-4o"]


@pytest.mark.asyncio
async def test_filter_executable_models_normalizes_prefixed_ids_and_version_suffixes(monkeypatch):
    async def _executable_ids():
        return {"gpt-5.3-codex", "claude-sonnet", "gemini-2.5-flash"}

    monkeypatch.setattr(model_registry, "get_executable_model_ids", _executable_ids)
    models = await model_registry.filter_executable_models(
        [
            "codex:gpt-5.3-codex",
            "claude:claude-sonnet-4-6",
            "litellm:gemini-2.5-flash",
            "deepseek-chat",
        ]
    )
    assert models == [
        "codex:gpt-5.3-codex",
        "claude:claude-sonnet-4-6",
        "litellm:gemini-2.5-flash",
    ]


@pytest.mark.asyncio
async def test_filter_executable_models_accepts_deepseek_compatibility_alias(monkeypatch):
    async def _executable_ids():
        return {"deepseek-v4-pro"}

    monkeypatch.setattr(model_registry, "get_executable_model_ids", _executable_ids)
    models = await model_registry.filter_executable_models(["deepseek-reasoner", "deepseek-chat"])
    assert models == ["deepseek-reasoner"]


@pytest.mark.asyncio
async def test_filter_executable_models_keeps_original_when_registry_empty(monkeypatch):
    async def _executable_ids():
        return set()

    monkeypatch.setattr(model_registry, "get_executable_model_ids", _executable_ids)
    models = await model_registry.filter_executable_models(["claude-sonnet", "deepseek-chat"])
    assert models == ["claude-sonnet", "deepseek-chat"]


def test_coerce_json_object_accepts_json_string():
    metadata = model_registry._coerce_json_object('{"execution_backend":"openai_compatible_direct","flag":true}')

    assert metadata == {
        "execution_backend": "openai_compatible_direct",
        "flag": True,
    }


@pytest.mark.asyncio
async def test_fetch_anthropic_models_reports_oauth_runtime_only(monkeypatch):
    async def _fake_key_records(_provider, include_rate_limited=False):
        assert _provider == "anthropic"
        assert include_rate_limited is False
        return [
            {
                "key_name": "ANTHROPIC_AUTH_TOKEN",
                "value": "sk-ant-oat01-test",
            }
        ]

    from app.core import llm_key_provider

    monkeypatch.setattr(llm_key_provider, "get_provider_key_records", _fake_key_records)

    rows, result = await model_registry._fetch_anthropic_models()

    assert rows == []
    assert result["status"] == "skipped"
    assert result["error"] == "oauth_runtime_only_models_api_unavailable"
    assert result["runtime_executable"] is True
    assert result["auto_discovery_supported"] is False
    assert "x-api-key required" in result["discovery_requirement"]
