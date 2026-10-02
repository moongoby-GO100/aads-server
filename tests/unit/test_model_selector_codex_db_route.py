"""DB 에 검증된 codex_cli 행이 있으면 provider 미지정 선택은 구독 CLI 로 간다.

2026-10-02: gpt-6.1-sol 이 하드코딩 _CODEX_MODELS 에 없어 openai 직결 행이 잡혔고,
도구 턴마다 "[gpt-6.1-sol 실행 불가 → Codex CLI gpt-5.6-sol 전환]" 으로 강등됐다.
"""
from __future__ import annotations

import pytest

from app.services import model_selector


def _rows(model_id: str, *, codex_status: str = "verified", codex_backend: str = "codex_cli", **codex_extra):
    return [
        {"provider": "openai", "model_id": model_id, "is_active": True, "is_executable": True,
         "verification_status": "discovered",
         "metadata": {"execution_backend": "openai_compatible_direct"}},
        {"provider": "codex", "model_id": model_id, "is_active": True, "is_executable": True,
         "verification_status": codex_status, "metadata": {"execution_backend": codex_backend},
         **codex_extra},
    ]


def _patch(monkeypatch, rows):
    async def registered(active_only=True):
        return rows
    monkeypatch.setattr(model_selector, "_list_registered_models", registered)


def test_gpt61sol_is_codex_model():
    assert "gpt-6.1-sol" in model_selector._CODEX_MODELS
    assert model_selector._CODEX_MODEL_DISPLAY["gpt-6.1-sol"] == "GPT-6.1 Sol (Codex CLI)"


@pytest.mark.asyncio
async def test_gpt61sol_without_provider_prefers_codex_row(monkeypatch):
    _patch(monkeypatch, _rows("gpt-6.1-sol"))
    row = await model_selector._get_registered_model_row("gpt-6.1-sol")
    assert row["provider"] == "codex"


@pytest.mark.asyncio
async def test_db_only_verified_codex_model_prefers_codex_row(monkeypatch):
    assert "gpt-9-test" not in model_selector._CODEX_MODELS
    _patch(monkeypatch, _rows("gpt-9-test"))
    row = await model_selector._get_registered_model_row("gpt-9-test")
    assert row["provider"] == "codex"
    assert model_selector._route_metadata(row)["execution_backend"] == "codex_cli"


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs", [
    {"codex_status": "discovered"},
    {"codex_backend": "openai_compatible_direct"},
    {"is_executable": False},
    {"retired_at": "2026-10-01T00:00:00+00:00"},
])
async def test_unverified_or_unrunnable_codex_row_is_not_preferred(monkeypatch, kwargs):
    _patch(monkeypatch, _rows("gpt-9-test", **kwargs))
    row = await model_selector._get_registered_model_row("gpt-9-test")
    assert row["provider"] == "openai"


@pytest.mark.asyncio
async def test_explicit_openai_provider_keeps_openai_row(monkeypatch):
    _patch(monkeypatch, _rows("gpt-6.1-sol"))
    row = await model_selector._get_registered_model_row("gpt-6.1-sol", provider="openai")
    assert row["provider"] == "openai"
