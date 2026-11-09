"""이미지 첨부 턴에서 agy 릴레이가 500(TypeError)으로 죽고 Codex 로 폴백하던 회귀 방지."""
import base64
import importlib.util
import os
import sys


_RELAY = os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "claude_relay_server.py")


def _load_relay():
    spec = importlib.util.spec_from_file_location("claude_relay_server_under_test", _RELAY)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


_PNG = base64.b64encode(
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
).decode()


def test_relay_flatten_accepts_list_blocks():
    relay = _load_relay()
    blocks = [
        {"type": "text", "text": "이미지 분석"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": _PNG}},
    ]
    system_text, user_text = relay._antigravity_flatten_prompt_inputs(
        [{"type": "text", "text": "SYS"}], blocks, None, "sess-img-test")
    assert system_text == "SYS"
    assert user_text == "이미지 분석"
    assert isinstance("[SYSTEM]\n" + system_text + "\n\n[USER]\n" + user_text, str)


def test_relay_flatten_passthrough_str():
    relay = _load_relay()
    assert relay._antigravity_flatten_prompt_inputs("a", "b", None, "s") == ("a", "b")


def test_relay_flatten_appends_image_paths():
    relay = _load_relay()
    _, user_text = relay._antigravity_flatten_prompt_inputs(
        "", "본문", [{"name": "x.png", "media_type": "image/png", "data": _PNG}], "sess-img-test2")
    assert "[첨부 이미지 파일 경로]" in user_text
    assert "aads-codex-images" in user_text


def test_server_agy_request_splits_images():
    from app.services import model_selector as ms
    formatted, images = ms._extract_codex_prompt_and_images([
        {"type": "text", "text": "질문"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": _PNG}},
    ])
    assert isinstance(formatted, str) and "질문" in formatted
    assert len(images) == 1
