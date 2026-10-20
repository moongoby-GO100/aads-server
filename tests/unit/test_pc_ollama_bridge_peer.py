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
