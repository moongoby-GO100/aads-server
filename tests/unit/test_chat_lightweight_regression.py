"""Chat Lightweight regression tests — P1."""
from pathlib import Path

CHAT_PAGE = Path("aads-dashboard/src/app/chat/page.tsx")
CHAT_SERVICE = Path("app/services/chat_service.py")
MODEL_SELECTOR = Path("app/services/model_selector.py")
INTENT_ROUTER = Path("app/services/intent_router.py")


def test_long_content_not_overwritten_by_minimal():
    source = CHAT_PAGE.read_text()
    assert "mergeMessagePreservingFullContent" in source
    assert "is_truncated" in source or "content_length" in source


def test_has_tools_triggers_hydration():
    source = CHAT_PAGE.read_text()
    assert "needsHydrate" in source
    assert "hydrateMessageTools" in source
    assert "has_tools" in source


def test_minimal_api_returns_tool_metadata():
    source = CHAT_SERVICE.read_text()
    assert "has_tools" in source
    assert "tool_count" in source
    assert "tool_names" in source
    assert "is_truncated" in source


def test_polling_has_message_id_skip():
    source = CHAT_PAGE.read_text()
    assert "lastKnownMsgIdRef" in source


def test_thinking_pipeline_connected():
    ms = MODEL_SELECTOR.read_text()
    cs = CHAT_SERVICE.read_text()
    ir = INTENT_ROUTER.read_text()
    assert "ThinkingDelta" in ms
    assert "thinking_text" in ms
    assert "thinking_summary" in cs
    assert '"thinking": True' in ir


def test_codex_model_aliases_normalized():
    source = CHAT_SERVICE.read_text()
    assert "gpt-5.5" in source
    assert "gpt-5.4" in source
    assert "gpt-5.3-codex" in source
