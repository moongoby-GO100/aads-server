"""스트림 재시도가 모델을 바꿀 때 조용히 바꿔치기하지 않는다.

2026-10-02 실측(세션 9102c970): 사용자가 openai:gpt-6.1-sol 을 골랐는데 첫 시도가
도구 호출 불가로 죽자 재시도가 claude-fable-5-1 로 바뀌었고, fallback_chain 은 비었고,
배너도 없었고, model_selector 는 "user explicitly selected 'claude-fable-5-1'" 로 적었다.
"""
from __future__ import annotations

import logging

import pytest

from app.services import chat_service, model_selector
from app.services.intent_router import IntentResult


def test_openai_request_retries_on_same_model_codex_row_first():
    chain = chat_service._cross_provider_chat_fallback_chain("openai:gpt-6.1-sol")
    assert chain[:3] == ["openai:gpt-6.1-sol", "codex:gpt-6.1-sol", "claude-fable-5-1"]
    assert chat_service._cross_provider_chat_fallback_chain("codex:gpt-6.1-sol")[1] == "claude-fable-5-1"


@pytest.mark.asyncio
async def test_codex_candidate_kept_only_when_registered_row_is_runnable(monkeypatch):
    rows = {}

    async def _row(model_id, provider=None):
        assert provider == "codex"
        return rows.get(model_id)

    monkeypatch.setattr(model_selector, "_get_registered_model_row", _row)
    base = "openai:gpt-6.1-sol"
    chain = chat_service._cross_provider_chat_fallback_chain(base)

    rows["gpt-6.1-sol"] = {"provider": "codex", "model_id": "gpt-6.1-sol", "is_active": True, "is_executable": True}
    kept = await chat_service._drop_unregistered_codex_retry_candidates(chain, base)
    assert kept[:2] == [base, "codex:gpt-6.1-sol"]

    rows.clear()
    kept = await chat_service._drop_unregistered_codex_retry_candidates(chain, base)
    assert "codex:gpt-6.1-sol" not in kept
    assert kept[:2] == [base, "claude-fable-5-1"]


def test_retry_model_switch_records_chain_and_banner():
    chain: list = []
    banner = chat_service._note_retry_model_switch(
        chain,
        prev_model="openai:gpt-6.1-sol",
        new_model="codex:gpt-6.1-sol",
        base_model="openai:gpt-6.1-sol",
        reason="요청 오류",
    )
    assert len(chain) == 1
    assert set(chain[0]) == {"from", "to", "reason", "at"}
    assert chain[0]["from"] == "openai:gpt-6.1-sol"
    assert chain[0]["to"] == "codex:gpt-6.1-sol"
    assert chain[0]["reason"] == "요청 오류"
    assert banner.strip() == "[openai:gpt-6.1-sol 실행 불가 → codex:gpt-6.1-sol 전환]"
    assert banner.startswith("\n\n[") and banner.endswith("]\n\n")


def test_retry_without_model_change_records_nothing():
    chain: list = []
    banner = chat_service._note_retry_model_switch(
        chain, prev_model="claude-fable-5-1", new_model="Claude-Fable-5-1",
        base_model="claude-fable-5-1", reason=None,
    )
    assert chain == []
    assert banner == ""


def test_retry_returning_to_requested_model_has_no_banner_but_is_recorded():
    chain: list = []
    banner = chat_service._note_retry_model_switch(
        chain, prev_model="claude-fable-5-1", new_model="gpt-6-astra",
        base_model="gpt-6-astra", reason="시간 초과",
    )
    assert len(chain) == 1
    assert banner == ""


@pytest.mark.asyncio
async def test_retry_override_is_not_logged_as_user_selection(monkeypatch, caplog):
    async def _no_key(*_a, **_k):
        return ""

    async def _models():
        return {"claude-fable-5-1"}

    async def _row(model_id, provider=None):
        return {"provider": "anthropic", "model_id": "claude-fable-5-1", "is_active": True, "is_executable": True,
                "metadata": {}}

    async def _slots(project: str = ""):
        return {}

    async def _stream(*_a, **_k):
        yield {"type": "done", "model": "claude-fable-5-1", "cost": "0", "input_tokens": 1, "output_tokens": 1}

    monkeypatch.setattr(model_selector, "_get_db_key", _no_key)
    monkeypatch.setattr(model_selector, "get_available_model_ids", _models)
    monkeypatch.setattr(model_selector, "_get_registered_model_row", _row)
    monkeypatch.setattr(model_selector, "_get_claude_slot_records", _slots)
    for name in ("_stream_cli_relay", "_stream_agent_sdk", "_stream_litellm"):
        monkeypatch.setattr(model_selector, name, _stream)

    def _intent():
        result = IntentResult(intent="cto_strategy", model="gpt-6.1-sol", use_tools=True, tool_group="all")
        result.model_locked = True
        return result

    async def _run(**kwargs):
        return [
            e async for e in model_selector.call_stream(
                _intent(), "system", [{"role": "user", "content": "x"}],
                model_override="claude-fable-5-1", **kwargs,
            )
        ]

    with caplog.at_level(logging.INFO, logger=model_selector.logger.name):
        await _run(retry_override=True)
    messages = [r.getMessage() for r in caplog.records]
    assert any("cascade_skip: retry_override 'claude-fable-5-1'" in m for m in messages)
    assert not any("user explicitly selected" in m for m in messages)

    caplog.clear()
    with caplog.at_level(logging.INFO, logger=model_selector.logger.name):
        await _run()
    assert any("user explicitly selected 'claude-fable-5-1'" in r.getMessage() for r in caplog.records)
