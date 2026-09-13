"""Chat Lightweight regression tests — P1."""
from pathlib import Path

import pytest

from app.services import chat_service
from app.services.chat_service import _message_select_fields

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
    """fields=minimal 폴링 계약 — 본문은 잘라 보내되 길이/도구 요약은 유지한다.

    투영은 Python 후처리가 아니라 SQL SELECT 절(_message_select_fields)에서
    수행되므로, 계약 회귀는 생성되는 투영 문자열로 검증한다.
    """
    projection = _message_select_fields("minimal")

    assert "LEFT(content, 200) AS content" in projection
    assert "LENGTH(content) AS content_length" in projection
    assert "(LENGTH(content) > 200) AS is_truncated" in projection
    assert "AS has_tools" in projection
    assert "AS tool_count" in projection
    assert "AS tool_names" in projection
    # 대용량 컬럼은 폴링 응답에 실리지 않는다 (전체 tools_called/thinking/embedding).
    assert "AS tools_called" not in projection
    assert "thinking_summary" not in projection
    assert "embedding" not in projection


def test_full_projection_is_unchanged():
    assert _message_select_fields("full") == "*"
    assert _message_select_fields("") == "*"


@pytest.mark.skip(
    reason="chat_service.local_file_preview_artifact 미구현 — 2026-07-15 0c5b339f 에서 "
    "구현 없이 테스트만 추가됐고 백엔드·대시보드 어디에도 호출부가 없다. "
    "구현할지 폐기할지 결정 후 skip 을 푼다."
)
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


@pytest.mark.skip(
    reason="chat_service.local_file_preview_artifact 미구현 — 위 테스트와 동일 사유."
)
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
