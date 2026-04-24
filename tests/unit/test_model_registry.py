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
