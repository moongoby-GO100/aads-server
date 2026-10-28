"""담당 없는 막힘 마일스톤·업무 -> 주도(또는 마일스톤 담당)에게 정리 요청.

재착수(dispatch_count 증가·상태 변경)는 하지 않고, 같은 마일스톤에는 24시간에
한 번만 보내며, 주도도 담당도 없으면 보내지 않고 사유 경로로만 넘긴다.
"""
from __future__ import annotations

import asyncio

import pytest

from app.services import goal_dispatch


class _Conn:
    def __init__(self, rows, items=None, failed_job=None, stamped=None):
        self.rows = rows
        self.items = items or {}
        self.failed_job = failed_job
        self.stamped = stamped if stamped is not None else set()
        self.executed: list[str] = []
        self.claims: list[str] = []

    async def execute(self, sql, *args):
        self.executed.append(sql)
        return "UPDATE 0"

    async def fetch(self, sql, *args):
        if "FROM work_items w" in sql and "ORDER BY CASE" in sql:
            return self.items.get(args[0], [])
        return self.rows

    async def fetchval(self, sql, *args):
        return self.failed_job

    async def fetchrow(self, sql, *args):
        assert "lead_triage_at = NOW()" in sql, sql[:80]
        mid = args[0]
        if mid in self.stamped:
            return None
        self.stamped.add(mid)
        self.claims.append(mid)
        return {"lead_triage_at": "now"}


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *_exc):
                return False

        return _Ctx()

    async def execute(self, *_a, **_k):
        return "UPDATE 1"


def _row(n=1, *, status="blocked", owner="", lead="lead-1", goal="g1", **extra):
    base = {
        "milestone_id": f"{n:08d}-0000-0000-0000-000000000000",
        "milestone_title": f"M{n}",
        "milestone_status": status,
        "dispatch_blocked_at": None,
        "dispatch_note": "3회 보냈으나 답이 확인되지 않음",
        "lead_triage_at": None,
        "goal_title": "목표",
        "project": "AADS",
        "goal_id": goal,
        "owner_role_key": "",
        "goal_lead_session_id": lead,
        "session_id": owner,
        "unassigned_count": 0,
    }
    base.update(extra)
    return base


@pytest.fixture
def harness(monkeypatch):
    from app.core import db_pool
    from app.services import chat_service, orchestration_limits

    async def _open(*_a, **_k):
        return (False, "")

    async def _ok(*_a, **_k):
        return (True, "")

    monkeypatch.setattr(orchestration_limits, "owner_paused", _open)
    monkeypatch.setattr(orchestration_limits, "goal_paused", _open)
    monkeypatch.setattr(orchestration_limits, "cost_gate", _ok)
    monkeypatch.setattr(orchestration_limits, "load_gate", _ok)
    monkeypatch.setattr(goal_dispatch, "_ENABLED", True)
    monkeypatch.setattr(goal_dispatch, "_LEAD_TRIAGE_ENABLED", True)

    sent: list[tuple[str, str]] = []

    async def _stream(*, session_id, content, **_kw):
        sent.append((session_id, content))
        return
        yield  # pragma: no cover

    async def _no_standby(*_a, **_k):
        return False

    monkeypatch.setattr(chat_service, "send_message_stream", _stream)
    monkeypatch.setattr(chat_service, "handoff_internal_turn_if_standby", _no_standby)

    def install(conn):
        monkeypatch.setattr(db_pool, "get_pool", lambda: _Pool(conn))

    return install, sent


async def _run():
    result = await goal_dispatch.triage_unowned_milestones(None)
    tasks = list(goal_dispatch._send_tasks)
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    return result


def test_blocked_unowned_milestone_goes_to_lead_without_redispatch(harness):
    install, sent = harness
    conn = _Conn(
        [_row(1, unassigned_count=2)],
        items={"00000001-0000-0000-0000-000000000000": [
            {"type": "epic", "title": "에픽A", "status": "draft"},
            {"type": "task", "title": "태스크B", "status": "ready"},
        ]},
        failed_job="runner-deadbeef",
    )
    install(conn)
    result = asyncio.run(_run())

    assert result["sent"] == 1
    assert [s for s, _ in sent] == ["lead-1"]
    text = sent[0][1]
    for needle in ("[목표 정리 요청]", "M1", "blocked", "runner-deadbeef",
                   "담당 없는 업무 2건", "[에픽] 에픽A (draft)", "[태스크] 태스크B (ready)",
                   "담당 지정", "업무 재배정", "막힘 해소 판단", "취소"):
        assert needle in text, needle
    # 재착수 흔적이 없다: dispatch_count·status 를 건드리는 SQL 이 없다.
    assert not any("dispatch_count" in q or "SET status" in q for q in conn.executed)
    assert conn.claims == ["00000001-0000-0000-0000-000000000000"]


def test_same_milestone_only_once_per_24h(harness):
    install, sent = harness
    conn = _Conn([_row(1)])
    install(conn)
    first = asyncio.run(_run())
    second = asyncio.run(_run())
    assert first["sent"] == 1
    assert second["sent"] == 0 and second["skipped"] == 1
    assert len(sent) == 1


def test_goal_without_lead_sends_nothing_and_uses_missing_owner_path(harness, monkeypatch):
    install, sent = harness
    calls: list[dict] = []

    async def _fake_missing(conn, row, *, remaining, write_notes=True):
        calls.append({"milestone": row["milestone_id"], "remaining": remaining,
                      "write_notes": write_notes})
        return 0

    monkeypatch.setattr(goal_dispatch, "_handle_missing_owner", _fake_missing)
    conn = _Conn([_row(1, lead="")])
    install(conn)
    result = asyncio.run(_run())

    assert result["sent"] == 0 and result["no_lead"] == 1
    assert sent == []
    assert len(calls) == 1 and calls[0]["write_notes"] is True


def test_no_lead_keeps_block_reason_note(harness, monkeypatch):
    install, _ = harness
    calls: list[bool] = []

    async def _fake_missing(conn, row, *, remaining, write_notes=True):
        calls.append(write_notes)
        return 0

    monkeypatch.setattr(goal_dispatch, "_handle_missing_owner", _fake_missing)
    install(_Conn([_row(1, lead="", dispatch_blocked_at="2026-10-28T00:00:00+00:00")]))
    asyncio.run(_run())
    assert calls == [False]


def test_owned_milestone_unassigned_items_go_to_owner_in_one_bundle(harness):
    install, sent = harness
    mid = "00000002-0000-0000-0000-000000000000"
    items = [{"type": "task", "title": f"T{i}", "status": "draft"} for i in range(3)]
    install(_Conn([_row(2, status="in_progress", owner="owner-9", unassigned_count=3)],
                  items={mid: items}))
    result = asyncio.run(_run())

    assert result["sent"] == 1
    assert len(sent) == 1 and sent[0][0] == "owner-9"
    text = sent[0][1]
    assert "담당 없는 업무 3건" in text and "이 마일스톤 담당인 너" in text
    assert "막힌 사유" not in text


def test_owned_milestone_without_items_is_skipped(harness):
    install, sent = harness
    install(_Conn([_row(3, status="in_progress", owner="owner-9")]))
    result = asyncio.run(_run())
    assert result["sent"] == 0 and sent == []


def test_cycle_cap_follows_owner_request_limit(harness, monkeypatch):
    install, sent = harness
    monkeypatch.setattr(goal_dispatch, "_OWNER_REQUEST_PER_CYCLE", 2)
    rows = [_row(n, lead=f"lead-{n}", goal=f"g{n}") for n in range(1, 5)]
    install(_Conn(rows))
    result = asyncio.run(_run())
    assert result["sent"] == 2 and len(sent) == 2


def test_paused_goal_is_respected_and_not_stamped(harness, monkeypatch):
    from app.services import orchestration_limits

    install, sent = harness

    async def _paused(*_a, **_k):
        return (True, "멈춤")

    monkeypatch.setattr(orchestration_limits, "goal_paused", _paused)
    conn = _Conn([_row(1)])
    install(conn)
    result = asyncio.run(_run())
    assert result["sent"] == 0 and result["skipped"] == 1
    assert conn.claims == [] and sent == []


def test_cost_gate_and_owner_pause_are_respected(harness, monkeypatch):
    from app.services import orchestration_limits

    install, sent = harness

    async def _closed(*_a, **_k):
        return (False, "비용 상한")

    monkeypatch.setattr(orchestration_limits, "cost_gate", _closed)
    conn = _Conn([_row(1)])
    install(conn)
    assert asyncio.run(_run())["sent"] == 0
    assert conn.claims == []

    async def _owner_paused(*_a, **_k):
        return (True, "멈춤")

    async def _ok(*_a, **_k):
        return (True, "")

    monkeypatch.setattr(orchestration_limits, "cost_gate", _ok)
    monkeypatch.setattr(orchestration_limits, "owner_paused", _owner_paused)
    conn2 = _Conn([_row(1)])
    install(conn2)
    assert asyncio.run(_run())["sent"] == 0
    assert conn2.claims == [] and sent == []


def test_one_message_per_recipient_per_cycle(harness):
    install, sent = harness
    install(_Conn([_row(1), _row(2)]))
    result = asyncio.run(_run())
    assert result["sent"] == 1 and result["skipped"] == 1
    assert len(sent) == 1
