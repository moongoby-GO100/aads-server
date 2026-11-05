"""재작업 소진 주도 판단 요청 — 기록(소진 분기)과 발송(스윕)의 분리를 고정한다.

DB 없이 SQL 표지로 분기하는 메모리 가짜 DB 를 쓴다. 상태 필터·LIMIT·UNIQUE·`AND status=` 조건은
가짜가 실제로 흉내 내므로 "오래된 행이 LIMIT 을 막는다" 같은 회귀가 테스트에서도 드러난다.
"""
from __future__ import annotations

import asyncio
import itertools
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import asyncpg
import pytest

from app.services import goal_failure_retry as gfr
from app.services import goal_retry_exhaust_triage as triage
from tests.unit.test_goal_failure_retry import GOAL_ID, MILESTONE_ID, FakeConn

TENANT = GOAL_ID
LEAD_SESSION = "109dfc17-8c86-4584-9de9-b022eff40b23"
_ids = itertools.count(1)


def _uuid(n: int) -> str:
    return f"00000000-0000-0000-0000-{n:012d}"


class FakeDB:
    """requests / milestones / goals / sessions / links / alerts / queue 를 메모리에 둔다."""

    def __init__(self):
        self.table = True
        self.requests: list[dict] = []
        self.milestones = {MILESTONE_ID: {"title": "마일스톤A", "status": "blocked"}}
        self.goal = {"title": "목표A", "project": "AADS", "owner_session_id": LEAD_SESSION}
        self.sessions = {LEAD_SESSION}
        self.links: list[datetime] = []
        self.alerts: list[str] = []
        self.queue: dict[str, dict] = {}
        self.fail_sent_update = 0
        self.fail_ceo_sent_update = 0
        self.enqueue_calls: list[tuple] = []
        self.tick = itertools.count(0)

    def add(self, kind="lead", status="pending", milestone_id=MILESTONE_ID, lineage="job-1", **extra):
        row = {
            "id": _uuid(next(_ids)), "tenant_id": TENANT, "goal_id": GOAL_ID,
            "milestone_id": milestone_id, "lineage": lineage, "kind": kind, "status": status,
            "milestone_status_at_request": "blocked", "executions": 2, "attempts": 0,
            "last_error": None, "delivery_ref": None, "first_attempt_at": None, "sent_at": None, "resolution": None,
            "target_session_id": None,
            "created_at": datetime(2026, 11, 6, tzinfo=UTC) + timedelta(seconds=next(self.tick)),
        }
        row.update(extra)
        self.requests.append(row)
        return row

    def of(self, kind, status=None):
        return [r for r in self.requests if r["kind"] == kind and (status is None or r["status"] == status)]

    def find(self, rid):
        return next((r for r in self.requests if r["id"] == rid), None)

    def pool(self):
        return _Pool(self)


class _Pool:
    def __init__(self, db):
        self.db = db

    def acquire(self):
        conn = _Conn(self.db)

        class Ctx:
            async def __aenter__(self_inner):
                return conn

            async def __aexit__(self_inner, *_):
                return False

        return Ctx()


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False


class _Conn:
    def __init__(self, db: FakeDB):
        self.db = db

    def transaction(self):
        return _Tx()

    def _insert_unique(self, row):
        key = (row["tenant_id"], row["milestone_id"], row["lineage"], row["kind"])
        if any((r["tenant_id"], r["milestone_id"], r["lineage"], r["kind"]) == key for r in self.db.requests):
            return None
        return self.db.add(**{k: row[k] for k in ("kind", "milestone_id", "lineage")},
                           executions=row["executions"],
                           milestone_status_at_request=row["milestone_status_at_request"])

    async def fetchval(self, q, *a):
        db = self.db
        if "to_regclass" in q:
            return "goal_retry_exhaust_requests" if db.table else None
        if "FROM chat_sessions" in q:
            return a[0] if a[0] in db.sessions else None
        if "FROM alert_history" in q:
            return 1 if a[0] in db.alerts else None
        if "FROM goal_task_links" in q:
            return 1 if any(t > a[2] for t in db.links) else None
        if q.lstrip().startswith("UPDATE"):
            return self._update(q, a, returning=True)
        raise AssertionError(q)

    async def fetchrow(self, q, *a):
        db = self.db
        assert "FOR UPDATE OF r" in q, q
        r = db.find(a[0])
        if not r or r["status"] != "sent":
            return None
        m = db.milestones.get(r["milestone_id"])
        return {"id": r["id"], "tenant_id": r["tenant_id"], "milestone_id": r["milestone_id"],
                "baseline_at": r["first_attempt_at"] or r["sent_at"],
                "status_at_request": r["milestone_status_at_request"],
                "current_status": m["status"] if m else None}

    async def fetch(self, q, *a):
        db = self.db
        if "r.kind = $1" in q:
            rows = sorted((r for r in db.requests if r["kind"] == a[0] and r["status"] == "pending"),
                          key=lambda r: r["created_at"])[:20]
            out = []
            for r in rows:
                m = db.milestones.get(r["milestone_id"])
                out.append({
                    "id": r["id"], "tenant_id": r["tenant_id"], "goal_id": r["goal_id"],
                    "milestone_id": r["milestone_id"], "lineage": r["lineage"],
                    "executions": r["executions"], "attempts": r["attempts"],
                    "goal_title": db.goal["title"], "project": db.goal["project"],
                    "owner_session_id": db.goal["owner_session_id"],
                    "target_session_id": r["target_session_id"], "delivery_ref": r["delivery_ref"],
                    "milestone_title": m["title"] if m else None,
                    "milestone_status": m["status"] if m else None,
                })
            return out
        if "kind = 'lead' AND status = 'sent'" in q:
            cutoff = datetime.now(UTC) - timedelta(minutes=a[0])
            assert "COALESCE(first_attempt_at, sent_at)" in q

            def base(r):
                return r["first_attempt_at"] or r["sent_at"]
            rows = sorted((r for r in db.requests if r["kind"] == "lead" and r["status"] == "sent"
                           and base(r) < cutoff), key=base)[:20]
            return [{"id": r["id"]} for r in rows]
        raise AssertionError(q)

    async def execute(self, q, *a):
        if "INSERT INTO goal_retry_exhaust_requests" in q:
            src = self.db.find(a[0])
            self._insert_unique({**src, "kind": "ceo"})
            return "INSERT 0 1"
        self._update(q, a, returning=False)
        return "UPDATE 1"

    def _update(self, q, a, *, returning):
        db = self.db
        if "INSERT INTO goal_retry_exhaust_requests" in q:
            raise AssertionError(q)
        r = db.find(a[0])
        flat = re.sub(r"\s+", " ", q)
        if "attempts = attempts + 1" in flat:
            if not r or r["status"] != "pending":
                return None
            r["attempts"] += 1
            r["last_error"] = a[1]
            if r["attempts"] >= a[2]:
                r["status"], r["resolution"] = "undeliverable", "send_failed"
            return r["status"]
        if "target_session_id IS NULL" in flat:
            if r and r["status"] == "pending" and r["target_session_id"] is None:
                r["target_session_id"] = a[1]
                r["first_attempt_at"] = r["first_attempt_at"] or datetime.now(UTC)
                return r["target_session_id"]
            return None
        if "delivery_ref IS NULL" in flat:
            if r and r["status"] == "pending" and r["delivery_ref"] is None:
                r["delivery_ref"] = a[1]
                return r["delivery_ref"]
            return None
        if "status = 'sent'" in flat and "delivery_ref" in flat:
            assert "sent_at = COALESCE(sent_at, NOW())" in flat and "target_session_id" not in flat
            attr = "fail_sent_update" if r and r["kind"] == "lead" else "fail_ceo_sent_update"
            if getattr(db, attr):
                setattr(db, attr, getattr(db, attr) - 1)
                raise RuntimeError("connection lost")
            if r and r["status"] == "pending":
                r.update(status="sent", sent_at=r["sent_at"] or datetime.now(UTC), delivery_ref=a[1])
            return None
        if "no_lead_session" in flat:
            if r and r["status"] == "pending":
                r.update(status="undeliverable", resolution="no_lead_session")
                return r["id"]
            return None
        if "status = 'resolved', resolution = $2" in flat:
            if r and r["status"] == "pending":
                r.update(status="resolved", resolution=a[1])
            return None
        if "'lead_acted'" in flat:
            if r and r["status"] == "sent":
                r.update(status="resolved", resolution="lead_acted")
            return None
        if "'escalated'" in flat:
            if r and r["status"] == "sent":
                r.update(status="escalated", resolution="lead_no_response")
                return r["id"]
            return None
        raise AssertionError(q)


@pytest.fixture
def db(monkeypatch):
    fake = FakeDB()
    monkeypatch.setattr(triage, "get_pool", fake.pool)
    fake.sent_messages = []

    async def enqueue(session_id, message, dedupe_key):
        key = (session_id, dedupe_key)
        row = next(r for r in fake.requests if r["id"] in dedupe_key)
        # 발송 전에 수신자와 첫 시도 시각이 이미 행에 고정돼 있어야 한다.
        assert row["target_session_id"] == session_id and row["first_attempt_at"]
        fake.enqueue_calls.append(key)
        if key in fake.queue:
            return {"queue_id": fake.queue[key]["id"], "queue_status": "pending", "created": False}
        fake.queue[key] = {"id": f"q{len(fake.queue) + 1}", "message": message}
        return {"queue_id": fake.queue[key]["id"], "queue_status": "pending", "created": True}

    async def notify(title, message, project):
        fake.alerts.append(title)
        fake.notified = getattr(fake, "notified", []) + [title]
        return True

    fake.enqueue_impl, fake.notify_impl = enqueue, notify
    monkeypatch.setattr(triage, "_enqueue", lambda *a: fake.enqueue_impl(*a))
    monkeypatch.setattr(triage, "_notify", lambda *a: fake.notify_impl(*a))
    return fake


def _sweep():
    return asyncio.run(triage.sweep_exhaust_requests())


def _age_sent(db, minutes=120):
    for r in db.of("lead", "sent"):
        r["sent_at"] -= timedelta(minutes=minutes)
        r["first_attempt_at"] -= timedelta(minutes=minutes)
    db.links[:] = [t - timedelta(minutes=minutes) for t in db.links]


# ── 1·2: 기록 ─────────────────────────────────────────────────────────────────

class _RecordConn(FakeConn):
    """asyncpg 트랜잭션 의미를 흉내 낸다: 중첩 transaction() 은 savepoint, 쿼리 오류는 그 트랜잭션을
    aborted 로 만들고(다음 쿼리는 InFailedSQLTransactionError), 최상위가 aborted 인 채 끝나면 통째로 롤백된다."""

    _TRACKED = ("dispatch_note", "evidence", "milestone_status", "goal_status")

    def __init__(self, table=True, fail_insert=False):
        super().__init__()
        self.executions = {"job-x": 2}
        self.table, self.fail_insert, self.rows = table, fail_insert, {}
        self.depth, self.aborted = 0, False

    def _snapshot(self):
        return {**{k: getattr(self, k) for k in self._TRACKED}, "rows": dict(self.rows)}

    def _restore(self, snap):
        for k in self._TRACKED:
            setattr(self, k, snap[k])
        self.rows = dict(snap["rows"])

    def transaction(self):
        conn, lock_tx = self, super().transaction()

        class Tx:
            async def __aenter__(tx):
                await lock_tx.__aenter__()
                conn.depth += 1
                tx.level, tx.snap, tx.aborted_before = conn.depth, conn._snapshot(), conn.aborted
                return tx

            async def __aexit__(tx, exc_type, *_):
                conn.depth -= 1
                try:
                    if exc_type is not None or (conn.aborted and tx.level == 1):
                        conn._restore(tx.snap)  # ROLLBACK / ROLLBACK TO SAVEPOINT
                        conn.aborted = tx.aborted_before
                    elif conn.aborted and not tx.aborted_before:
                        raise asyncpg.exceptions.InFailedSQLTransactionError("release savepoint")
                finally:
                    await lock_tx.__aexit__(exc_type, *_)
                return False

        return Tx()

    def _check_alive(self):
        if self.aborted:
            raise asyncpg.exceptions.InFailedSQLTransactionError(
                "current transaction is aborted, commands ignored until end of transaction block")

    async def execute(self, q, *a):
        self._check_alive()
        return await super().execute(q, *a)

    async def fetchrow(self, q, *a):
        self._check_alive()
        return await super().fetchrow(q, *a)

    async def fetch(self, q, *a):
        self._check_alive()
        return await super().fetch(q, *a)

    async def fetchval(self, q, *a):
        self._check_alive()
        if "to_regclass" in q:
            return "goal_retry_exhaust_requests" if self.table else None
        if "INSERT INTO goal_retry_exhaust_requests" in q:
            if self.fail_insert:
                self.aborted = True
                raise RuntimeError("insert boom")
            key = (a[0], a[2], a[3])
            if key in self.rows:
                return None
            self.rows[key] = a
            return "new-id"
        return await super().fetchval(q, *a)


def _ensure(conn):
    return asyncio.run(gfr.ensure_retry_candidate(
        conn, milestone_id=MILESTONE_ID, goal_id=GOAL_ID, failed_task_id="job-x", reason="linked_task_failed"))


def _forbid_delivery(monkeypatch):
    async def boom(*_a, **_k):
        raise AssertionError("ensure_retry_candidate 안에서 발송하면 안 된다")
    monkeypatch.setattr(triage, "_enqueue", boom)
    monkeypatch.setattr(triage, "_notify", boom)


def test_exhaustion_records_exactly_one_lead_row_and_never_delivers(monkeypatch):
    _forbid_delivery(monkeypatch)
    conn = _RecordConn()
    first = _ensure(conn)
    assert first["why"] == "retry_exhausted" and first["triage_request"] == "recorded"
    second = _ensure(conn)
    assert second["why"] == "retry_exhausted" and second["triage_request"] == "exists"
    assert len(conn.rows) == 1


def test_completed_milestone_is_not_recorded():
    conn = _RecordConn()
    conn.milestone_status = "completed"
    assert _ensure(conn)["triage_request"] == "skipped_completed"
    assert conn.rows == {}


@pytest.mark.parametrize("kwargs,expected", [
    ({"table": False}, "skipped_no_table"),
    ({"fail_insert": True}, "error"),
])
def test_missing_table_or_insert_error_keeps_legacy_exhaustion_result(kwargs, expected, monkeypatch):
    _forbid_delivery(monkeypatch)
    conn = _RecordConn(**kwargs)
    result = _ensure(conn)
    assert result["why"] == "retry_exhausted" and result["triage_request"] == expected
    assert result["executions"] == 2 and result["lineage"] == "job-x"
    # INSERT 오류가 바깥 트랜잭션을 aborted 로 만들어 소진 메모까지 롤백하면 안 된다.
    assert conn.dispatch_note and "소진" in conn.dispatch_note
    assert conn.aborted is False and conn.depth == 0


def test_fake_conn_reproduces_aborted_transaction_without_savepoint():
    conn = _RecordConn(fail_insert=True)

    async def bare():
        async with conn.transaction():
            await conn.execute("UPDATE milestones SET dispatch_note = $2", MILESTONE_ID, "메모")
            with pytest.raises(RuntimeError):
                await conn.fetchval("INSERT INTO goal_retry_exhaust_requests", "t", "g", "m", "l")
            with pytest.raises(asyncpg.exceptions.InFailedSQLTransactionError):
                await conn.fetchval("SELECT 1")
    asyncio.run(bare())
    assert conn.dispatch_note is None  # aborted 로 끝난 최상위 트랜잭션은 통째로 롤백


def test_savepoint_rollback_restores_only_the_inner_work():
    conn = _RecordConn(fail_insert=True)

    async def nested():
        async with conn.transaction():
            await conn.execute("UPDATE milestones SET dispatch_note = $2", MILESTONE_ID, "메모")
            with pytest.raises(RuntimeError):
                async with conn.transaction():
                    await conn.fetchval("INSERT INTO goal_retry_exhaust_requests", "t", "g", "m", "l")
            assert await conn.fetchval("SELECT 1") is None  # 복구됨
    asyncio.run(nested())
    assert conn.dispatch_note == "메모"


# ── 3·4·5·6: 주도 발송 ────────────────────────────────────────────────────────

def test_pending_lead_is_enqueued_once_and_marked_sent(db):
    row = db.add()
    stats = _sweep()
    assert stats["sent"] == 1
    assert row["status"] == "sent" and row["sent_at"] and row["delivery_ref"] == "q1"
    assert row["target_session_id"] == LEAD_SESSION
    assert len(db.queue) == 1
    (_, key), = db.queue.keys()
    assert key == f"goal_retry_exhaust:{row['id']}"
    body = next(iter(db.queue.values()))["message"]
    assert "목표A" in body and "마일스톤A" in body and "2/2" in body and "상태를 바꾸지 않았다" in body
    assert "대표님 상신 요청" in body and "60분" in body
    _sweep()
    assert len(db.queue) == 1


def test_enqueue_error_keeps_pending_then_gives_up_with_one_ceo_row(db, monkeypatch):
    async def boom(*_a):
        raise RuntimeError("queue down")
    db.enqueue_impl = boom
    row = db.add()
    _sweep()
    assert row["status"] == "pending" and row["attempts"] == 1 and "queue down" in row["last_error"]
    assert db.of("ceo") == []
    for _ in range(10):
        _sweep()
    assert row["status"] == "undeliverable" and row["attempts"] == 5
    assert len(db.of("ceo")) == 1


def test_update_failure_after_enqueue_is_retried_with_same_dedupe_key(db):
    row = db.add()
    db.fail_sent_update = 1
    _sweep()
    assert row["status"] == "pending" and len(db.queue) == 1
    _sweep()
    assert row["status"] == "sent" and row["delivery_ref"] == "q1"
    assert len(db.queue) == 1


def test_recipient_is_pinned_before_send_and_survives_owner_change(db):
    row = db.add()
    db.fail_sent_update = 1
    _sweep()
    assert row["status"] == "pending" and row["target_session_id"] == LEAD_SESSION
    assert row["first_attempt_at"] is not None
    other = "209dfc17-8c86-4584-9de9-b022eff40b23"
    db.sessions.add(other)
    db.goal["owner_session_id"] = other
    _sweep()
    assert row["status"] == "sent" and row["target_session_id"] == LEAD_SESSION
    assert len(db.queue) == 1 and len(db.enqueue_calls) == 2
    assert set(db.enqueue_calls) == {(LEAD_SESSION, f"goal_retry_exhaust:{row['id']}")}


def test_pinned_session_that_disappeared_is_undeliverable_with_ceo_row(db):
    row = db.add(target_session_id=LEAD_SESSION, first_attempt_at=datetime.now(UTC))
    db.sessions = set()
    db.goal["owner_session_id"] = "309dfc17-8c86-4584-9de9-b022eff40b23"
    db.sessions.add(db.goal["owner_session_id"])
    _sweep()
    assert row["status"] == "undeliverable" and row["resolution"] == "no_lead_session"
    assert len(db.of("ceo")) == 1 and db.queue == {}


def test_no_lead_session_makes_one_ceo_row(db):
    db.goal["owner_session_id"] = None
    row = db.add()
    _sweep()
    _sweep()
    assert row["status"] == "undeliverable" and row["resolution"] == "no_lead_session"
    assert len(db.of("ceo")) == 1 and db.queue == {}


def test_lead_session_missing_in_tenant_counts_as_no_lead(db):
    db.sessions = set()
    row = db.add()
    _sweep()
    assert row["status"] == "undeliverable" and len(db.of("ceo")) == 1


def test_closed_milestone_is_resolved_without_delivery(db):
    db.milestones[MILESTONE_ID]["status"] = "completed"
    row = db.add()
    _sweep()
    assert row["status"] == "resolved" and row["resolution"] == "milestone_closed"
    assert db.queue == {} and db.of("ceo") == []


# ── 7·8: 무응답 판정 ──────────────────────────────────────────────────────────

def test_new_task_link_after_send_resolves_the_request(db):
    row = db.add()
    _sweep()
    _age_sent(db)
    db.links.append(row["sent_at"] + timedelta(minutes=5))
    _sweep()
    assert row["status"] == "resolved" and row["resolution"] == "lead_acted"
    assert db.of("ceo") == []


def test_link_made_after_first_attempt_counts_even_if_sent_update_failed(db):
    row = db.add()
    db.fail_sent_update = 1
    _sweep()
    assert row["status"] == "pending"
    row["first_attempt_at"] -= timedelta(minutes=10)  # 재시도가 한참 뒤에 일어난 상황
    link_at = row["first_attempt_at"] + timedelta(minutes=5)
    db.links.append(link_at)
    _sweep()
    assert row["status"] == "sent" and link_at < row["sent_at"]
    _age_sent(db)
    _sweep()
    assert row["status"] == "resolved" and row["resolution"] == "lead_acted"
    assert db.of("ceo") == []


def test_status_change_after_send_resolves_the_request(db):
    row = db.add()
    _sweep()
    _age_sent(db)
    db.milestones[MILESTONE_ID]["status"] = "in_progress"
    _sweep()
    assert row["status"] == "resolved"


def test_no_response_escalates_once_even_if_swept_again(db):
    row = db.add()
    _sweep()
    _sweep()  # 대기 시간 전이면 판정하지 않는다
    assert row["status"] == "sent"
    _age_sent(db)
    _sweep()
    _sweep()
    assert row["status"] == "escalated"
    assert len(db.of("ceo")) == 1
    assert db.of("ceo")[0]["status"] == "sent"


def test_old_processed_rows_do_not_starve_pending_rows(db):
    for i in range(25):
        db.add(status="resolved" if i % 2 else "escalated", lineage=f"old-{i}", resolution="x")
    late = db.add(lineage="late")
    _sweep()
    assert late["status"] == "sent"


# ── 9: 대표님 상신 ────────────────────────────────────────────────────────────

def test_ceo_notify_true_marks_sent_with_unique_title(db):
    row = db.add(kind="ceo")
    _sweep()
    assert row["status"] == "sent"
    title = db.notified[0]
    assert title.startswith("[재작업 소진] 목표A / 마일스톤A (") and row["id"][:8] in title
    assert row["delivery_ref"] == title


def test_ceo_notify_false_stays_pending_then_gives_up(db):
    async def refuse(*_a):
        return False
    db.notify_impl = refuse
    row = db.add(kind="ceo")
    _sweep()
    assert row["status"] == "pending" and row["attempts"] == 1 and row["last_error"] == "notify_false"
    for _ in range(10):
        _sweep()
    assert row["status"] == "undeliverable"


def test_ceo_title_is_pinned_before_notify_and_not_rebuilt(db):
    row = db.add(kind="ceo")
    db.fail_ceo_sent_update = 1
    _sweep()
    assert row["status"] == "pending" and len(db.notified) == 1
    assert row["delivery_ref"] == db.notified[0]
    db.goal["title"] = "바뀐 목표"
    db.milestones[MILESTONE_ID]["title"] = "바뀐 마일스톤"
    _sweep()
    assert len(db.notified) == 1  # 저장된 제목으로 alert_history 를 찾아 notify 0회
    assert row["status"] == "sent" and row["delivery_ref"] == db.notified[0]
    assert "바뀐" not in row["delivery_ref"]


def test_ceo_existing_alert_title_skips_notify(db):
    row = db.add(kind="ceo")
    title = triage._ceo_title({"id": row["id"], "goal_title": "목표A", "milestone_title": "마일스톤A"})
    db.alerts.append(title)
    row["delivery_ref"] = title

    async def boom(*_a):
        raise AssertionError("notify must not be called")
    db.notify_impl = boom
    _sweep()
    assert row["status"] == "sent"


def test_one_failing_row_does_not_block_the_others(db):
    first = db.add(lineage="a")
    second = db.add(lineage="b")

    async def flaky(session_id, message, dedupe_key):
        if first["id"] in dedupe_key:
            raise RuntimeError("x")
        return {"queue_id": "ok", "queue_status": "pending", "created": True}
    db.enqueue_impl = flaky
    _sweep()
    assert first["status"] == "pending" and second["status"] == "sent"


def test_missing_table_skips_quietly(db):
    db.table = False
    assert _sweep() == {"skipped": "no_table"}


# ── 10: 소스 제약 ─────────────────────────────────────────────────────────────

def test_module_source_has_no_stream_or_blocking_wait_primitives():
    source = Path(triage.__file__).read_text(encoding="utf-8")
    for forbidden in ("send_message_stream", "_send_semaphore", "wait_for"):
        assert forbidden not in source, forbidden
