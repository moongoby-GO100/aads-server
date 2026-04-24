from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import uuid

import pytest

from app.services import chat_service


class _AcquireCtx:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _Pool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _AcquireCtx(self._conn)


@pytest.mark.asyncio
async def test_get_last_html_artifact():
    session_id = str(uuid.uuid4())
    artifact_id = uuid.uuid4()
    message_id = uuid.uuid4()
    created_at = datetime.now(timezone.utc)

    empty_conn = AsyncMock()
    empty_conn.fetchrow = AsyncMock(return_value=None)
    with patch("app.services.chat_service.get_pool", return_value=_Pool(empty_conn)):
        assert await chat_service._get_last_html_artifact(session_id) is None

    row_conn = AsyncMock()
    row_conn.fetchrow = AsyncMock(return_value={
        "id": artifact_id,
        "title": "Landing Page",
        "content": "<html>preview</html>",
        "message_id": message_id,
        "created_at": created_at,
    })
    with patch("app.services.chat_service.get_pool", return_value=_Pool(row_conn)):
        result = await chat_service._get_last_html_artifact(session_id)

    assert result == {
        "id": artifact_id,
        "title": "Landing Page",
        "content": "<html>preview</html>",
        "message_id": message_id,
        "created_at": created_at,
    }


def test_edit_intent_detection():
    assert chat_service._is_html_edit_intent("이 부분 파란색으로 바꿔")
    assert chat_service._is_html_edit_intent("make the button bigger")
    assert not chat_service._is_html_edit_intent("이 HTML의 구조를 설명해줘")
    assert not chat_service._is_html_edit_intent("")


def test_dedupe_recovery_like_messages_keeps_longest_recovery_message():
    shared_prefix = "partial prefix answer " * 3
    messages = [
        {"id": "u1", "role": "user", "content": "질문", "model_used": None},
        {"id": "a1", "role": "assistant", "content": f"{shared_prefix}A", "model_used": "recovered"},
        {"id": "a2", "role": "assistant", "content": f"{shared_prefix}A with more detail", "model_used": "recovered_from_redis"},
        {"id": "a3", "role": "assistant", "content": "최종 정상 응답", "model_used": "gpt-5.4"},
    ]

    deduped = chat_service._dedupe_recovery_like_messages(messages)

    assert [message["id"] for message in deduped] == ["u1", "a2", "a3"]


@pytest.mark.asyncio
async def test_html_context_injection():
    captured = {}
    session_id = str(uuid.uuid4())
    artifact_id = uuid.uuid4()
    now = datetime.now(timezone.utc)

    conn = AsyncMock()

    async def _mock_fetchrow(query, *args):
        if "FROM chat_messages WHERE idempotency_key" in query:
            return None
        if "WHERE session_id = $1 AND role = 'user' AND content = $2" in query:
            return None
        if "FROM chat_workspaces w" in query:
            return {
                "workspace_id": uuid.uuid4(),
                "workspace_name": "AADS",
                "system_prompt": "BASE_SYSTEM",
            }
        if "FROM chat_session_stats" in query:
            return {"cost_total": 0, "message_count": 0}
        if "SELECT settings FROM chat_users" in query:
            return {"settings": {}}
        return None

    conn.fetchrow = AsyncMock(side_effect=_mock_fetchrow)
    conn.fetch = AsyncMock(return_value=[])

    async def _mock_call_stream(*, system_prompt, **kwargs):
        captured["system_prompt"] = system_prompt
        yield {"type": "error", "content": "stop"}

    with (
        patch("app.services.chat_service.get_pool", return_value=_Pool(conn)),
        patch("app.services.chat_service.create_trace", return_value=None),
        patch(
            "app.services.chat_service._get_last_html_artifact",
            new=AsyncMock(return_value={
                "id": artifact_id,
                "title": "Preview",
                "content": "<html><body><button>Old</button></body></html>",
                "message_id": uuid.uuid4(),
                "created_at": now - timedelta(hours=1),
            }),
        ),
        patch(
            "app.services.context_builder.build_messages_context",
            new=AsyncMock(return_value=([{"role": "user", "content": "버튼 크기 키워"}], "BASE_SYSTEM")),
        ),
        patch(
            "app.services.intent_router.classify",
            new=AsyncMock(return_value=SimpleNamespace(
                intent="general",
                model="claude-sonnet",
                use_tools=False,
                tool_group=None,
                use_gemini_direct=False,
                gemini_mode=None,
            )),
        ),
        patch(
            "app.services.contradiction_detector.detect_contradictions",
            new=AsyncMock(return_value=""),
        ),
        patch(
            "app.services.chat_embedding_service.embed_texts",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "app.services.semantic_cache.SemanticCache.lookup",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "app.services.chat_service._save_message",
            new=AsyncMock(return_value={"id": uuid.uuid4()}),
        ),
        patch(
            "app.services.chat_service._save_and_update_session",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "app.services.model_selector.call_stream",
            new=_mock_call_stream,
        ),
    ):
        chunks = []
        async for chunk in chat_service.send_message_stream(
            session_id=session_id,
            content="버튼 크기 키워",
            attachments=[],
        ):
            chunks.append(chunk)

    assert '"html_context_used": true' in chunks[0]
    assert "[현재 작업 중인 HTML 아티팩트" in captured["system_prompt"]
    assert "```html" in captured["system_prompt"]
    assert "<button>Old</button>" in captured["system_prompt"]
