"""Chat Lightweight regression tests — P1."""
from pathlib import Path

REAL_DASHBOARD_CHAT_PAGE = Path("../aads-dashboard/src/app/chat/page.tsx")
CHAT_PAGE = REAL_DASHBOARD_CHAT_PAGE if REAL_DASHBOARD_CHAT_PAGE.exists() else Path("aads-dashboard/src/app/chat/page.tsx")
CHAT_SERVICE = Path("app/services/chat_service.py")
MODEL_SELECTOR = Path("app/services/model_selector.py")
INTENT_ROUTER = Path("app/services/intent_router.py")


def test_long_content_not_overwritten_by_minimal():
    source = CHAT_PAGE.read_text()
    # mergeServerMessageWithExisting was refactored in dashboard rebuild
    # assert "mergeServerMessageWithExisting" in source
    assert "keepExistingContent" in source
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


def test_autonomous_path_stores_structured_tool_events():
    """자율 실행 경로가 도구 이벤트를 구조화 dict로 저장하는지 확인."""
    source = CHAT_SERVICE.read_text()
    # autonomous path에서 tool_use를 dict로 저장
    assert '"type": "tool_use"' in source
    assert '"tool_use_id"' in source
    # autonomous path에서 tool_result도 저장
    assert '"type": "tool_result"' in source
    # autonomous path에서 done 이벤트로 model_used 업데이트
    assert '_done_model = _data.get("model")' in source


def test_autonomous_path_done_event_updates_model():
    """자율 실행 경로가 done 이벤트의 model로 model_used를 업데이트하는지 확인."""
    source = CHAT_SERVICE.read_text()
    assert "model_used = _done_model" in source
