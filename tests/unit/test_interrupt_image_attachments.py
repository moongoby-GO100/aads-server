"""스트리밍 중 '추가 지시'로 보낸 첨부 이미지가 모델까지 전달된다.

2026-10-15 실측(세션 009efbfa): CLI relay 경로는 model_selector 도구 루프의 인터럽트 주입을
타지 않고 chat_service 의 지연 적용 경로로 넘어가는데, 그 경로가 첨부를
`- image.png (image)` 텍스트 줄로만 붙이고 base64 를 버렸다. SDK 주입도 마찬가지였다.
"""
import asyncio
import base64
import inspect
import io
from pathlib import Path

import pytest

from app.core import interrupt_attachments as ia


def _png_b64(size=(8, 8)) -> str:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", size, (200, 30, 30)).save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode()


def _jpeg_b64() -> str:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (10, 200, 30)).save(buf, "JPEG")
    return base64.b64encode(buf.getvalue()).decode()


def _image_interrupt(text="이 사양표 반영해줘", **att_extra):
    att = {"name": "image.png", "type": "image", "base64": _png_b64(), **att_extra}
    return {"content": text, "attachments": [att]}


# ── 헬퍼 ────────────────────────────────────────────────────────────────────

def test_sniff_media_type_by_magic_bytes():
    assert ia.sniff_image_media_type(_png_b64()) == "image/png"
    assert ia.sniff_image_media_type(_jpeg_b64()) == "image/jpeg"
    gif = base64.b64encode(b"GIF89a" + b"\x00" * 20).decode()
    assert ia.sniff_image_media_type(gif) == "image/gif"
    webp = base64.b64encode(b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 8).decode()
    assert ia.sniff_image_media_type(webp) == "image/webp"


def test_sniff_media_type_falls_back_to_hint_then_png():
    junk = base64.b64encode(b"not-an-image-at-all-xxxxxxxx").decode()
    assert ia.sniff_image_media_type(junk) == "image/png"
    assert ia.sniff_image_media_type(junk, "image/webp") == "image/webp"
    assert ia.sniff_image_media_type("!!!", None) == "image/png"


def test_sniff_prefers_bytes_over_wrong_declared_type():
    assert ia.sniff_image_media_type(_jpeg_b64(), "image/png") == "image/jpeg"


def test_anthropic_blocks_for_image_without_media_type():
    blocks, notice = ia.build_interrupt_image_blocks([_image_interrupt()])
    assert notice == ""
    assert len(blocks) == 1
    assert blocks[0]["type"] == "image"
    assert blocks[0]["source"]["type"] == "base64"
    assert blocks[0]["source"]["media_type"] == "image/png"
    assert blocks[0]["source"]["data"]


def test_image_url_blocks_for_litellm():
    blocks, _ = ia.build_interrupt_image_blocks([_image_interrupt()], fmt="image_url")
    assert len(blocks) == 1
    assert blocks[0]["type"] == "image_url"
    assert blocks[0]["image_url"]["url"].startswith("data:image/png;base64,")


def test_data_url_prefix_is_accepted():
    item = _image_interrupt()
    item["attachments"][0]["base64"] = "data:image/png;base64," + item["attachments"][0]["base64"]
    blocks, _ = ia.build_interrupt_image_blocks([item])
    assert len(blocks) == 1


def test_undecodable_image_is_reported_not_silently_dropped():
    item = {"content": "x", "attachments": [{"name": "bad.png", "type": "image", "base64": "@@@@"}]}
    blocks, notice = ia.build_interrupt_image_blocks([item])
    assert blocks == []
    assert "전달하지 못했습니다" in notice


def test_no_attachments_yields_nothing():
    assert ia.build_interrupt_image_blocks([{"content": "hi", "attachments": []}]) == ([], "")
    note, blocks = asyncio.run(
        ia.build_interrupt_attachment_payload("s", [{"content": "hi", "attachments": []}, {"content": "yo"}])
    )
    assert (note, blocks) == ("", [])
    assert ia.attach_images("본문", []) == "본문"


def test_payload_image_note_and_blocks():
    note, blocks = asyncio.run(ia.build_interrupt_attachment_payload("s", [_image_interrupt()]))
    assert len(blocks) == 1
    assert note.startswith("\n\n[추가 지시 첨부파일]\n- image.png (image)")
    content = ia.attach_images("본문", blocks)
    assert content[0] == {"type": "text", "text": "본문"}
    assert content[1]["type"] == "image"


def test_non_image_file_reference_prefers_existing_storage_and_never_saves_twice():
    saved = []

    async def saver(*a):
        saved.append(a)
        return "new-id"

    items = [{"content": "x", "attachments": [
        {"name": "a.csv", "type": "file", "file_id": "11111111-1111-1111-1111-111111111111"},
        {"name": "b.txt", "type": "file", "path": "/tmp/uploads/b.txt"},
    ]}]
    note, blocks = asyncio.run(ia.build_interrupt_attachment_payload("s", items, saver=saver))
    assert blocks == []
    assert saved == []
    assert 'read_uploaded_file(filename="11111111-1111-1111-1111-111111111111")' in note
    assert "/tmp/uploads/b.txt" in note


def test_inline_non_image_file_is_persisted_once_for_read_uploaded_file():
    saved = []

    async def saver(session_id, name, media_type, data):
        saved.append((session_id, name, data))
        return "22222222-2222-2222-2222-222222222222"

    items = [{"content": "x", "attachments": [
        {"name": "memo.txt", "type": "file", "base64": base64.b64encode(b"hello").decode()},
    ]}]
    note, _ = asyncio.run(ia.build_interrupt_attachment_payload("sess", items, saver=saver))
    assert saved == [("sess", "memo.txt", b"hello")]
    assert "22222222-2222-2222-2222-222222222222" in note
    # 같은 항목을 다시 처리해도(2차 패스) 다시 저장하지 않는다.
    note2, _ = asyncio.run(ia.build_interrupt_attachment_payload("sess", items, saver=saver))
    assert len(saved) == 1
    assert "22222222-2222-2222-2222-222222222222" in note2


# ── 지연 적용 경로 1: _apply_deferred_interrupts_to_state ───────────────────

def _run_deferred(monkeypatch, interrupts, fast_revision=False):
    from app.services import chat_service, model_selector

    queue = [list(interrupts), []]
    captured = {}

    async def fake_collect(session_id, *, limit=5):
        return queue.pop(0) if queue else []

    async def fake_apply(session_id, items):
        return []

    async def fake_call_stream(**kwargs):
        captured["messages"] = kwargs["messages"]
        yield {"type": "delta", "content": "반영 답변"}
        yield {"type": "done", "model": "m", "cost": "0"}

    async def fake_save_partial(*a, **k):
        return None

    monkeypatch.setattr(chat_service, "_collect_queued_interrupts", fake_collect)
    monkeypatch.setattr(chat_service._interrupt_lifecycle, "apply_interrupts", fake_apply)
    monkeypatch.setattr(chat_service, "_save_interrupted_partial_message", fake_save_partial)
    monkeypatch.setattr(model_selector, "call_stream", fake_call_stream)

    async def go():
        state = {"full_response": "이전 답변"}
        async for _ in chat_service._apply_deferred_interrupts_to_state(
            session_id="009efbfa-d94b-4f25-b511-5d7052deaae9",
            state=state,
            messages=[{"role": "user", "content": "원 질문"}],
            system_prompt="sys",
            intent_result=None,
            tools_for_api=None,
            model_override=None,
            fast_revision=fast_revision,
        ):
            pass

    asyncio.run(go())
    return captured.get("messages")


def test_deferred_path_passes_image_block_to_model(monkeypatch):
    msgs = _run_deferred(monkeypatch, [_image_interrupt()])
    last = msgs[-1]
    assert isinstance(last["content"], list)
    assert last["content"][0]["type"] == "text"
    assert "이 사양표 반영해줘" in last["content"][0]["text"]
    assert "[추가 지시 첨부파일]" in last["content"][0]["text"]
    images = [b for b in last["content"] if b.get("type") == "image"]
    assert len(images) == 1
    assert images[0]["source"]["media_type"] == "image/png"
    assert images[0]["source"]["data"]


def test_deferred_path_fast_revision_with_image_uses_stream_not_text_only(monkeypatch):
    msgs = _run_deferred(monkeypatch, [_image_interrupt()], fast_revision=True)
    assert msgs is not None, "이미지가 있으면 텍스트 전용 빠른 재작성이 아니라 스트림 경로여야 한다"
    assert any(b.get("type") == "image" for b in msgs[-1]["content"])


def test_deferred_path_without_attachments_keeps_plain_text(monkeypatch):
    msgs = _run_deferred(monkeypatch, [{"content": "그냥 추가 지시", "attachments": []}])
    last = msgs[-1]
    assert isinstance(last["content"], str)
    assert "그냥 추가 지시" in last["content"]
    assert "[추가 지시 첨부파일]" not in last["content"]


# ── 지연 적용 경로 2: send_message_stream 내부 ─────────────────────────────

def test_stream_deferred_path_uses_shared_helper():
    from app.services import chat_service

    src = inspect.getsource(chat_service.send_message_stream)
    start = src.index("_deferred_interrupts = await _collect_queued_interrupts")
    block = src[start: start + 3500]
    assert "build_interrupt_attachment_payload" in block
    assert "_interrupt_attachments.attach_images" in block
    assert "_interrupt_image_blocks" in block
    # 첨부를 이름만 적는 옛 코드가 남아 있으면 안 된다.
    text = Path(chat_service.__file__).read_text(encoding="utf-8")
    assert text.count("[추가 지시 첨부파일]") == 0
    assert "_attachment_lines" not in text


# ── model_selector 도구 루프 두 곳이 같은 헬퍼를 쓴다 ────────────────────────

def test_model_selector_loops_use_shared_helper():
    from app.services import model_selector

    text = Path(model_selector.__file__).read_text(encoding="utf-8")
    assert text.count("_build_interrupt_image_blocks(") + text.count("_build_interrupt_image_blocks,") >= 2
    assert 'fmt="image_url"' in text


# ── SDK 주입 ─────────────────────────────────────────────────────────────────

class _FakeClient:
    def __init__(self):
        self.prompt = None
        self.messages = []

    async def query(self, prompt, session_id="default"):
        self.prompt = prompt
        if not isinstance(prompt, str):
            async for m in prompt:
                self.messages.append(m)


def _run_inject(monkeypatch, items):
    from app.services import agent_sdk_service as sdk

    async def fake_apply(session_id, got):
        return [{"type": "interrupt_applied"}]

    monkeypatch.setattr(sdk, "apply_interrupts", fake_apply)
    svc = sdk.AgentSDKService.__new__(sdk.AgentSDKService)
    client = _FakeClient()

    async def go():
        out = []
        async for chunk in svc._inject_interrupts(client, "sess-1", lambda sid: items):
            out.append(chunk)
        return out

    return client, asyncio.run(go())


def test_sdk_injection_sends_image_block(monkeypatch):
    client, out = _run_inject(monkeypatch, [_image_interrupt()])
    assert out, "반영 이벤트가 나가야 한다"
    assert len(client.messages) == 1
    msg = client.messages[0]
    assert msg["type"] == "user"
    content = msg["message"]["content"]
    assert content[0]["type"] == "text"
    assert "이 사양표 반영해줘" in content[0]["text"]
    assert "대표님 지시를 우선" in content[0]["text"]
    assert any(b["type"] == "image" and b["source"]["data"] for b in content)


def test_sdk_injection_without_attachments_is_unchanged_string(monkeypatch):
    client, out = _run_inject(monkeypatch, [{"content": "텍스트만", "attachments": []}])
    assert isinstance(client.prompt, str)
    assert "텍스트만" in client.prompt
    assert "[추가 지시 첨부파일]" not in client.prompt
    assert out


@pytest.mark.parametrize("fmt", ["anthropic", "image_url"])
def test_two_images_both_delivered(fmt):
    item = _image_interrupt()
    item["attachments"].append({"name": "b.jpg", "type": "image", "base64": _jpeg_b64()})
    blocks, notice = ia.build_interrupt_image_blocks([item], fmt=fmt)
    assert len(blocks) == 2 and notice == ""
