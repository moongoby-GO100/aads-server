"""추가 지시(interrupt) 접수→반영 수명주기 계약 테스트.

가짜 DB 는 실제 SQL 을 해석하지 않고 `-- ilc:<op>` 태그와 라우터 쿼리의 식별 문구로
분기한다. 호출 횟수가 아니라 DB 상태(상태 행·원문 행·프로세스 큐)를 검증한다.
트랜잭션은 예외 시 상태를 되돌려 "저장 실패 = 흔적 없음"을 그대로 재현한다.
"""

from __future__ import annotations

import copy
import itertools
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

os.environ.setdefault("JWT_SECRET_KEY", "unit-test-secret")

from app.core import interrupt_queue as iq
from app.routers import chat as chat_router
from app.services import chat_interrupt_lifecycle as ilc

EXECUTION_ID = "11111111-1111-4111-8111-111111111111"
GENERATION_ID = "22222222-2222-4222-8222-222222222222"
_TICK = itertools.count()
_BASE = datetime(2026, 10, 3, 3, 0, 0, tzinfo=timezone.utc)


def _now() -> datetime:
    return _BASE + timedelta(milliseconds=next(_TICK))


class _FakeDB:
    """공유 저장소 — 연결(=슬롯/커넥션)이 바뀌어도 이 상태는 그대로다."""

    def __init__(self, session_id: uuid.UUID):
        self.session_id = session_id
        self.messages: dict[uuid.UUID, dict] = {}
        self.states: dict[uuid.UUID, dict] = {}
        self.message_count = 0
        self.execution = {
            "execution_id": EXECUTION_ID,
            "status": "running",
            "error_message": None,
            "last_event_id": "1-0",
            "updated_age_seconds": 2,
            "started_age_seconds": 20,
            "partial_content": "",
            "tools_called": [],
        }
        self.fail_state_insert: Exception | None = None
        self.fail_snapshot: Exception | None = None
        self.fail_apply: Exception | None = None
        self.state_table_exists = True
        self.current_execution = EXECUTION_ID
        self.on_lock = None
        self.queried_tags: list[str] = []

    def snapshot(self):
        return copy.deepcopy((self.messages, self.states, self.message_count))

    def restore(self, saved):
        self.messages, self.states, self.message_count = copy.deepcopy(saved)


class _InFailedSQLTransactionError(Exception):
    pass


class _Tx:
    """asyncpg 트랜잭션/savepoint 흉내. 문장이 실패했는데 이 컨텍스트 안에서 예외가 삼켜지면
    (=savepoint 로 감싸지 않았으면) 이후 문장은 InFailedSQLTransaction 으로 막힌다."""

    def __init__(self, db: _FakeDB, conn: "_FakeConn"):
        self.db = db
        self.conn = conn
        self.saved = None
        self.failed = False

    async def __aenter__(self):
        self.conn._check_alive()
        self.saved = self.db.snapshot()
        self.conn.stack.append(self)
        return self

    async def __aexit__(self, exc_type, _exc, _tb):
        self.conn.stack.remove(self)
        if exc_type is not None or self.failed:
            self.db.restore(self.saved)
        return False


class _UndefinedTableError(Exception):
    pass


_UndefinedTableError.__name__ = "UndefinedTableError"


def _tag(sql: str) -> str:
    m = re.search(r"-- ilc:(\w+)", sql)
    return m.group(1) if m else ""


class _FakeConn:
    def __init__(self, db: _FakeDB):
        self.db = db
        self.stack: list[_Tx] = []

    def transaction(self):
        return _Tx(self.db, self)

    def _check_alive(self):
        if any(t.failed for t in self.stack):
            raise _InFailedSQLTransactionError("current transaction is aborted")

    async def _run(self, fn, sql, args):
        self._check_alive()
        tag = _tag(sql)
        if tag:
            self.db.queried_tags.append(tag)
        try:
            return await fn(sql, *args)
        except Exception:
            if self.stack:
                self.stack[-1].failed = True
            raise

    async def fetchval(self, sql, *args):
        return await self._run(self._fetchval, sql, args)

    async def fetchrow(self, sql, *args):
        return await self._run(self._fetchrow, sql, args)

    async def fetch(self, sql, *args):
        return await self._run(self._fetch, sql, args)

    async def execute(self, sql, *args):
        return await self._run(self._execute, sql, args)

    def _need_table(self):
        if not self.db.state_table_exists:
            raise _UndefinedTableError("relation chat_interrupt_states does not exist")

    # ── fetchval ──
    async def _fetchval(self, sql, *args):
        db, tag = self.db, _tag(sql)
        if tag == "lock_idem":
            if db.on_lock:
                db.on_lock(db)
            return None
        if tag == "find_idem":
            self._need_table()
            for row in db.states.values():
                if row["session_id"] == args[0] and row["idempotency_key"] == args[1]:
                    return str(row["message_id"])
            return None
        if "newer.session_id" in sql:
            return False
        if "regexp_replace(content" in sql:
            for mid, m in db.messages.items():
                body = re.sub(r"^\[[^\]]*\]\s*", "", m["content"])
                if m["session_id"] == args[0] and body == args[1] and (m["intent"] or "") in ("", "queued_interrupt"):
                    return mid
            return None
        if "chat_execution_generations" in sql:
            return GENERATION_ID
        if "INSERT INTO chat_messages" in sql:
            mid = uuid.uuid4()
            db.messages[mid] = {
                "session_id": args[0], "content": args[1], "intent": "queued_interrupt",
                "created_at": _now(), "edited_at": None,
            }
            return mid
        if "SELECT COUNT(*)" in sql:
            return sum(
                1 for mid, m in db.messages.items()
                if m["intent"] in ("interrupt_applied", "interrupt_completed", "recovered_interrupt")
            )
        raise AssertionError(f"unexpected fetchval: {sql[:80]}")

    # ── fetchrow ──
    async def _fetchrow(self, sql, *args):
        db, tag = self.db, _tag(sql)
        if tag == "insert":
            if db.fail_state_insert:
                raise db.fail_state_insert
            self._need_table()
            mid, sid, state, reason, summary, idem, exe, gen, owner = args
            if mid in db.states:
                return None
            if idem and any(r["session_id"] == sid and r["idempotency_key"] == idem for r in db.states.values()):
                raise RuntimeError("duplicate key value violates unique constraint uq_chat_interrupt_states_idem")
            now = _now()
            db.states[mid] = {
                "message_id": mid, "session_id": sid, "state": state, "wait_reason": reason,
                "summary": summary, "public_reply": None, "idempotency_key": idem,
                "execution_id": exe, "generation_id": gen, "owner_instance": owner,
                "applied_execution_id": None,
                "received_at": now, "queued_at": now if state == "QUEUED" else None,
                "applied_at": None, "working_at": None, "done_at": None,
            }
            return dict(db.states[mid])
        if tag == "get":
            self._need_table()
            row = db.states.get(uuid.UUID(str(args[0])))
            return dict(row) if row else None
        if tag == "apply_lock":
            mid, sid = args
            row = db.states.get(mid)
            if not row or row["session_id"] != sid or row["state"] not in ("RECEIVED", "QUEUED"):
                return None
            return {"summary": row["summary"], "wait_reason": row["wait_reason"]}
        if tag == "msg":
            m = db.messages.get(args[0])
            if not m:
                return None
            return {"message_id": args[0], "intent": m["intent"], "content": m["content"],
                    "created_at": m["created_at"], "edited_at": m["edited_at"]}
        if tag == "ctx":
            return {"execution_id": db.current_execution, "generation_id": GENERATION_ID}
        if "te.id::text AS execution_id" in sql:
            return dict(db.execution)
        raise AssertionError(f"unexpected fetchrow: {sql[:80]}")

    # ── fetch ──
    async def _fetch(self, sql, *args):
        db, tag = self.db, _tag(sql)
        if tag == "snapshot":
            if db.fail_snapshot:
                raise db.fail_snapshot
            self._need_table()
            rows = [dict(r) for r in db.states.values() if r["session_id"] == args[0]]
            return sorted(rows, key=lambda r: r["received_at"])
        if tag in ("legacy", "legacy_nostate"):
            if tag == "legacy":
                self._need_table()
            out = []
            for mid, m in db.messages.items():
                if (m["session_id"] == args[0] and (tag == "legacy_nostate" or mid not in db.states)
                        and m["intent"] in ("queued_interrupt", "interrupt_applied", "interrupt_completed")):
                    out.append({"message_id": mid, "intent": m["intent"], "content": m["content"],
                                "created_at": m["created_at"], "edited_at": m["edited_at"]})
            return out
        if "UPDATE chat_messages m" in sql:
            targets = args[1]
            hit = []
            for mid, m in db.messages.items():
                if (m["session_id"] == args[0] and m["intent"] in ("queued_interrupt", "interrupt_needs_confirm")
                        and (targets is None or mid in targets)):
                    m["intent"] = "interrupt_cancelled"
                    hit.append({"id": mid, "content": m["content"]})
            return hit
        raise AssertionError(f"unexpected fetch: {sql[:80]}")

    # ── execute ──
    async def _execute(self, sql, *args):
        db, tag = self.db, _tag(sql)
        if "UPDATE chat_sessions SET message_count" in sql:
            db.message_count += 1
            return "UPDATE 1"
        if tag == "apply":
            if db.fail_apply:
                raise db.fail_apply
            mid, exe, gen, reply = args
            row = db.states[mid]
            row.update(state="APPLIED", applied_at=_now(),
                       execution_id=row["execution_id"] or exe,
                       generation_id=row["generation_id"] or gen,
                       applied_execution_id=exe, public_reply=reply, wait_reason="none")
            return "UPDATE 1"
        if tag == "working":
            self._need_table()
            n = 0
            for r in db.states.values():
                if (r["session_id"] == args[0] and r["state"] == "APPLIED"
                        and (args[1] is None or r["applied_execution_id"] in (None, args[1]))):
                    r.update(state="WORKING", working_at=_now())
                    n += 1
            return f"UPDATE {n}"
        if tag == "done":
            self._need_table()
            n = 0
            for r in db.states.values():
                if (r["session_id"] == args[0] and r["state"] in ("APPLIED", "WORKING")
                        and (args[1] is None or r["applied_execution_id"] in (None, args[1]))):
                    r.update(state="DONE", done_at=_now(), wait_reason="none")
                    n += 1
            return f"UPDATE {n}"
        if tag == "cancel":
            self._need_table()
            n = 0
            for r in db.states.values():
                if (r["session_id"] == args[0] and r["message_id"] in args[1]
                        and r["state"] in ("RECEIVED", "QUEUED")):
                    r.update(state="CANCELLED", wait_reason="none")
                    n += 1
            return f"UPDATE {n}"
        if tag == "refresh":
            self._need_table()
            n = 0
            for r in db.states.values():
                if (r["session_id"] == args[0] and r["state"] in ("RECEIVED", "QUEUED")
                        and r["wait_reason"] != args[1]):
                    r["wait_reason"] = args[1]
                    n += 1
            return f"UPDATE {n}"
        if tag == "expire":
            self._need_table()
            n = 0
            for r in db.states.values():
                m = db.messages.get(r["message_id"])
                if m and m["intent"] == "interrupt_expired" and r["state"] in ("RECEIVED", "QUEUED"):
                    r.update(state="EXPIRED", wait_reason="none")
                    n += 1
            return f"UPDATE {n}"
        raise AssertionError(f"unexpected execute: {sql[:80]}")


class _Acquire:
    def __init__(self, db):
        self.db = db

    async def __aenter__(self):
        return _FakeConn(self.db)

    async def __aexit__(self, *_a):
        return False


class _Pool:
    def __init__(self, db):
        self.db = db

    def acquire(self):
        return _Acquire(self.db)


@pytest.fixture
def env():
    session_id = uuid.uuid4()
    db = _FakeDB(session_id)
    iq._interrupt_queues.clear()
    iq._pending_interrupts.clear()
    with (
        patch("app.core.db_pool.get_pool", side_effect=lambda: _Pool(db)),
        patch.object(chat_router.svc, "get_session", AsyncMock(return_value={"id": str(session_id)})),
        patch.object(chat_router, "is_streaming", return_value=True),
        patch.object(chat_router, "set_streaming"),
    ):
        yield db
    iq._interrupt_queues.clear()
    iq._pending_interrupts.clear()


async def _send(db: _FakeDB, content: str, *, key: str | None = None):
    req = chat_router.InterruptRequest(content=content, attachments=[], idempotency_key=key)
    return await chat_router.interrupt_session(db.session_id, req, {"tenant": {"id": str(uuid.uuid4())}})


async def _snapshot(db: _FakeDB):
    return await ilc.fetch_snapshot(_FakeConn(db), db.session_id)


def _queued(db: _FakeDB) -> list[dict]:
    return list(iq._interrupt_queues.get(str(db.session_id), []))


# ── 접수 ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_single_instruction_returns_receipt_and_is_durable(env):
    res = await _send(env, "  보고서 \n 제목을 바꿔줘 ")

    assert res["queued"] is True and res["duplicate"] is False
    assert res["state"] == "QUEUED"
    assert res["wait_reason"] == "model_pre_output"
    assert res["message_id"] == res["command_id"]
    assert res["summary"] == "보고서 제목을 바꿔줘"
    assert res["execution_id"] == EXECUTION_ID and res["generation_id"] == GENERATION_ID
    mid = uuid.UUID(res["message_id"])
    assert env.states[mid]["state"] == "QUEUED"
    assert env.states[mid]["queued_at"] is not None
    assert env.messages[mid]["intent"] == "queued_interrupt"
    assert [q["message_id"] for q in _queued(env)] == [res["message_id"]]


@pytest.mark.asyncio
async def test_three_instructions_keep_arrival_order_and_distinct_ids(env):
    receipts = [await _send(env, f"지시 {n}") for n in (1, 2, 3)]

    ids = [r["message_id"] for r in receipts]
    assert len(set(ids)) == 3
    snap = await _snapshot(env)
    assert [i["id"] for i in snap] == ids
    assert [i["summary"] for i in snap] == ["지시 1", "지시 2", "지시 3"]
    assert {i["state"] for i in snap} == {"QUEUED"}
    assert env.message_count == 3
    assert [q["message_id"] for q in _queued(env)] == ids


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tools, status, error, partial, expected",
    [
        ([{"type": "tool_use", "tool_name": "run_remote_command", "tool_use_id": "t1"}], "running", None, "", "tool_running"),
        ([], "retrying", "codex_relay_busy: waiting", "", "relay_wait"),
        ([], "running", None, "", "model_pre_output"),
    ],
)
async def test_wait_reason_is_reported_in_receipt(env, tools, status, error, partial, expected):
    env.execution.update(tools_called=tools, status=status, error_message=error, partial_content=partial)

    res = await _send(env, "우선순위 바꿔줘")

    assert res["wait_reason"] == expected
    assert env.states[uuid.UUID(res["message_id"])]["wait_reason"] == expected


def test_classify_wait_reason_tool_result_clears_tool_running():
    events = [
        {"type": "tool_use", "tool_name": "x", "tool_use_id": "t1"},
        {"type": "tool_result", "tool_name": "x", "tool_use_id": "t1"},
    ]
    assert ilc.classify_wait_reason(status="running", tool_events=events, has_output=True) == "none"
    assert ilc.classify_wait_reason(status="running", tool_events=events, has_output=False) == "model_pre_output"


# ── 멱등 ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_same_idempotency_key_resent_yields_one_record(env):
    first = await _send(env, "배포 순서 바꿔줘", key="idem-1")
    again = await _send(env, "문구가 달라져도 같은 키", key="idem-1")

    assert again["duplicate"] is True
    assert again["message_id"] == first["message_id"]
    assert again["state"] == first["state"]
    assert len(env.messages) == 1 and len(env.states) == 1
    assert env.message_count == 1
    assert len(_queued(env)) == 1


@pytest.mark.asyncio
async def test_replay_after_apply_reports_current_state_not_queued(env):
    first = await _send(env, "테스트도 돌려줘", key="idem-2")
    await ilc.apply_interrupts(str(env.session_id), _queued(env))

    replay = await _send(env, "테스트도 돌려줘", key="idem-2")

    assert replay["duplicate"] is True
    assert replay["state"] == "APPLIED"
    assert replay["message_id"] == first["message_id"]


@pytest.mark.asyncio
async def test_different_idempotency_keys_are_separate_records(env):
    a = await _send(env, "A 해줘", key="k-a")
    b = await _send(env, "B 해줘", key="k-b")
    assert a["message_id"] != b["message_id"]
    assert len(env.states) == 2


# ── 저장 실패 ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_state_write_failure_means_no_receipt_and_no_trace(env):
    env.fail_state_insert = RuntimeError("synthetic state failure")

    with pytest.raises(HTTPException) as exc:
        await _send(env, "저장 실패 케이스")

    assert exc.value.status_code == 503
    assert exc.value.detail["code"] == "interrupt_receipt_failed"
    assert exc.value.detail["queued"] is False
    assert env.messages == {} and env.states == {}
    assert env.message_count == 0
    assert _queued(env) == []


@pytest.mark.asyncio
async def test_missing_state_table_keeps_receipt_durable_via_message_row(env):
    env.state_table_exists = False

    res = await _send(env, "테이블 없는 전환 구간")

    assert res["queued"] is True
    assert res["state"] == "QUEUED"
    assert len(env.messages) == 1 and env.states == {}
    snap = await _snapshot(env)
    assert [i["state"] for i in snap] == ["QUEUED"]
    assert snap[0]["id"] == res["message_id"]


# ── 반영(APPLIED)·진행(WORKING)·완료(DONE) ─────────────────────────────────


@pytest.mark.asyncio
async def test_apply_marks_applied_in_arrival_order_and_emits_public_reply(env):
    receipts = [await _send(env, f"지시 {n}") for n in (1, 2, 3)]

    events = await ilc.apply_interrupts(str(env.session_id), _queued(env))

    assert [e["message_id"] for e in events] == [r["message_id"] for r in receipts]
    for ev in events:
        assert ev["type"] == "interrupt_applied"
        assert ev["state"] == "APPLIED"
        assert ev["applied_at"]
        assert ev["public_reply"].startswith("추가 지시 반영: 기존 작업 계속")
    stamps = [env.states[uuid.UUID(r["message_id"])]["applied_at"] for r in receipts]
    assert stamps == sorted(stamps) and len(set(stamps)) == 3
    for r in receipts:
        row = env.states[uuid.UUID(r["message_id"])]
        assert row["queued_at"] < row["applied_at"]
        assert row["public_reply"]
        assert row["wait_reason"] == "none"


@pytest.mark.asyncio
async def test_apply_is_monotonic_and_idempotent(env):
    res = await _send(env, "한 번만 반영")
    first = await ilc.apply_interrupts(str(env.session_id), _queued(env))
    applied_at = env.states[uuid.UUID(res["message_id"])]["applied_at"]

    await ilc.promote_working(str(env.session_id))
    again = await ilc.apply_interrupts(str(env.session_id), _queued(env))

    row = env.states[uuid.UUID(res["message_id"])]
    assert row["state"] == "WORKING"
    assert row["applied_at"] == applied_at
    assert first[0]["applied_at"] == again[0]["applied_at"] == ilc.to_kst_iso(applied_at)
    assert again[0]["state"] == "WORKING"


@pytest.mark.asyncio
async def test_full_lifecycle_received_to_done(env):
    res = await _send(env, "전체 흐름")
    mid = uuid.UUID(res["message_id"])
    seen = [env.states[mid]["state"]]

    await ilc.apply_interrupts(str(env.session_id), _queued(env))
    seen.append(env.states[mid]["state"])
    await ilc.promote_working(str(env.session_id))
    seen.append(env.states[mid]["state"])
    await ilc.mark_done(_FakeConn(env), env.session_id)
    seen.append(env.states[mid]["state"])

    assert seen == ["QUEUED", "APPLIED", "WORKING", "DONE"]
    row = env.states[mid]
    assert row["queued_at"] <= row["applied_at"] <= row["working_at"] <= row["done_at"]
    snap = await _snapshot(env)
    assert snap[0]["state"] == "DONE" and snap[0]["done_at"]


@pytest.mark.asyncio
async def test_event_without_message_id_still_emitted_when_state_write_unavailable(env):
    with patch("app.core.db_pool.get_pool", side_effect=RuntimeError("pool down")):
        events = await ilc.apply_interrupts(
            str(env.session_id), [{"content": "옛 큐 항목", "attachments": []}]
        )
    assert events == [{"type": "interrupt_applied", "content": "옛 큐 항목"}]


# ── 연결 끊김·슬롯 교체 후 복원 ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_snapshot_survives_dropped_connection_and_process_queue_loss(env):
    res = await _send(env, "연결이 끊겨도 남아야 함")
    iq._interrupt_queues.clear()

    snap = await _snapshot(env)

    assert [(i["id"], i["state"]) for i in snap] == [(res["message_id"], "QUEUED")]


@pytest.mark.asyncio
async def test_apply_failure_leaves_state_queued_then_recovers_on_next_apply(env):
    res = await _send(env, "반영 중 DB 단절")
    pending = _queued(env)
    with patch("app.core.db_pool.get_pool", side_effect=RuntimeError("connection dropped")):
        events = await ilc.apply_interrupts(str(env.session_id), pending)

    assert events[0]["type"] == "interrupt_applied" and "state" not in events[0]
    assert (await _snapshot(env))[0]["state"] == "QUEUED"

    events = await ilc.apply_interrupts(str(env.session_id), pending)

    assert events[0]["state"] == "APPLIED"
    assert (await _snapshot(env))[0]["state"] == "APPLIED"
    assert events[0]["message_id"] == res["message_id"]


@pytest.mark.asyncio
async def test_owner_slot_change_keeps_same_snapshot(env):
    with patch.object(chat_router.svc, "_EXECUTION_OWNER_INSTANCE", "slot-a"):
        res = await _send(env, "슬롯 A 에서 접수")
    before = await _snapshot(env)

    with patch.object(chat_router.svc, "_EXECUTION_OWNER_INSTANCE", "slot-b"):
        after = await _snapshot(env)
        events = await ilc.apply_interrupts(str(env.session_id), [{"content": "x", "message_id": res["message_id"]}])

    assert before == after
    assert before[0]["owner_instance"] == "slot-a"
    assert events[0]["state"] == "APPLIED"
    assert (await _snapshot(env))[0]["owner_instance"] == "slot-a"


# ── 취소·만료는 일반 추가 지시와 분리 ──────────────────────────────────────


@pytest.mark.asyncio
async def test_cancel_marks_only_unapplied_and_applied_stays(env):
    applied = await _send(env, "이미 반영된 지시")
    await ilc.apply_interrupts(str(env.session_id), _queued(env))
    env.messages[uuid.UUID(applied["message_id"])]["intent"] = "interrupt_applied"
    waiting = await _send(env, "아직 대기 중인 지시")

    ctx = {"tenant": {"id": str(uuid.uuid4())}}
    out = await chat_router.cancel_queued_interrupts(env.session_id, None, ctx)

    assert out["cancelled"] == 1 and out["message_ids"] == [waiting["message_id"]]
    by_id = {i["id"]: i["state"] for i in await _snapshot(env)}
    assert by_id[waiting["message_id"]] == "CANCELLED"
    assert by_id[applied["message_id"]] == "APPLIED"
    assert env.messages[uuid.UUID(waiting["message_id"])]["intent"] == "interrupt_cancelled"


@pytest.mark.asyncio
async def test_cancelled_instruction_is_not_applied_later(env):
    res = await _send(env, "취소할 지시")
    pending = _queued(env)
    ctx = {"tenant": {"id": str(uuid.uuid4())}}
    await chat_router.cancel_queued_interrupts(env.session_id, None, ctx)

    await ilc.apply_interrupts(str(env.session_id), pending)

    assert env.states[uuid.UUID(res["message_id"])]["state"] == "CANCELLED"


@pytest.mark.asyncio
async def test_goal_change_wording_is_a_normal_instruction_not_a_cancel(env):
    res = await _send(env, "목표를 바꿔서 B 부터 해줘")

    assert res["state"] == "QUEUED"
    assert env.messages[uuid.UUID(res["message_id"])]["intent"] == "queued_interrupt"
    assert env.states[uuid.UUID(res["message_id"])]["state"] != "CANCELLED"


@pytest.mark.asyncio
async def test_expire_only_touches_unapplied(env):
    old = await _send(env, "만료될 지시")
    env.messages[uuid.UUID(old["message_id"])]["intent"] = "interrupt_expired"
    kept = await _send(env, "다른 지시")
    await ilc.apply_interrupts(str(env.session_id), [q for q in _queued(env) if q["message_id"] == kept["message_id"]])

    n = await ilc.mark_expired(_FakeConn(env), env.session_id)

    by_id = {i["id"]: i["state"] for i in await _snapshot(env)}
    assert n == 1
    assert by_id[old["message_id"]] == "EXPIRED"
    assert by_id[kept["message_id"]] == "APPLIED"


# ── 스냅샷·heartbeat ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_status_endpoint_and_streaming_snapshot_share_fields(env):
    res = await _send(env, "필드 점검")
    ctx = {"tenant": {"id": str(uuid.uuid4())}}

    out = await chat_router.get_interrupt_status(env.session_id, ctx)

    assert out["session_id"] == str(env.session_id)
    item = out["interrupts"][0]
    assert set(item) == {
        "id", "state", "wait_reason", "summary", "public_reply", "received_at", "queued_at",
        "applied_at", "working_at", "done_at", "execution_id", "generation_id", "owner_instance",
    }
    assert item["id"] == res["message_id"]
    assert item["received_at"].endswith("+09:00")


@pytest.mark.asyncio
async def test_heartbeat_silent_without_interrupts_then_unchanged_then_final_done(env):
    sid = str(env.session_id)
    event, digest = await ilc.poll_status_event(sid, "")
    assert event is None and digest == ""

    await _send(env, "heartbeat 대상")
    first, d1 = await ilc.poll_status_event(sid, "")
    assert first["unchanged"] is False and len(first["interrupts"]) == 1

    same, d2 = await ilc.poll_status_event(sid, d1)
    assert same == {"type": "interrupt_status", "unchanged": True, "digest": d1}
    assert d2 == d1

    await ilc.apply_interrupts(sid, _queued(env))
    changed, d3 = await ilc.poll_status_event(sid, d2)
    assert changed["unchanged"] is False and d3 != d2
    assert changed["interrupts"][0]["state"] == "APPLIED"

    await ilc.mark_done(_FakeConn(env), env.session_id)
    final, d4 = await ilc.poll_status_event(sid, d3)
    assert final["unchanged"] is False and final["interrupts"][0]["state"] == "DONE"
    assert d4 == ""
    quiet, _ = await ilc.poll_status_event(sid, d4)
    assert quiet is None


@pytest.mark.asyncio
async def test_heartbeat_refreshes_wait_reason_of_queued_only(env):
    sid = str(env.session_id)
    queued = await _send(env, "대기 중")
    event, _ = await ilc.poll_status_event(sid, "", wait_reason="tool_running")

    assert event["interrupts"][0]["wait_reason"] == "tool_running"
    assert env.states[uuid.UUID(queued["message_id"])]["wait_reason"] == "tool_running"

    await ilc.apply_interrupts(sid, _queued(env))
    await ilc.poll_status_event(sid, "", wait_reason="relay_wait")
    assert env.states[uuid.UUID(queued["message_id"])]["wait_reason"] == "none"


# ── 형식 계약 ───────────────────────────────────────────────────────────────


def test_applied_sse_carries_message_id_state_applied_at():
    import json

    item = {"id": "m1", "state": "APPLIED", "applied_at": "2026-10-03T12:00:00+09:00",
            "public_reply": "추가 지시 반영: ...", "summary": "s"}
    raw = ilc.applied_sse(ilc.applied_event(item, "본문"))

    assert raw.startswith("event: interrupt_applied\ndata: ")
    payload = json.loads(raw.split("data: ", 1)[1])
    assert payload["type"] == "interrupt_applied"
    assert (payload["message_id"], payload["state"], payload["applied_at"]) == ("m1", "APPLIED", item["applied_at"])


def test_public_reply_and_prompt_instruction_use_the_agreed_format():
    reply = ilc.build_public_reply("제목 변경", "tool_running")
    assert reply.startswith("추가 지시 반영: 기존 ")
    assert "계속" in reply and "지금 " in reply and "다음 " in reply
    assert "추가 지시 반영: 기존 A 계속, B 우선/추가. 지금 C 확인 중, 다음 D" in ilc.INTERRUPT_REPLY_INSTRUCTION


def test_summary_is_capped_normalized_and_masked():
    long = "가" * 300
    assert len(ilc.build_summary(long)) == ilc.SUMMARY_LIMIT
    assert ilc.build_summary("a\n\n  b\tc") == "a b c"
    assert "sk-ant-oat01-" not in ilc.build_summary("토큰 sk-ant-oat01-" + "x" * 40 + " 사용")


def test_sse_event_type_maps_to_protocol_v2():
    from app.services.chat_protocol import _LEGACY_EVENT_TYPES

    assert _LEGACY_EVENT_TYPES["interrupt_status"] == "command.status"


# ── 재작업 라운드 2: 멱등 경쟁·재전송 상태·savepoint·원자성·범위 ───────────


@pytest.mark.asyncio
@pytest.mark.parametrize("table_exists", [True, False])
async def test_resend_without_state_row_keeps_one_message_and_derives_state(env, table_exists):
    first = await _send(env, "상태 행 없는 옛 접수")
    env.states.pop(uuid.UUID(first["message_id"]), None)
    env.state_table_exists = table_exists

    again = await _send(env, "상태 행 없는 옛 접수")

    assert again["duplicate"] is True and again["message_id"] == first["message_id"]
    assert again["state"] == "QUEUED"
    assert len(env.messages) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "intent, expected",
    [("queued_interrupt", "QUEUED"), ("interrupt_applied", "APPLIED"), ("interrupt_completed", "DONE"),
     ("interrupt_cancelled", "CANCELLED"), ("interrupt_expired", "EXPIRED")],
)
async def test_get_receipt_derives_state_from_intent_when_state_row_missing(env, intent, expected):
    first = await _send(env, "의도로 유도")
    mid = uuid.UUID(first["message_id"])
    env.messages[mid]["intent"] = intent
    env.states.pop(mid)

    item = await ilc.get_receipt(_FakeConn(env), mid)

    assert item["id"] == first["message_id"] and item["state"] == expected
    assert await ilc.get_receipt(_FakeConn(env), uuid.uuid4()) is None


@pytest.mark.asyncio
async def test_resend_of_done_instruction_reports_done_not_queued(env):
    first = await _send(env, "끝난 지시", key="idem-done")
    await ilc.apply_interrupts(str(env.session_id), _queued(env))
    await ilc.mark_done(_FakeConn(env), env.session_id)

    again = await _send(env, "끝난 지시", key="idem-done")

    assert again["duplicate"] is True and again["state"] == "DONE"
    assert again["message_id"] == first["message_id"]


@pytest.mark.asyncio
async def test_concurrent_same_key_loser_gets_winners_receipt_not_503(env):
    """락을 기다리는 사이 승자가 커밋한 상황: 패자는 새 행 없이 승자의 접수를 돌려받는다."""
    winner_id = uuid.uuid4()

    def _winner_commits(db: _FakeDB):
        now = _now()
        db.messages[winner_id] = {
            "session_id": db.session_id, "content": "[추가 지시] 동시 전송", "intent": "queued_interrupt",
            "created_at": now, "edited_at": None,
        }
        db.message_count += 1
        db.states[winner_id] = {
            "message_id": winner_id, "session_id": db.session_id, "state": "QUEUED",
            "wait_reason": "model_pre_output", "summary": "동시 전송", "public_reply": None,
            "idempotency_key": "idem-race", "execution_id": uuid.UUID(EXECUTION_ID),
            "generation_id": None, "owner_instance": "slot-a", "applied_execution_id": None,
            "received_at": now, "queued_at": now, "applied_at": None, "working_at": None, "done_at": None,
        }
        db.on_lock = None

    env.on_lock = _winner_commits

    res = await _send(env, "동시 전송", key="idem-race")

    assert res["duplicate"] is True
    assert res["message_id"] == str(winner_id)
    assert res["state"] == "QUEUED"
    assert len(env.messages) == 1 and len(env.states) == 1
    assert env.message_count == 1
    assert _queued(env) == []


@pytest.mark.asyncio
async def test_keyed_receipt_takes_idempotency_lock_before_insert(env):
    await _send(env, "락 순서", key="idem-lock")

    order = [t for t in env.queried_tags if t in ("lock_idem", "find_idem", "insert")]
    assert order == ["find_idem", "lock_idem", "find_idem", "insert"]


@pytest.mark.asyncio
async def test_unkeyed_receipt_takes_no_lock(env):
    await _send(env, "키 없는 접수")
    assert "lock_idem" not in env.queried_tags


@pytest.mark.asyncio
async def test_state_writes_never_poison_callers_transaction_when_table_missing(env):
    env.state_table_exists = False
    conn = _FakeConn(env)
    sid = env.session_id
    mid = uuid.uuid4()

    async with conn.transaction():
        assert await ilc.find_by_idempotency_key(conn, sid, "k") is None
        assert await ilc.get_item(conn, mid) is None
        assert await ilc.mark_applied(conn, sid, [mid], execution_id=EXECUTION_ID) == []
        assert await ilc.mark_working(conn, sid) == 0
        assert await ilc.mark_done(conn, sid) == 0
        assert await ilc.mark_cancelled(conn, sid, [mid]) == 0
        assert await ilc.mark_expired(conn, sid) == 0
        assert await ilc.refresh_wait_reason(conn, sid, "relay_wait") == 0
        assert await ilc.fetch_snapshot(conn, sid) == []
        # 같은 트랜잭션의 뒤 문장이 InFailedSQLTransaction 으로 막히지 않아야 한다.
        assert await conn.fetchval("SELECT COUNT(*) FROM chat_messages") == 0


@pytest.mark.asyncio
async def test_non_table_errors_roll_back_savepoint_and_propagate(env):
    await _send(env, "롤백 확인")
    env.fail_snapshot = RuntimeError("boom")
    conn = _FakeConn(env)

    async with conn.transaction():
        with pytest.raises(RuntimeError):
            await ilc.fetch_snapshot(conn, env.session_id)
        assert await conn.fetchval("SELECT COUNT(*) FROM chat_messages") == 0


@pytest.mark.asyncio
async def test_streaming_status_snapshot_failure_does_not_abort_callers_connection(env):
    env.fail_snapshot = RuntimeError("snapshot exploded")
    conn = _FakeConn(env)

    with patch.object(chat_router, "_get_streaming_status_revisions", AsyncMock(return_value={})):
        async with conn.transaction():
            payload = await chat_router._finalize_streaming_status(
                env.session_id, {"is_streaming": True}, conn
            )
            assert payload["interrupts"] == []
            assert await conn.fetchval("SELECT COUNT(*) FROM chat_messages") == 0


@pytest.mark.asyncio
async def test_streaming_status_carries_interrupts_snapshot(env):
    res = await _send(env, "스냅샷 포함")
    with patch.object(chat_router, "_get_streaming_status_revisions", AsyncMock(return_value={})):
        payload = await chat_router._finalize_streaming_status(env.session_id, {"is_streaming": True})

    assert [(i["id"], i["state"]) for i in payload["interrupts"]] == [(res["message_id"], "QUEUED")]


@pytest.mark.asyncio
async def test_apply_is_atomic_state_and_public_reply_move_together(env):
    res = await _send(env, "원자적 반영")
    mid = uuid.UUID(res["message_id"])
    env.fail_apply = RuntimeError("write failed")

    events = await ilc.apply_interrupts(str(env.session_id), _queued(env))

    row = env.states[mid]
    assert row["state"] == "QUEUED" and row["public_reply"] is None and row["applied_at"] is None
    assert row["wait_reason"] == "model_pre_output"
    assert "state" not in events[0]

    env.fail_apply = None
    events = await ilc.apply_interrupts(str(env.session_id), _queued(env))
    assert env.states[mid]["state"] == "APPLIED" and env.states[mid]["public_reply"]
    assert events[0]["state"] == "APPLIED"


@pytest.mark.asyncio
async def test_one_failing_apply_does_not_block_the_others(env):
    a = await _send(env, "A")
    b = await _send(env, "B")
    original = ilc._apply_one

    async def _flaky(conn, session_id, mid, exe, gen):
        if str(mid) == a["message_id"]:
            raise RuntimeError("only A fails")
        return await original(conn, session_id, mid, exe, gen)

    with patch.object(ilc, "_apply_one", _flaky):
        events = await ilc.apply_interrupts(str(env.session_id), _queued(env))

    assert "state" not in events[0] and events[1]["state"] == "APPLIED"
    assert env.states[uuid.UUID(a["message_id"])]["state"] == "QUEUED"
    assert env.states[uuid.UUID(b["message_id"])]["state"] == "APPLIED"


@pytest.mark.asyncio
async def test_working_and_done_only_touch_the_applying_execution(env):
    exec_x, exec_y = EXECUTION_ID, "33333333-3333-4333-8333-333333333333"
    a = await _send(env, "실행 X 가 반영")
    env.current_execution = exec_x
    await ilc.apply_interrupts(str(env.session_id), _queued(env))
    iq._interrupt_queues.clear()
    b = await _send(env, "실행 Y 가 반영")
    env.current_execution = exec_y
    await ilc.apply_interrupts(str(env.session_id), _queued(env))
    ida, idb = uuid.UUID(a["message_id"]), uuid.UUID(b["message_id"])

    await ilc.promote_working(str(env.session_id), exec_x)
    assert (env.states[ida]["state"], env.states[idb]["state"]) == ("WORKING", "APPLIED")

    await ilc.mark_done(_FakeConn(env), env.session_id, exec_x)
    assert (env.states[ida]["state"], env.states[idb]["state"]) == ("DONE", "APPLIED")

    await ilc.mark_done(_FakeConn(env), env.session_id, exec_y)
    assert env.states[idb]["state"] == "DONE"


@pytest.mark.asyncio
async def test_unscoped_done_still_covers_whole_session(env):
    a = await _send(env, "범위 없음")
    await ilc.apply_interrupts(str(env.session_id), _queued(env))

    await ilc.mark_done(_FakeConn(env), env.session_id)

    assert env.states[uuid.UUID(a["message_id"])]["state"] == "DONE"


# ── heartbeat 부하 ──────────────────────────────────────────────────────────


def test_should_poll_status_skips_idle_ticks_but_probes_periodically():
    idle = [ilc.should_poll_status(queue_has_items=False, last_digest="", tick=t) for t in range(1, 11)]
    assert idle == [False, False, False, False, True, False, False, False, False, True]
    assert ilc.should_poll_status(queue_has_items=True, last_digest="", tick=1) is True
    assert ilc.should_poll_status(queue_has_items=False, last_digest="abc", tick=1) is True


@pytest.mark.asyncio
async def test_idle_poll_reads_states_table_once_and_never_writes_or_scans_messages(env):
    event, digest = await ilc.poll_status_event(str(env.session_id), "", wait_reason="tool_running")

    assert event is None and digest == ""
    assert env.queried_tags == ["snapshot"]


@pytest.mark.asyncio
async def test_poll_after_all_done_sends_terminal_items_once_then_goes_quiet(env):
    sid = str(env.session_id)
    res = await _send(env, "끝까지")
    _, d1 = await ilc.poll_status_event(sid, "")
    await ilc.apply_interrupts(sid, _queued(env))
    await ilc.mark_done(_FakeConn(env), env.session_id)

    final, d2 = await ilc.poll_status_event(sid, d1)

    assert d2 == "" and final["unchanged"] is False
    assert [(i["id"], i["state"]) for i in final["interrupts"]] == [(res["message_id"], "DONE")]
    assert final["interrupts"][0]["done_at"]
    assert (await ilc.poll_status_event(sid, d2))[0] is None


def test_status_pump_is_awaited_on_exit_and_gated():
    import inspect

    from app.services import chat_service

    src = inspect.getsource(chat_service.with_background_completion)
    assert "gather(_intr_status_task, return_exceptions=True)" in src
    assert "should_poll_status(" in src
