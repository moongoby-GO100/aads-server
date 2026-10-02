"""무출력 정지 방지 + Codex 401 인증 오류 표시 + 전환 기록.

2026-10-02 실측(세션 9102c970): Codex 가 401(refresh token revoked)로 끝났는데 로그에는
codex_empty_result 만 남았고, 예비 모델로 넘어간 폴백 SDK 경로는 model_info 뒤 35분간 멈췄으며,
실행 행에는 전환 기록이 없었다.
"""
from __future__ import annotations

import asyncio
import inspect
import json

import pytest

from app.services import chat_service, model_selector
from app.services.intent_router import IntentResult


# ── (i) 스톨 타임아웃 ────────────────────────────────────────────────────

async def _heartbeats_only():
    yield {"type": "model_info", "model": "claude-fable-5-1"}
    while True:
        yield {"type": "heartbeat"}
        await asyncio.sleep(0.01)


async def _drain(stream):
    return [e async for e in stream]


@pytest.mark.asyncio
async def test_heartbeats_and_model_info_do_not_reset_stall_clock():
    with pytest.raises(model_selector.StreamStallError) as exc:
        await asyncio.wait_for(
            _drain(model_selector.iter_with_stall_timeout(
                _heartbeats_only(), label="sdk", first_output_sec=0.15, idle_sec=0.15)),
            timeout=3,
        )
    assert "stream_stall_timeout" in str(exc.value)
    assert exc.value.phase == "첫 출력 대기"


@pytest.mark.asyncio
async def test_stall_can_be_reported_as_error_event_and_cancels_the_attempt():
    closed = {"v": False}

    async def _hang():
        try:
            yield {"type": "model_info", "model": "m"}
            await asyncio.sleep(3600)
        finally:
            closed["v"] = True

    events = await asyncio.wait_for(
        _drain(model_selector.iter_with_stall_timeout(
            _hang(), label="sdk", first_output_sec=0.1, idle_sec=0.1, as_error_event=True)),
        timeout=3,
    )
    assert [e["type"] for e in events] == ["model_info", "error"]
    assert events[-1]["content"].startswith("stream_stall_timeout")
    assert closed["v"] is True


@pytest.mark.asyncio
async def test_idle_limit_applies_after_output_and_progress_resets_it():
    async def _slow_then_stall():
        yield {"type": "delta", "content": "a"}
        await asyncio.sleep(0.05)
        yield {"type": "delta", "content": "b"}
        await asyncio.sleep(3600)

    seen = []
    with pytest.raises(model_selector.StreamStallError) as exc:
        async for e in model_selector.iter_with_stall_timeout(
            _slow_then_stall(), label="x", first_output_sec=5, idle_sec=0.2
        ):
            seen.append(e["content"])
    assert seen == ["a", "b"]
    assert exc.value.phase == "출력 이후 대기"


@pytest.mark.asyncio
async def test_disabled_limits_pass_events_through():
    async def _ok():
        yield {"type": "delta", "content": "a"}
        yield {"type": "done"}

    events = await _drain(model_selector.iter_with_stall_timeout(
        _ok(), label="x", first_output_sec=0, idle_sec=0))
    assert [e["type"] for e in events] == ["delta", "done"]


def test_stall_limits_defaults_follow_existing_conventions_and_grace(monkeypatch):
    monkeypatch.setattr(model_selector, "_STREAM_STALL_FIRST_OUTPUT_SEC", 420.0)
    monkeypatch.setattr(model_selector, "_STREAM_STALL_IDLE_SEC", 600.0)
    assert model_selector.stream_stall_limits() == (420.0, 600.0)
    assert model_selector.stream_stall_limits(grace=60.0) == (480.0, 660.0)
    monkeypatch.setattr(model_selector, "_STREAM_STALL_FIRST_OUTPUT_SEC", 0.0)
    assert model_selector.stream_stall_limits(grace=60.0)[0] == 0.0
    assert model_selector._STREAM_STALL_FIRST_OUTPUT_SEC == 0.0


@pytest.mark.asyncio
async def test_call_stream_moves_on_when_fallback_sdk_attempt_goes_silent(monkeypatch):
    async def _no_key(*_a, **_k):
        return ""

    async def _models():
        return {"claude-fable-5-1"}

    async def _row(model_id, provider=None):
        return {"provider": "anthropic", "model_id": "claude-fable-5-1", "is_active": True,
                "is_executable": True, "metadata": {}}

    async def _slots(project: str = ""):
        return {}

    calls = {"relay": 0, "sdk": 0, "sdk_closed": 0}

    async def _relay(*_a, **_k):
        calls["relay"] += 1
        yield {"type": "error", "content": "CLI exited with code 255"}

    async def _sdk(*_a, **_k):
        calls["sdk"] += 1
        try:
            yield {"type": "model_info", "model": "claude-fable-5-1"}
            await asyncio.sleep(3600)
        finally:
            calls["sdk_closed"] += 1

    async def _noop(*_a, **_k):
        return None

    monkeypatch.setattr(model_selector, "_get_db_key", _no_key)
    monkeypatch.setattr(model_selector, "get_available_model_ids", _models)
    monkeypatch.setattr(model_selector, "_get_registered_model_row", _row)
    monkeypatch.setattr(model_selector, "_get_claude_slot_records", _slots)
    monkeypatch.setattr(model_selector, "_stream_cli_relay", _relay)
    monkeypatch.setattr(model_selector, "_stream_agent_sdk", _sdk)
    monkeypatch.setattr(model_selector, "_alert_no_slots_left", _noop)
    monkeypatch.setattr(model_selector, "_relay_clear_aads_session_for_oauth_fallback", _noop)
    monkeypatch.setattr(model_selector, "_STREAM_STALL_FIRST_OUTPUT_SEC", 0.1)
    monkeypatch.setattr(model_selector, "_STREAM_STALL_IDLE_SEC", 0.1)

    intent = IntentResult(intent="cto_strategy", model="claude-fable-5-1", use_tools=True, tool_group="all")
    events = []

    async def _run():
        try:
            async for e in model_selector.call_stream(
                intent, "system", [{"role": "user", "content": "x"}], model_override="claude-fable-5-1"
            ):
                events.append(e)
        except Exception:
            pass  # 모든 단계 소진 후의 명시적 종료는 허용 — 멈춰 있지만 않으면 된다

    await asyncio.wait_for(_run(), timeout=20)
    assert calls["sdk"] >= 1, "SDK 단계까지 내려가야 한다"
    assert calls["sdk_closed"] == calls["sdk"], "멈춘 SDK 시도는 취소되어 닫혀야 한다"
    assert not any(e.get("type") == "done" for e in events), "멈춘 시도가 성공으로 끝나면 안 된다"


# ── (ii) Codex 401 → 인증 오류 ───────────────────────────────────────────

class _FakeResp:
    status_code = 200

    def __init__(self, lines):
        self._lines = lines

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aread(self):
        return b""


class _FakeStream:
    def __init__(self, lines):
        self._resp = _FakeResp(lines)

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *_a):
        return False


def _fake_client_cls(lines):
    class _Client:
        def __init__(self, *_a, **_k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        async def get(self, *_a, **_k):
            return _FakeResp([])

        def stream(self, *_a, **_k):
            return _FakeStream(lines)

    return _Client


async def _run_codex_once(monkeypatch, lines, revoked=()):
    async def _project(_sid):
        return "AADS"

    async def _order(_project):
        return None

    async def _revoked():
        return list(revoked)

    monkeypatch.setattr(model_selector, "_resolve_codex_project", _project)
    monkeypatch.setattr(model_selector, "_codex_account_order_for_project", _order)
    monkeypatch.setattr(model_selector, "_codex_revoked_account_names", _revoked)
    monkeypatch.setattr(model_selector, "_load_relay_shared_secret", lambda: "")
    monkeypatch.setattr(model_selector.httpx, "AsyncClient", _fake_client_cls(lines))
    return await _drain(model_selector._stream_codex_relay_once(
        "gpt-6.1-sol", "system", [{"role": "user", "content": "hi"}], session_id="9102c970-test"))


_EMPTY_RESULT = json.dumps({"type": "result", "result": "", "input_tokens": 0, "output_tokens": 0})


@pytest.mark.asyncio
async def test_codex_401_error_event_is_reported_as_auth_failure(monkeypatch):
    lines = [json.dumps({"type": "error", "content": "unexpected status 401 Unauthorized: token_revoked sk-secret-value"})]
    events = await _run_codex_once(monkeypatch, lines)
    err = events[-1]
    assert err["type"] == "error"
    assert "Codex 인증 실패(재로그인 필요)" in err["content"]
    assert err["content"].startswith("codex_auth_failed")
    assert "sk-secret-value" not in err["content"]
    assert "token_revoked" not in err["content"]


@pytest.mark.asyncio
async def test_codex_empty_result_with_revoked_account_is_auth_failure_not_empty_result(monkeypatch):
    events = await _run_codex_once(monkeypatch, [_EMPTY_RESULT], revoked=["CODEX_OAUTH_MAIN"])
    err = events[-1]
    assert err["type"] == "error"
    assert "Codex 인증 실패(재로그인 필요)" in err["content"]
    assert "CODEX_OAUTH_MAIN" in err["content"]
    assert "codex_empty_result" not in err["content"]


@pytest.mark.asyncio
async def test_codex_empty_result_with_diagnostic_text_is_auth_failure(monkeypatch):
    lines = [json.dumps({"type": "result", "result": "", "input_tokens": 0, "output_tokens": 0,
                         "stderr": "401 Unauthorized: refresh token revoked"})]
    events = await _run_codex_once(monkeypatch, lines)
    assert "Codex 인증 실패(재로그인 필요)" in events[-1]["content"]
    assert "[revoked]" in events[-1]["content"]


@pytest.mark.asyncio
async def test_codex_empty_result_without_auth_evidence_stays_empty_result(monkeypatch):
    events = await _run_codex_once(monkeypatch, [_EMPTY_RESULT])
    assert events[-1]["type"] == "error"
    assert events[-1]["content"].startswith("codex_empty_result")


def test_codex_auth_failure_is_not_retried_on_same_model():
    msg = model_selector._codex_auth_failure_message("revoked", ["A"])
    assert model_selector._is_codex_retryable_error(msg) is False


def test_codex_auth_failure_reason_label_and_interruption_category():
    msg = model_selector._codex_auth_failure_message("unauthorized")
    assert chat_service._safe_fallback_reason(msg) == "Codex 인증 실패(재로그인 필요)"
    assert chat_service._classify_interruption_reason(msg) == "llm_provider_error"
    assert chat_service._classify_interruption_reason("stream_stall_timeout:x:420s") == "watchdog_timeout"


# ── (iii) 전환 시 fallback_chain·actual_model 기록 ───────────────────────

class _FakeConn:
    def __init__(self, sink):
        self.sink = sink

    async def execute(self, sql, *args):
        self.sink.append((sql, args))


class _FakePool:
    def __init__(self, sink):
        self.sink = sink

    def acquire(self):
        sink = self.sink

        class _Ctx:
            async def __aenter__(self_inner):
                return _FakeConn(sink)

            async def __aexit__(self_inner, *_a):
                return False

        return _Ctx()


@pytest.mark.asyncio
async def test_switch_is_persisted_with_actual_model_and_same_chain_shape(monkeypatch):
    sink: list = []
    monkeypatch.setattr(chat_service, "get_pool", lambda: _FakePool(sink))
    chain: list = []
    chat_service._note_retry_model_switch(
        chain, prev_model="codex:gpt-6.1-sol", new_model="claude-fable-5-1",
        base_model="codex:gpt-6.1-sol",
        reason=chat_service._safe_fallback_reason(model_selector._codex_auth_failure_message("revoked")),
    )
    execution_id = "11111111-1111-1111-1111-111111111111"
    await chat_service._persist_retry_model_switch(execution_id, chain, "claude-fable-5-1")

    assert len(sink) == 1
    sql, args = sink[0]
    assert "fallback_chain" in sql and "actual_model" in sql and "chat_turn_executions" in sql
    saved = json.loads(args[0])
    assert saved[0]["from"] == "codex:gpt-6.1-sol"
    assert saved[0]["to"] == "claude-fable-5-1"
    assert saved[0]["reason"] == "Codex 인증 실패(재로그인 필요)"
    assert set(saved[0]) == {"from", "to", "reason", "at"}
    assert args[1] == "claude-fable-5-1"
    assert str(args[2]) == execution_id


@pytest.mark.asyncio
async def test_persist_is_noop_without_execution_or_chain(monkeypatch):
    sink: list = []
    monkeypatch.setattr(chat_service, "get_pool", lambda: _FakePool(sink))
    await chat_service._persist_retry_model_switch(None, [{"from": "a", "to": "b"}], "b")
    await chat_service._persist_retry_model_switch("11111111-1111-1111-1111-111111111111", [], "b")
    assert sink == []


def test_retry_loop_uses_stall_guard_and_persists_switch():
    src = inspect.getsource(chat_service.send_message_stream)
    assert "iter_with_stall_timeout(" in src
    assert "_persist_retry_model_switch(" in src
    assert "_note_retry_model_switch(" in src
    assert "StreamStallError" in src
