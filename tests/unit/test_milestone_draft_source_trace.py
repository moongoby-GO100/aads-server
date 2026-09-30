"""Milestone drafts cite their source turns, and one draft traces message → revision → event."""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import uuid

import pytest

from app.services import directive_draft_service as drafts
from tests.unit.test_goal_milestone_draft_autogen import MemoryConnection, _advance

T0 = dt.datetime(2026, 9, 29, 7, 0, tzinfo=dt.timezone.utc)


def _message(conn, role, content, *, minutes, session_id=None, tenant_id=None):
    row = {
        "id": uuid.uuid4(), "role": role, "content": content,
        "created_at": T0 + dt.timedelta(minutes=minutes),
        "session_id": session_id or conn.session["id"],
        "tenant_id": tenant_id or conn.goal["tenant_id"],
    }
    conn.messages.append(row)
    return row


def _seed_conversation(conn, turns=6):
    return [
        _message(conn, "user" if i % 2 == 0 else "assistant", f"turn {i}", minutes=i)
        for i in range(turns)
    ]


def test_auto_advance_records_source_messages_of_the_linked_session(monkeypatch):
    conn = MemoryConnection()
    other_session = _message(conn, "user", "다른 세션", minutes=0, session_id=uuid.uuid4())
    other_tenant = _message(conn, "user", "다른 테넌트", minutes=0, tenant_id=uuid.uuid4())
    turns = _seed_conversation(conn)
    asyncio.run(_advance(conn, monkeypatch))

    assert len(conn.drafts) == 1
    source_ids = conn.drafts[0]["source_message_ids"]
    assert source_ids == [row["id"] for row in turns]
    assert other_session["id"] not in source_ids and other_tenant["id"] not in source_ids
    # Same window as the chat path: this session, this tenant, default 8 turns.
    assert conn.message_queries == [
        (conn.session["id"], conn.goal["tenant_id"], drafts.DEFAULT_CONTEXT_WINDOW)
    ]
    # Revision metadata carries the ids in the chat path's format.
    metadata = json.loads(conn.revisions[0][-1])
    assert metadata["source_message_ids"] == [str(value) for value in source_ids]
    assert metadata["milestone_id"] == str(conn.milestone["id"])


def test_window_is_capped_like_the_chat_path(monkeypatch):
    conn = MemoryConnection()
    turns = _seed_conversation(conn, turns=12)
    asyncio.run(_advance(conn, monkeypatch))
    assert conn.drafts[0]["source_message_ids"] == [row["id"] for row in turns[-8:]]


def test_unidentifiable_source_stays_empty_and_is_logged(monkeypatch, caplog):
    conn = MemoryConnection()
    _message(conn, "assistant", "사용자 요청 없는 응답", minutes=0)
    with caplog.at_level(logging.WARNING, logger=drafts.logger.name):
        asyncio.run(_advance(conn, monkeypatch))
    assert len(conn.drafts) == 1
    assert conn.drafts[0]["source_message_ids"] == []
    assert json.loads(conn.revisions[0][-1])["source_message_ids"] == []
    assert "milestone_draft_source_messages_unresolved" in caplog.text


def test_identical_retransition_keeps_one_draft_and_one_revision(monkeypatch):
    conn = MemoryConnection()
    _seed_conversation(conn)
    asyncio.run(_advance(conn, monkeypatch))
    conn.milestone_status = "pending"
    asyncio.run(_advance(conn, monkeypatch))
    assert len(conn.drafts) == 1
    assert conn.drafts[0]["current_revision"] == 1
    assert len(conn.revisions) == len(conn.events) == 1


def test_changed_retransition_adds_revision_with_sources(monkeypatch):
    conn = MemoryConnection()
    _seed_conversation(conn)
    asyncio.run(_advance(conn, monkeypatch))
    first_sources = conn.drafts[0]["source_message_ids"]
    newer = _message(conn, "user", "기준을 바꾸자", minutes=30)
    conn.milestone_status = "pending"
    conn.milestone["completion_criteria"] = "새 기준 확인"
    asyncio.run(_advance(conn, monkeypatch))

    assert len(conn.drafts) == 1
    assert conn.drafts[0]["current_revision"] == 2
    assert len(conn.revisions) == len(conn.events) == 2
    assert [args[2] for args in conn.revisions] == [1, 2]
    assert [args[2] for args in conn.events] == [1, 2]
    # The draft keeps its original origin; revision 2 cites the turns at its own time.
    assert conn.drafts[0]["source_message_ids"] == first_sources
    rev2_sources = json.loads(conn.revisions[1][-1])["source_message_ids"]
    assert rev2_sources[-1] == str(newer["id"])


def test_no_active_session_creates_no_draft(monkeypatch, caplog):
    conn = MemoryConnection(session=False)
    with caplog.at_level(logging.WARNING, logger=drafts.logger.name):
        asyncio.run(_advance(conn, monkeypatch))
    assert conn.milestone_status == "in_progress"
    assert not conn.drafts and not conn.revisions and not conn.events
    assert not conn.message_queries
    assert "milestone_draft_skipped_no_active_session" in caplog.text


# ── 3-stage trace ────────────────────────────────────────────────────────────

class TraceConnection:
    """Rows of one tenant's draft store, answering load_draft_trace queries."""

    def __init__(self):
        self.tenant_id = uuid.uuid4()
        self.session_id = uuid.uuid4()
        self.goal_id = uuid.uuid4()
        self.milestone_id = uuid.uuid4()
        self.messages = [
            {"id": uuid.uuid4(), "session_id": self.session_id, "tenant_id": self.tenant_id,
             "role": role, "created_at": T0, "preview": f"{role} text"}
            for role in ("user", "assistant")
        ]
        self.draft = {
            "id": uuid.uuid4(), "tenant_id": self.tenant_id, "session_id": self.session_id,
            "status": "draft", "current_revision": 2,
            "source_message_ids": [row["id"] for row in self.messages],
            "classification": json.dumps({
                "source_mode": "milestone_auto_advance",
                "goal_id": str(self.goal_id), "milestone_id": str(self.milestone_id),
            }),
            "created_at": T0,
        }
        ids = [str(row["id"]) for row in self.messages]
        self.revisions = [
            {"revision": n, "change_source": source, "created_at": T0,
             "metadata": json.dumps({"source_message_ids": ids})}
            for n, source in ((1, "fallback"), (2, "regenerated"))
        ]
        self.events = [
            {"revision": n, "action": action, "created_at": T0}
            for n, action in ((1, "created"), (2, "edited"))
        ]
        self.goal_linked = True

    async def fetchrow(self, query, *args):
        assert "FROM directive_drafts" in query
        draft_id, tenant_id = args
        if draft_id == self.draft["id"] and tenant_id == self.tenant_id:
            return self.draft
        return None

    async def fetch(self, query, *args):
        if "FROM chat_messages" in query:
            return [row for row in self.messages if row["id"] in args[0]]
        if "FROM directive_draft_revisions" in query:
            return self.revisions if args[1] == self.tenant_id else []
        if "FROM directive_draft_events" in query:
            return self.events if args[1] == self.tenant_id else []
        raise AssertionError(query)

    async def fetchval(self, query, *args):
        assert "FROM goals" in query and "goal_task_links" in query
        goal_id, tenant_id, milestone_id, session_id = args
        return (self.goal_linked and goal_id == str(self.goal_id)
                and tenant_id == self.tenant_id and milestone_id == str(self.milestone_id)
                and session_id == str(self.session_id))


def _trace(conn, tenant_id=None):
    return asyncio.run(drafts.load_draft_trace(
        conn, tenant_id=str(tenant_id or conn.tenant_id), draft_id=str(conn.draft["id"]),
    ))


def test_trace_links_messages_revisions_and_events():
    conn = TraceConnection()
    trace = _trace(conn)
    assert trace["linked"] is True
    assert [m["id"] for m in trace["source_messages"]] == [str(r["id"]) for r in conn.messages]
    assert all(m["in_draft_session"] and m["preview"] for m in trace["source_messages"])
    assert [r["revision"] for r in trace["revisions"]] == [1, 2]
    assert trace["revisions"][0]["source_message_ids"] == [str(r["id"]) for r in conn.messages]
    assert [(e["revision"], e["action"]) for e in trace["events"]] == [(1, "created"), (2, "edited")]
    assert trace["integrity"] == {
        "source_message_count": 2, "resolved_message_count": 2, "missing_message_ids": [],
        "foreign_session_messages": 0, "foreign_tenant_messages": 0,
        "goal_mismatch": 0, "events_without_revision": 0,
    }
    assert trace["draft"]["goal_id"] == str(conn.goal_id)


def test_trace_is_not_visible_to_another_tenant():
    conn = TraceConnection()
    with pytest.raises(drafts.DraftNotFoundError):
        _trace(conn, tenant_id=uuid.uuid4())


def test_trace_flags_foreign_tenant_and_session_messages():
    conn = TraceConnection()
    conn.messages[0]["tenant_id"] = uuid.uuid4()
    conn.messages[1]["session_id"] = uuid.uuid4()
    trace = _trace(conn)
    assert trace["linked"] is False
    assert trace["integrity"]["foreign_tenant_messages"] == 1
    assert trace["integrity"]["foreign_session_messages"] == 1
    assert all("preview" not in m for m in trace["source_messages"])


def test_trace_flags_goal_mismatch_and_missing_sources():
    conn = TraceConnection()
    conn.goal_linked = False
    conn.draft["source_message_ids"] = conn.draft["source_message_ids"] + [uuid.uuid4()]
    trace = _trace(conn)
    assert trace["linked"] is False
    assert trace["integrity"]["goal_mismatch"] == 1
    assert len(trace["integrity"]["missing_message_ids"]) == 1


def test_trace_of_unsourced_draft_is_not_linked():
    conn = TraceConnection()
    conn.draft["source_message_ids"] = []
    trace = _trace(conn)
    assert trace["linked"] is False
    assert trace["source_messages"] == []
    assert len(trace["revisions"]) == 2


# ── backfill: fill only rows whose source can be reproduced ─────────────────

def _load_backfill_script():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "scripts" / "backfill_milestone_draft_sources.py"
    spec = importlib.util.spec_from_file_location("backfill_milestone_draft_sources", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BackfillConnection:
    def __init__(self, candidates, messages):
        self.candidates = candidates
        self.messages = messages
        self.window_args = []

    async def fetch(self, query, *args):
        if "cardinality(d.source_message_ids) = 0" in query:
            return self.candidates
        if "FROM chat_messages" in query:
            self.window_args.append(args)
            session_id, tenant_id, limit, as_of = args
            rows = [m for m in self.messages if m["session_id"] == session_id
                    and m["tenant_id"] == tenant_id and m["created_at"] <= as_of]
            return rows[-limit:]
        raise AssertionError(query)


def _candidate(session_id, tenant_id, *, started_delta=dt.timedelta(milliseconds=8), linked=True):
    created = T0 + dt.timedelta(minutes=10)
    return {
        "id": uuid.uuid4(), "tenant_id": tenant_id, "session_id": session_id,
        "created_at": created, "goal_id": str(uuid.uuid4()), "milestone_id": str(uuid.uuid4()),
        "milestone_started_at": created - started_delta, "session_linked": linked,
    }


def test_backfill_plan_fills_only_reproducible_rows():
    script = _load_backfill_script()
    tenant, session, quiet_session = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    before = [
        {"id": uuid.uuid4(), "session_id": session, "tenant_id": tenant, "role": role,
         "content": role, "created_at": T0 + dt.timedelta(minutes=i)}
        for i, role in enumerate(("user", "assistant"))
    ]
    after = {"id": uuid.uuid4(), "session_id": session, "tenant_id": tenant, "role": "user",
             "content": "after", "created_at": T0 + dt.timedelta(minutes=11)}
    only_assistant = {"id": uuid.uuid4(), "session_id": quiet_session, "tenant_id": tenant,
                      "role": "assistant", "content": "x", "created_at": T0}
    candidates = [
        _candidate(session, tenant),
        _candidate(session, tenant, started_delta=dt.timedelta(hours=2)),
        _candidate(session, tenant, linked=False),
        _candidate(quiet_session, tenant),
    ]
    conn = BackfillConnection(candidates, before + [after, only_assistant])
    plans = asyncio.run(script.plan_backfill(conn))

    assert [p["decision"] for p in plans] == ["fill", "unresolved", "unresolved", "unresolved"]
    assert plans[0]["source_message_ids"] == [str(m["id"]) for m in before]
    assert str(after["id"]) not in plans[0]["source_message_ids"]
    assert plans[1]["reason"].startswith("transition_time_unknown")
    assert plans[2]["reason"] == "session_not_linked_to_goal"
    assert plans[3]["reason"].startswith("no_user_request_in_window")
    assert all(p["source_message_ids"] == [] for p in plans[1:])
    # The window is pinned to the draft's creation moment with the chat-path limit.
    assert conn.window_args[0] == (session, tenant, drafts.DEFAULT_CONTEXT_WINDOW,
                                   candidates[0]["created_at"])


def test_backfill_update_only_touches_still_empty_milestone_rows():
    script = _load_backfill_script()
    sql = " ".join(script.APPLY_SQL.split())
    assert "SET source_message_ids = $3::uuid[]" in sql
    assert "cardinality(source_message_ids) = 0" in sql
    assert "classification->>'source_mode' = 'milestone_auto_advance'" in sql
    assert "classification =" not in sql and "classification=" not in sql
