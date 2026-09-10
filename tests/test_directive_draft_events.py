from __future__ import annotations

import uuid

import pytest

from app.services import directive_draft_service as service


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


class _Connection:
    def __init__(self, draft: dict):
        self.draft = draft
        self.executed: list[tuple[str, tuple[object, ...]]] = []

    def transaction(self):
        return _Transaction()

    async def fetchrow(self, query: str, *args):
        if "SELECT * FROM directive_drafts" in query:
            return self.draft
        if "UPDATE directive_drafts SET status" in query:
            self.draft = {**self.draft, "status": args[2]}
            return self.draft
        raise AssertionError(f"unexpected fetchrow query: {query}")

    async def execute(self, query: str, *args):
        normalized = " ".join(query.split())
        if "UPDATE chat_artifacts" in normalized:
            assert "jsonb_build_object('status', $3::text)" in normalized
        self.executed.append((normalized, args))
        return "OK"


class _Acquire:
    def __init__(self, conn: _Connection):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *_args):
        return None


class _Pool:
    def __init__(self, conn: _Connection):
        self.conn = conn

    def acquire(self):
        return _Acquire(self.conn)


@pytest.mark.asyncio
async def test_sent_event_updates_artifact_with_explicit_postgres_text_type(monkeypatch) -> None:
    tenant_id = uuid.uuid4()
    draft_id = uuid.uuid4()
    artifact_id = uuid.uuid4()
    conn = _Connection(
        {
            "id": draft_id,
            "tenant_id": tenant_id,
            "artifact_id": artifact_id,
            "current_revision": 2,
            "status": "draft",
        }
    )
    monkeypatch.setattr(service, "get_pool", lambda: _Pool(conn))

    result = await service.record_event(
        tenant_id=str(tenant_id),
        user_id=None,
        draft_id=str(draft_id),
        action="sent",
        metadata={"source": "composer"},
    )

    assert result["status"] == "sent"
    assert any("INSERT INTO directive_draft_events" in query for query, _ in conn.executed)
