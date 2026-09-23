"""Review request recovery uses stored input and fences late workers."""

import asyncio
from datetime import datetime, timezone

import pytest

from app.api import code_review
from app.core import db_pool
from app.services import code_reviewer


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *_args):
        return False


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _Acquire(self.conn)


class _ResumeConn:
    def __init__(self, updated, selected=None):
        self.updated = updated
        self.selected = selected
        self.queries = []

    async def fetchrow(self, query, *args):
        self.queries.append((query, args))
        return self.updated if query.lstrip().startswith("UPDATE") else self.selected


def test_failed_review_resumes_with_saved_payload(monkeypatch):
    conn = _ResumeConn({"status": "queued"})
    scheduled = []
    monkeypatch.setattr(db_pool, "get_pool", lambda: _Pool(conn))
    monkeypatch.setattr(code_review, "_review_deadlines", lambda: (510, 600))
    monkeypatch.setattr(code_review, "_schedule_review_request", scheduled.append)

    request_id = code_review.uuid4()
    assert asyncio.run(code_review._resume_stored_request(request_id)) == "queued"
    assert scheduled == [request_id]
    query, args = conn.queries[0]
    assert "status='failed'" in query and "status='running'" in query
    assert "diff" not in query and "instruction" not in query
    assert args == (request_id, 600)


def test_completed_review_is_never_rescheduled(monkeypatch):
    conn = _ResumeConn(None, {"status": "completed"})
    scheduled = []
    monkeypatch.setattr(db_pool, "get_pool", lambda: _Pool(conn))
    monkeypatch.setattr(code_review, "_review_deadlines", lambda: (510, 600))
    monkeypatch.setattr(code_review, "_schedule_review_request", scheduled.append)

    assert asyncio.run(code_review._resume_stored_request(code_review.uuid4())) == "completed"
    assert scheduled == []


def test_missing_review_is_404(monkeypatch):
    conn = _ResumeConn(None, None)
    monkeypatch.setattr(db_pool, "get_pool", lambda: _Pool(conn))
    monkeypatch.setattr(code_review, "_review_deadlines", lambda: (510, 600))

    with pytest.raises(code_review.HTTPException) as exc:
        asyncio.run(code_review._resume_stored_request(code_review.uuid4()))
    assert exc.value.status_code == 404


def test_hard_deadline_fails_request_with_claim_fence(monkeypatch):
    now = datetime.now(timezone.utc)

    class Conn:
        def __init__(self):
            self.writes = []

        async def fetchrow(self, query, *_args):
            assert "status='queued'" in query
            return {
                "job_id": "runner-test001",
                "project": "AADS",
                "diff": "diff --git a/a b/a\n",
                "instruction": "review",
                "files_changed": ["a"],
                "created_at": now,
                "started_at": now,
                "attempts": 2,
            }

        async def execute(self, query, *args):
            self.writes.append((query, args))

    async def slow_review(**_kwargs):
        await asyncio.sleep(0.1)

    conn = Conn()
    monkeypatch.setattr(db_pool, "get_pool", lambda: _Pool(conn))
    monkeypatch.setattr(code_reviewer, "review_code_diff", slow_review)
    monkeypatch.setattr(code_review, "_review_deadlines", lambda: (0.01, 600))
    monkeypatch.setattr(code_review, "_review_concurrency", asyncio.Semaphore(2))

    asyncio.run(code_review._execute_review_request(code_review.uuid4()))
    assert len(conn.writes) == 1
    query, args = conn.writes[0]
    assert "SET status='failed'" in query
    assert "AND attempts=$3" in query
    assert args[-1] == 2
