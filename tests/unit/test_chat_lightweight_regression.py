"""Chat Lightweight regression tests — P1."""
from pathlib import Path

from app.services import chat_service
from app.services.chat_service import _project_message_fields

REAL_DASHBOARD_CHAT_PAGE = Path("../aads-dashboard/src/app/chat/page.tsx")
CHAT_PAGE = REAL_DASHBOARD_CHAT_PAGE if REAL_DASHBOARD_CHAT_PAGE.exists() else Path("aads-dashboard/src/app/chat/page.tsx")
CHAT_SERVICE = Path("app/services/chat_service.py")
MODEL_SELECTOR = Path("app/services/model_selector.py")
INTENT_ROUTER = Path("app/services/intent_router.py")


def test_long_content_not_overwritten_by_minimal():
    source = CHAT_PAGE.read_text()
    assert "mergeServerMessageWithExisting" in source
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


def test_minimal_projection_preserves_polling_contract():
    content = "x" * 400
    messages = [{
        "id": "message-1",
        "content": content,
        "tools_called": [
            {"type": "tool_use", "tool_name": "query_database"},
            {"type": "tool_result", "tool_name": "query_database", "content": "large result"},
        ],
    }]

    result = _project_message_fields(messages, "minimal")[0]

    assert result["content"] == content[:320]
    assert result["content_length"] == 400
    assert result["is_truncated"] is True
    assert result["has_tools"] is True
    assert result["tool_count"] == 1
    assert result["tool_names"] == ["query_database"]
    assert result["tools_called"] == []
    assert messages[0]["content"] == content


def test_full_projection_is_unchanged():
    messages = [{"id": "message-1", "content": "full", "tools_called": []}]
    assert _project_message_fields(messages, None) is messages


def test_local_file_preview_returns_html_artifact(tmp_path, monkeypatch):
    report = tmp_path / "report.html"
    report.write_text("<html><body><h1>GO100</h1></body></html>", encoding="utf-8")
    monkeypatch.setattr(chat_service, "_LOCAL_FILE_ALLOWED_ROOTS", (tmp_path.resolve(),))

    artifact = chat_service.local_file_preview_artifact(str(report))

    assert artifact["type"] == "html_preview"
    assert artifact["title"] == "report.html"
    assert "GO100" in artifact["content"]
    assert artifact["metadata"]["source"] == "local_file_preview"
    assert artifact["metadata"]["source_path"] == str(report.resolve())


def test_local_file_preview_blocks_sensitive_names(tmp_path, monkeypatch):
    secret_file = tmp_path / ".env"
    secret_file.write_text("TOKEN=value", encoding="utf-8")
    monkeypatch.setattr(chat_service, "_LOCAL_FILE_ALLOWED_ROOTS", (tmp_path.resolve(),))

    try:
        chat_service.local_file_preview_artifact(str(secret_file))
    except PermissionError as exc:
        assert "보안상" in str(exc)
    else:
        raise AssertionError("sensitive local file was not blocked")


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
