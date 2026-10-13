"""폴백 시 실제로 답한 모델이 done/model_info 와 model_fallback 이벤트에 기록되는지 검증."""
from __future__ import annotations

import pytest

from app.services import model_selector
from tests.unit.test_antigravity_host_relay import _patch_routing, _row, _run

AGY_MODEL = "gemini-3.1-pro-high"
CODEX_FALLBACK = "gpt-5.5"


@pytest.mark.asyncio
async def test_agy_failure_labels_codex_as_actual_model(monkeypatch):
    calls = []
    _patch_routing(
        monkeypatch, [_row("antigravity", AGY_MODEL)], calls,
        antigravity_events=[{"type": "error", "content": "Antigravity CLI timeout"}],
        codex_events=[
            {"type": "model_info", "model": "GPT-5.5 (Codex CLI)"},
            {"type": "delta", "content": "codex-ok"},
            {"type": "done", "model": "GPT-5.5 (Codex CLI)"},
        ],
    )
    events = await _run(AGY_MODEL)

    assert calls == [("antigravity", AGY_MODEL), ("codex", CODEX_FALLBACK)]

    done = [e for e in events if e["type"] == "done"]
    assert len(done) == 1
    assert done[0]["model"] == CODEX_FALLBACK
    assert done[0]["actual_model"] == CODEX_FALLBACK
    assert all(e["model"] == CODEX_FALLBACK for e in events if e["type"] == "model_info")
    assert not any(e.get("model") == AGY_MODEL for e in events)

    switched = [e for e in events if e["type"] == "model_fallback"]
    assert len(switched) == 1
    assert switched[0]["from_model"] == AGY_MODEL
    assert switched[0]["to_model"] == CODEX_FALLBACK
    assert {"stage", "stage_label", "content"} <= set(switched[0])

    assert any(f"[{AGY_MODEL} (Antigravity) 오류 → Codex CLI {CODEX_FALLBACK} 전환]" in str(e.get("content", "")) for e in events)


@pytest.mark.asyncio
async def test_agy_success_keeps_requested_model_and_no_fallback_event(monkeypatch):
    calls = []
    _patch_routing(monkeypatch, [_row("antigravity", AGY_MODEL)], calls)
    events = await _run(AGY_MODEL)

    assert calls == [("antigravity", AGY_MODEL)]
    assert events[-1]["type"] == "done" and events[-1]["model"] == AGY_MODEL
    assert not any(e["type"] == "model_fallback" for e in events)


@pytest.mark.asyncio
async def test_codex_fallback_helper_never_rewrites_to_requested_model(monkeypatch):
    async def codex(model, *_a, **_k):
        yield {"type": "model_info", "model": "display"}
        yield {"type": "done", "model": "display", "cost": "0"}

    monkeypatch.setattr(model_selector, "_stream_codex_relay", codex)
    events = [e async for e in model_selector._stream_codex_as_fallback(
        "deepseek-chat", CODEX_FALLBACK, "sys", [{"role": "user", "content": "hi"}],
    )]

    assert [e["type"] for e in events] == ["model_fallback", "model_info", "done"]
    assert events[0]["from_model"] == "deepseek-chat" and events[0]["to_model"] == CODEX_FALLBACK
    assert events[-1]["model"] == CODEX_FALLBACK
