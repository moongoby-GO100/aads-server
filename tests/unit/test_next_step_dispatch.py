"""Approved and auto next steps must have durable, complete delivery."""
from __future__ import annotations

import asyncio

from app.api import pipeline_runner, project_docs
from app.services import chat_service, next_step_proposals


SESSION = "765160d6-facb-4de4-8b36-8591b9466d68"
REQUEST = "ab081897-51b9-4a42-b22c-1158b8ac2260"
SOURCE = "a297f497-5a1e-4f0d-9c06-f536717d36d4"


def _row(title: str, decision: str, *, active: bool = True) -> dict:
    return {
        "id": title,
        "action_summary": f"[{title}] 작업 설명과 실행 범위",
        "decision": decision,
        "active": active,
        "source_message_id": SOURCE,
    }


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *_):
        return False


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _Acquire(self.conn)


class _ApprovalConn:
    def __init__(self, rows):
        self.rows = rows
        self.statements = []

    async def execute(self, sql, *args):
        self.statements.append((sql, args))

    async def fetch(self, sql, *args):
        assert "source_message_id" in sql
        return self.rows

    async def fetchval(self, *_):
        raise AssertionError("next_step must not wait on unrelated pending cards")


def test_batch_prompt_keeps_every_approval_and_excludes_rejections():
    prompt = project_docs._next_step_decision_prompt([
        _row("P0", "approved"),
        _row("P1", "approved"),
        _row("P2", "rejected"),
        _row("expired", "approved", active=False),
    ])
    assert prompt.index("P0") < prompt.index("P1")
    approved_part, skipped_part = prompt.split("실행하지 않을 거절·만료 항목:")
    assert "P0" in approved_part and "P1" in approved_part
    assert "P2" not in approved_part and "expired" not in approved_part
    assert "P2" in skipped_part and "expired" in skipped_part


def test_three_approvals_queue_one_reaction_with_all_items(monkeypatch):
    rows = [_row("P0", "pending"), _row("P1", "pending"), _row("P2", "approved")]
    conn = _ApprovalConn(rows)
    from app.core import db_pool

    monkeypatch.setattr(db_pool, "get_pool", lambda: _Pool(conn))
    queued = []

    async def fake_enqueue(session_id, prompt, **kwargs):
        queued.append((session_id, prompt, kwargs))
        return {"queue_id": "queue-1", "queue_status": "pending", "created": True}

    monkeypatch.setattr(chat_service, "enqueue_next_step_reaction", fake_enqueue)

    async def notify(request_id, summary):
        return await project_docs._notify_chat_of_approval_decision(
            session_id=SESSION, request_id=request_id, tool="next_step",
            summary=summary, decision="approved", scope="project",
            grant_executions=100, hours=8,
        )

    assert asyncio.run(notify(REQUEST, "P2")) == {"status": "waiting_for_decisions"}
    assert queued == []
    rows[1]["decision"] = "approved"
    assert asyncio.run(notify(REQUEST, "P1")) == {"status": "waiting_for_decisions"}
    assert queued == []
    rows[0]["decision"] = "approved"
    assert asyncio.run(notify(REQUEST, "P0")) == {
        "status": "pending", "queue_id": "queue-1", "created": True,
    }
    assert len(queued) == 1
    sid, prompt, kwargs = queued[0]
    assert sid == SESSION
    assert prompt.index("P0") < prompt.index("P1") < prompt.index("P2")
    assert kwargs["dedupe_key"] == f"next_step:approval:{SESSION}:{SOURCE}"


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False


class _QueueConn:
    def __init__(self):
        self.saved = None
        self.lock_calls = 0

    def transaction(self):
        return _Transaction()

    async def execute(self, sql, *args):
        assert "pg_advisory_xact_lock" in sql
        self.lock_calls += 1

    async def fetchrow(self, sql, *args):
        if "SELECT id::text" in sql:
            return self.saved
        assert "INSERT INTO chat_deferred_reactions" in sql
        assert "⚠️ 이 메시지는 자동 트리거" in args[1]
        self.saved = {"id": "queue-1", "status": "pending"}
        return self.saved


def test_durable_queue_reuses_same_key_and_applies_safety_guard(monkeypatch):
    conn = _QueueConn()
    monkeypatch.setattr(chat_service, "get_pool", lambda: _Pool(conn))

    async def run():
        first = await chat_service.enqueue_next_step_reaction(
            SESSION, "P0 수행", dedupe_key="next_step:approval:batch-1",
        )
        second = await chat_service.enqueue_next_step_reaction(
            SESSION, "P0 수행", dedupe_key="next_step:approval:batch-1",
        )
        return first, second

    first, second = asyncio.run(run())
    assert first == {"queue_id": "queue-1", "queue_status": "pending", "created": True}
    assert second == {"queue_id": "queue-1", "queue_status": "pending", "created": False}
    assert conn.lock_calls == 2


def test_auto_is_reported_queued_only_after_durable_insert(monkeypatch):
    from app.core import db_pool

    class AutoConn:
        async def fetchval(self, *_):
            return "11111111-1111-4111-8111-111111111111"

    monkeypatch.setattr(db_pool, "get_pool", lambda: _Pool(AutoConn()))

    async def bubble(*_):
        return SOURCE

    async def project(*_):
        return "AADS"

    async def grant(*_):
        return "grant-id"

    monkeypatch.setattr(next_step_proposals, "_current_bubble_id", bubble)
    monkeypatch.setattr(next_step_proposals, "_session_project", project)
    monkeypatch.setattr(next_step_proposals, "_covered_by_existing_grant", grant)

    async def queued(*_, **__):
        return {"queue_id": "queue-2", "queue_status": "pending", "created": True}

    monkeypatch.setattr(chat_service, "enqueue_next_step_reaction", queued)
    step = {"title": "P1 검사", "detail": "실제 검사", "tool": "pipeline_runner_submit"}
    result = asyncio.run(next_step_proposals.propose(SESSION, [step]))
    assert result["auto_fired"] == 1
    assert result["auto_enqueue_failed"] == 0
    assert result["auto"][0]["queue_id"] == "queue-2"
    assert result["auto"][0]["queued"] is True

    async def reused(*_, **__):
        return {"queue_id": "queue-2", "queue_status": "claimed", "created": False}

    monkeypatch.setattr(chat_service, "enqueue_next_step_reaction", reused)
    repeated = asyncio.run(next_step_proposals.propose(SESSION, [step]))
    assert repeated["auto_fired"] == 0
    assert repeated["auto"][0]["queue_id"] == "queue-2"
    assert repeated["auto"][0]["queue_status"] == "claimed"

    async def broken(*_, **__):
        raise RuntimeError("queue unavailable")

    monkeypatch.setattr(chat_service, "enqueue_next_step_reaction", broken)
    failed = asyncio.run(next_step_proposals.propose(SESSION, [step]))
    assert failed["auto_fired"] == 0
    assert failed["auto_enqueue_failed"] == 1
    assert failed["auto"][0]["queued"] is False
    assert "queue_id" not in failed["auto"][0]


def test_review_hold_is_not_presented_as_ceo_approval():
    display = pipeline_runner._runner_display_status(
        "review_hold", "review_hold", "REVIEW_MODEL_NO_RESPONSE",
    )
    assert display["approval_available"] is False
    assert "승인 버튼" in display["action_hint"]
    assert pipeline_runner._runner_display_status(
        "awaiting_approval", "awaiting_approval", "",
    )["approval_available"] is True
