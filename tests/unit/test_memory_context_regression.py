import asyncio

import pytest


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _Pool:
    def __init__(self, rows):
        self.rows = rows

    def acquire(self):
        return _Acquire(self)

    async def fetch(self, *_args, **_kwargs):
        return self.rows


@pytest.mark.asyncio
async def test_search_semantic_returns_session_id_for_auto_rag_origin():
    from app.services.chat_embedding_service import search_semantic

    rows = [{
        "id": "msg-1",
        "session_id": "session-1",
        "role": "assistant",
        "content": "이전 구조 분석 결과",
        "created_at": "2026-05-09 06:00:00",
        "session_name": "CEO",
        "similarity": 0.91,
    }]

    results = await search_semantic(
        _Pool(rows),
        [0.1] * 768,
        session_id=None,
        limit=1,
        pre_embedded=True,
    )

    assert results[0]["session_id"] == "session-1"


@pytest.mark.asyncio
async def test_auto_rag_filters_messages_already_in_current_history(monkeypatch):
    from app.services import auto_rag

    async def _fake_facts(_query_emb, _project):
        return []

    async def _fake_messages(_query_emb, _session_id, _project=None):
        return [
            {"msg_id": "current-msg", "similarity": 0.99, "text": "중복", "source": "대화"},
            {"msg_id": "past-msg", "similarity": 0.88, "text": "과거", "source": "대화"},
        ]

    monkeypatch.setattr(auto_rag, "_search_memory_facts", _fake_facts)
    monkeypatch.setattr(auto_rag, "_search_chat_messages", _fake_messages)

    results = await auto_rag._search_relevant(
        [0.1] * 768,
        session_id="session-1",
        project="AADS",
        current_message_ids={"current-msg"},
    )

    assert [r["msg_id"] for r in results] == ["past-msg"]


def test_schedule_message_embedding_uses_background_task(monkeypatch):
    from app.services import chat_embedding_service as svc

    created = {}

    def _fake_create_task(coro):
        created["called"] = True
        coro.close()
        return object()

    monkeypatch.setattr(asyncio, "create_task", _fake_create_task)

    assert svc.schedule_message_embedding(object(), "msg-1", "충분히 긴 메시지 내용") is True
    assert created["called"] is True


def test_backfill_chat_embeddings_defaults_to_assistant_canary():
    from scripts import backfill_chat_embeddings as script

    args = script.parse_args([])

    assert args.role == "assistant"
    assert args.limit == 100
    assert args.batch_size == 20
    assert args.order == "newest"


def test_backfill_chat_embeddings_clamps_batch_size_to_limit():
    from scripts import backfill_chat_embeddings as script

    args = script.parse_args(["--limit", "7", "--batch-size", "20"])

    assert args.limit == 7
    assert args.batch_size == 7
