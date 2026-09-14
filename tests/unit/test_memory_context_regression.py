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


def test_backfill_chat_embeddings_requires_explicit_scope():
    """범위를 안 주면 거절해야 한다.

    2026-09-14 교체. 옛 스크립트는 `--limit 100` 만 받고 전체를 훑었는데,
    CPU Ollama 실측 처리량이 시간당 963건이라 전체(33,000건)는 34시간이다.
    끝날 시점을 모르는 작업을 띄우지 않는다(R-BG) — 범위를 강제한다.
    """
    import pytest

    from scripts import backfill_chat_embeddings as script

    parser_argv = ["run"]
    with pytest.raises(SystemExit):
        script.main_argv(parser_argv)

    for scope in (["run", "--roster"], ["run", "--session", "x"], ["run", "--all"]):
        args = script.build_parser().parse_args(scope)
        assert args.cmd == "run"
        assert args.max_seconds > 0, "시간 상한 기본값이 있어야 한다"


def test_backfill_chat_embeddings_never_writes_dummy():
    """이 사고의 원인은 '실패를 조용히 그럴듯한 값으로 덮은 것' 이었다.

    백필은 진짜 임베딩만 저장한다. 실패하면 None 을 돌려주고 그 회차를
    건너뛴다 — 더미를 만들지 않는다. 그리고 채운 것에는 세대를 적는다.
    """
    import inspect

    from scripts import backfill_chat_embeddings as script

    assert script.EMBED_VER == 2
    assert script.DOC_PREFIX.startswith("search_document")

    src = inspect.getsource(script.ollama_embed)
    assert "return None" in src
    assert "dummy" not in src.lower() or "더미를 만들지 않는다" in src

    run_src = inspect.getsource(script.cmd_run)
    assert "embedding_ver={EMBED_VER}" in run_src, (
        "채운 벡터에 세대를 안 적으면 다음 백필이 같은 행을 또 채운다"
    )
    assert script.ROSTER and "StrategyCardLead" in script.ROSTER
