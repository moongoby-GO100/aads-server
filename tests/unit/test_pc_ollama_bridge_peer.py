"""PC Ollama 브리지의 슬롯 간 한 홉 전달 가드 테스트.

2026-09-21: PC Agent WebSocket 은 blue/green 중 소켓을 쥔 컨테이너의 프로세스
메모리에만 있다. LiteLLM 이 반대쪽 슬롯을 잡으면 503 'no online PC agent' 가
났다(실측 200/503/200). `_forward_to_peer` 가 그 틈을 메우되, 되돌아오는 루프와
무관한 오류까지 퍼뜨리지 않는지 확인한다.
"""
import asyncio

from app.api.pc_ollama_bridge import _PEER_HOP_KEY, _forward_to_peer, _normalize_messages


def test_forward_to_peer_skips_when_already_hopped():
    payload = {"model": "pc-qwen38-27b", _PEER_HOP_KEY: True}
    assert asyncio.run(_forward_to_peer(payload, "no online PC agent")) is None


def test_forward_to_peer_skips_unrelated_failures():
    payload = {"model": "pc-qwen38-27b"}
    detail = "PC Ollama generation cancelled (done=false, model=x)"
    assert asyncio.run(_forward_to_peer(payload, detail)) is None


def test_normalize_messages_preserves_vision_images():
    messages = _normalize_messages([
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "화면을 검수하세요"},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,aGVsbG8="},
                },
            ],
        }
    ])

    assert messages == [{
        "role": "user",
        "content": "화면을 검수하세요",
        "images": ["aGVsbG8="],
    }]


def test_normalize_messages_does_not_fetch_remote_images():
    messages = _normalize_messages([
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "check"},
                {"type": "image_url", "image_url": "https://example.com/a.png"},
            ],
        }
    ])

    assert messages == [{"role": "user", "content": "check"}]


def test_model_map_bonsai2_and_qwen35_aliases():
    from app.api.pc_ollama_bridge import _MODEL_MAP

    assert _MODEL_MAP["pc-qwen38-27b"] == "bonsai-2:27b"
    assert _MODEL_MAP["pc-bonsai2-27b"] == "bonsai-2:27b"
    assert _MODEL_MAP["pc-qwen35-9b"] == "qwen3.5:9b"
    assert not any(v.startswith("hf.co/") for v in _MODEL_MAP.values())


def test_run_pc_ollama_chat_sends_non_thinking_sampling(monkeypatch):
    from app.api import pc_ollama_bridge
    from app.services import pc_agent_manager as manager_module

    captured = {}

    async def fake_execute(**kwargs):
        captured.update(kwargs["params"])
        return {"status": "success", "result": {"result": {"content": "ok", "raw": {"done": True}}}}

    monkeypatch.setattr(manager_module.pc_agent_manager, "execute_routed_command", fake_execute)
    result = asyncio.run(pc_ollama_bridge._run_pc_ollama_chat(
        {"model": "pc-bonsai2-27b", "messages": [{"role": "user", "content": "hi"}]}
    ))

    assert result["ollama_model"] == "bonsai-2:27b"
    assert captured["think"] is False
    assert captured["temperature"] == 0.7
    assert captured["top_p"] == 0.8
    assert captured["presence_penalty"] == 1.5

    captured.clear()
    asyncio.run(pc_ollama_bridge._run_pc_ollama_chat(
        {"model": "pc-qwen35-9b", "temperature": 0.1, "messages": [{"role": "user", "content": "hi"}]}
    ))
    assert captured["temperature"] == 0.1

    captured.clear()
    asyncio.run(pc_ollama_bridge._run_pc_ollama_chat(
        {"model": "pc-gemma4-e4b", "messages": [{"role": "user", "content": "hi"}]}
    ))
    assert captured["temperature"] == 0.2
    assert "top_p" not in captured


class _FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


def _fake_httpx(monkeypatch, responses, calls):
    import httpx

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None, headers=None):
            calls.append((url, json, headers))
            outcome = responses.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

    monkeypatch.setattr(httpx, "AsyncClient", _Client)


_OK_BODY = {
    "model": "pc-qwen38-27b",
    "choices": [{"message": {"content": "peer-ok"}}],
    "usage": {"prompt_tokens": 3, "completion_tokens": 5},
}


def test_forward_to_peer_local_zero_peer_one_returns_peer_result(monkeypatch):
    from app.api import pc_ollama_bridge

    monkeypatch.setenv("PC_OLLAMA_BRIDGE_API_KEY", "test-token")
    monkeypatch.setenv("AADS_CONTAINER_NAME", "aads-server")
    calls = []
    _fake_httpx(monkeypatch, [_FakeResponse(200, _OK_BODY)], calls)

    payload = {"model": "pc-qwen38-27b", "stream": True}
    result = asyncio.run(pc_ollama_bridge._forward_to_peer(payload, "no online PC agent"))

    assert result["content"] == "peer-ok"
    assert result["completion_tokens"] == 5
    assert [c[0] for c in calls] == ["http://aads-server-green:8080/pc-ollama/v1/chat/completions"]
    assert calls[0][1][_PEER_HOP_KEY] is True
    assert "stream" not in calls[0][1]
    assert calls[0][2] == {"Authorization": "Bearer test-token"}


def test_forward_to_peer_both_empty_collects_peer_errors(monkeypatch):
    from app.api import pc_ollama_bridge

    monkeypatch.setenv("PC_OLLAMA_BRIDGE_API_KEY", "test-token")
    monkeypatch.delenv("AADS_CONTAINER_NAME", raising=False)
    calls = []
    _fake_httpx(
        monkeypatch,
        [_FakeResponse(503, {"detail": "no online PC agent"}), RuntimeError("boom")],
        calls,
    )
    errors: list[str] = []
    result = asyncio.run(
        pc_ollama_bridge._forward_to_peer({"model": "pc-qwen38-27b"}, "no online PC agent", errors=errors)
    )

    assert result is None
    assert len(calls) == 2
    assert errors == ["aads-server:8080 503 no online PC agent", "aads-server-green:8080 RuntimeError"]


def test_forward_to_peer_hops_on_routing_error_code_and_agent_failure(monkeypatch):
    from app.api import pc_ollama_bridge

    monkeypatch.setenv("PC_OLLAMA_BRIDGE_API_KEY", "test-token")
    monkeypatch.setenv("AADS_CONTAINER_NAME", "aads-server")
    calls = []
    _fake_httpx(monkeypatch, [_FakeResponse(200, _OK_BODY), _FakeResponse(200, _OK_BODY)], calls)

    payload = {"model": "pc-qwen38-27b"}
    assert asyncio.run(
        pc_ollama_bridge._forward_to_peer(payload, "x", error_code="NO_CAPABLE_AGENT")
    ) is not None
    assert asyncio.run(pc_ollama_bridge._forward_to_peer(payload, "x", agent_failed=True)) is not None
    assert asyncio.run(pc_ollama_bridge._forward_to_peer(payload, "x")) is None


def _fake_manager(monkeypatch, outcomes, agents):
    from app.services import pc_agent_manager as manager_module

    seen = []

    async def fake_execute(**kwargs):
        seen.append(kwargs["agent_id"])
        return outcomes.pop(0)

    monkeypatch.setattr(manager_module.pc_agent_manager, "execute_routed_command", fake_execute)
    monkeypatch.setattr(manager_module.pc_agent_manager, "list_agent_statuses", lambda: agents)
    return seen


def _agent(agent_id, caps=("pc_ollama",), status="online"):
    return {"agent_id": agent_id, "status": status, "capabilities": list(caps)}


_SUCCESS = {"status": "success", "result": {"result": {"content": "ok", "raw": {"done": True}}}}


def _agent_error(agent_id):
    return {"status": "error", "error_code": None, "message": "model not found", "lease": {"agent_id": agent_id}}


def test_run_retries_other_local_agent_after_agent_error(monkeypatch):
    from app.api import pc_ollama_bridge

    seen = _fake_manager(
        monkeypatch,
        [_agent_error("a-no-model"), _SUCCESS],
        [_agent("a-no-model"), _agent("a-ceo"), _agent("a-nocap", caps=("pc_control",)), _agent("a-off", status="offline")],
    )
    result = asyncio.run(pc_ollama_bridge._run_pc_ollama_chat(
        {"model": "pc-qwen38-27b", "messages": [{"role": "user", "content": "hi"}]}
    ))

    assert result["content"] == "ok"
    assert seen == ["", "a-ceo"]


def test_run_local_zero_goes_to_peer_and_returns_200_path(monkeypatch):
    from app.api import pc_ollama_bridge

    _fake_manager(
        monkeypatch,
        [{"status": "error", "error_code": "PC_AGENT_OFFLINE", "message": "no online PC agent"}],
        [],
    )
    hops = []

    async def fake_forward(payload, detail, **kwargs):
        hops.append((detail, kwargs["error_code"]))
        return {"display_model": "pc-qwen38-27b", "ollama_model": "bonsai-2:27b", "content": "peer", "prompt_tokens": 1, "completion_tokens": 2}

    monkeypatch.setattr(pc_ollama_bridge, "_forward_to_peer", fake_forward)
    result = asyncio.run(pc_ollama_bridge._run_pc_ollama_chat(
        {"model": "pc-qwen38-27b", "messages": [{"role": "user", "content": "hi"}]}
    ))

    assert result["content"] == "peer"
    assert hops == [("no online PC agent", "PC_AGENT_OFFLINE")]


def test_run_both_zero_raises_503_with_peer_reason(monkeypatch):
    import pytest
    from fastapi import HTTPException

    from app.api import pc_ollama_bridge

    _fake_manager(
        monkeypatch,
        [{"status": "error", "error_code": "PC_AGENT_OFFLINE", "message": "no online PC agent"}],
        [],
    )

    async def fake_forward(payload, detail, *, errors=None, **kwargs):
        errors.append("aads-server-green:8080 503 agent boom")
        return None

    monkeypatch.setattr(pc_ollama_bridge, "_forward_to_peer", fake_forward)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(pc_ollama_bridge._run_pc_ollama_chat(
            {"model": "pc-qwen38-27b", "messages": [{"role": "user", "content": "hi"}]}
        ))

    assert exc.value.status_code == 503
    assert exc.value.detail.startswith("no online PC agent")
    assert "agent boom" in exc.value.detail


def test_run_does_not_retry_or_hop_on_command_timeout(monkeypatch):
    import pytest
    from fastapi import HTTPException

    from app.api import pc_ollama_bridge

    seen = _fake_manager(
        monkeypatch,
        [{"status": "error", "error_code": "COMMAND_TIMEOUT", "message": "command timeout", "lease": {"agent_id": "a1"}}],
        [_agent("a1"), _agent("a2")],
    )
    with pytest.raises(HTTPException) as exc:
        asyncio.run(pc_ollama_bridge._run_pc_ollama_chat(
            {"model": "pc-qwen38-27b", "messages": [{"role": "user", "content": "hi"}]}
        ))

    assert exc.value.detail == "command timeout"
    assert seen == [""]
