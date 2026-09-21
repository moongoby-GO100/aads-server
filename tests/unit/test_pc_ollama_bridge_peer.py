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
