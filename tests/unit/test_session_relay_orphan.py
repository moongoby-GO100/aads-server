"""고아 pending relay 회수 (AADS-SESSION-RELAY-RESUME-ORPHAN-20261002).

대상 응답이 execution_resume 으로 다른 프로세스에서 이어 써져 완성됐는데
`_run_relay` 태스크가 컷오버로 사라져 relay 가 pending 으로 남던 사고를 막는다.
"""
import asyncio
import datetime as _dt
import inspect
import json
from pathlib import Path

from app.services import session_relay

RELAY = "9fe3c45c-1111-4222-8333-444444444444"
OTHER_RELAY = "b9c0ff35-1111-4222-8333-444444444444"
ORIGIN = "7fb5f50a-1111-4222-8333-444444444444"
TARGET = "65f926cb-1111-4222-8333-444444444444"
EXEC = "505a3cd0-1111-4222-8333-444444444444"
ANSWER_ID = "fae0b38b-1111-4222-8333-444444444444"
FINAL = "결론: 슬리피지 영향은 미미합니다."


def _msg(mid, content, intent=""):
    return {"id": mid, "content": content, "intent": intent}


class _Pool:
    """session_relay / chat_messages / chat_turn_executions 를 흉내 내는 상태 있는 풀."""

    def __init__(self, relay_overrides=None, exec_status="completed", messages=None,
                 tagged=None, no_exec_column=False, target_busy=False):
        self.relays = {RELAY: {
            "id": RELAY, "origin": ORIGIN, "target": TARGET, "goal_id": None,
            "question": "슬리피지 의견?", "target_execution_id": EXEC,
            "created_at": _dt.datetime(2026, 10, 2, 9, 32, tzinfo=_dt.timezone.utc),
            "age_sec": 600, "status": "pending", "pending_reply": None,
            "error": None, "answer_message_id": None,
            **(relay_overrides or {}),
        }}
        self.exec_status = exec_status
        self.messages = messages if messages is not None else []
        self.tagged = tagged or []
        self.no_exec_column = no_exec_column
        self.target_busy = target_busy
        self.queries = []

    async def fetch(self, query, *args):
        self.queries.append(query)
        if "FROM session_relay WHERE status = 'pending'" in query:
            if self.no_exec_column and "target_execution_id::text" in query:
                raise RuntimeError('column "target_execution_id" does not exist')
            return [dict(r, target_execution_id=None if self.no_exec_column else r["target_execution_id"])
                    for r in self.relays.values()
                    if r["status"] == "pending" and r["pending_reply"] is None]
        if "strpos(m.content" in query:
            return self.tagged
        if "FROM chat_messages WHERE execution_id" in query:
            return self.messages if args[0] == EXEC else []
        return []

    async def fetchval(self, query, *args):
        if "FROM chat_turn_executions WHERE id" in query:
            return self.exec_status
        if "coalesce(role_key, title)" in query:
            return "CTO"
        if "FROM chat_turn_executions" in query:  # _target_is_busy
            return 1 if self.target_busy else 0
        return None

    async def execute(self, query, *args):
        relay = self.relays[args[0]]
        if "SET target_execution_id" in query:
            if relay["target_execution_id"] is None:
                relay["target_execution_id"] = args[1]
                return "UPDATE 1"
            return "UPDATE 0"
        if "SET pending_reply = $2" in query:
            if relay["status"] == "pending" and relay["pending_reply"] is None:
                relay.update(pending_reply=args[1], answer_message_id=args[2])
                return "UPDATE 1"
            return "UPDATE 0"
        if "error='orphaned_no_answer'" in query:
            if relay["status"] == "pending" and relay["pending_reply"] is None:
                relay.update(status="failed", error="orphaned_no_answer")
                return "UPDATE 1"
            return "UPDATE 0"
        if "SET status='answered'" in query:
            relay.update(status="answered", pending_reply=None)
            return "UPDATE 1"
        if "SET status='blocked'" in query:
            relay["status"] = "blocked"
            return "UPDATE 1"
        if "SET status='failed'" in query:
            relay.update(status="failed", error=str(args[1]) if len(args) > 1 else None)
            return "UPDATE 1"
        return "UPDATE 0"


def _harness(monkeypatch, pool):
    from app.core import db_pool

    deliveries = []

    async def deliver(origin, content, goal_id=None, relay_id=""):
        deliveries.append((origin, content, relay_id))
        return True

    async def busy(_sid):
        return pool.target_busy

    monkeypatch.setattr(db_pool, "get_pool", lambda: pool)
    monkeypatch.setattr(session_relay, "_deliver_answer", deliver)
    monkeypatch.setattr(session_relay, "_target_is_busy", busy)
    session_relay._active_relays.discard(RELAY)
    return deliveries


def _reconcile():
    async def _go():
        out = await session_relay.reconcile_orphan_relays()
        await asyncio.gather(*list(session_relay._running), return_exceptions=True)
        return out

    return asyncio.run(_go())


def test_orphan_with_resumed_final_answer_is_answered_once(monkeypatch):
    # 재개는 같은 execution_id 의 placeholder 를 최종 메시지로 UPDATE 한다.
    # 앞서 보관된 부분 응답(_archived_partial)이 더 최신일 수 있어도 답으로 쓰지 않는다.
    pool = _Pool(messages=[
        _msg("m3", "[보관된 부분 응답]", "_archived_partial"),
        _msg(ANSWER_ID, FINAL, "status_check"),
    ])
    deliveries = _harness(monkeypatch, pool)
    out = _reconcile()
    assert out["answered"] == 1
    assert len(deliveries) == 1
    origin, content, relay_id = deliveries[0]
    assert origin == ORIGIN and relay_id == RELAY
    assert FINAL in content and "📨 **CTO의 답이 도착했습니다**" in content
    assert content.endswith(f"(relay_id={RELAY})")
    assert pool.relays[RELAY]["status"] == "answered"
    assert pool.relays[RELAY]["answer_message_id"] == ANSWER_ID


def test_resume_in_progress_placeholder_keeps_pending(monkeypatch):
    pool = _Pool(exec_status="retrying", messages=[
        _msg("ph", "…작업 중", "streaming_placeholder"),
    ])
    deliveries = _harness(monkeypatch, pool)
    out = _reconcile()
    assert out["waiting"] == 1 and out["answered"] == 0 and out["failed"] == 0
    assert deliveries == []
    assert pool.relays[RELAY]["status"] == "pending"


def test_running_execution_waits_even_if_a_valid_looking_message_exists(monkeypatch):
    pool = _Pool(exec_status="running", messages=[_msg("m1", "중간 산출", "status_check")])
    deliveries = _harness(monkeypatch, pool)
    assert _reconcile()["waiting"] == 1
    assert deliveries == []


def test_terminated_execution_without_answer_fails(monkeypatch):
    for exec_status, msgs in (
        ("interrupted", [_msg("x", "중단됨", "interruption_notice")]),
        ("completed", []),
        ("interrupted", [_msg("ph", "", "streaming_placeholder")]),
    ):
        pool = _Pool(exec_status=exec_status, messages=msgs)
        deliveries = _harness(monkeypatch, pool)
        out = _reconcile()
        assert out["failed"] == 1, (exec_status, msgs)
        assert deliveries == []
        assert pool.relays[RELAY]["status"] == "failed"
        assert pool.relays[RELAY]["error"] == "orphaned_no_answer"


def test_reconciling_twice_delivers_once(monkeypatch):
    pool = _Pool(messages=[_msg(ANSWER_ID, FINAL, "status_check")])
    deliveries = _harness(monkeypatch, pool)
    _reconcile()
    _reconcile()
    assert len(deliveries) == 1


def test_concurrent_reconcile_delivers_once(monkeypatch):
    pool = _Pool(messages=[_msg(ANSWER_ID, FINAL, "status_check")])
    deliveries = _harness(monkeypatch, pool)

    async def _go():
        await asyncio.gather(session_relay.reconcile_orphan_relays(),
                             session_relay.reconcile_orphan_relays())
        await asyncio.gather(*list(session_relay._running), return_exceptions=True)

    asyncio.run(_go())
    assert len(deliveries) == 1
    assert pool.relays[RELAY]["status"] == "answered"


def test_live_local_task_is_not_reclaimed(monkeypatch):
    pool = _Pool(messages=[_msg(ANSWER_ID, FINAL, "status_check")])
    deliveries = _harness(monkeypatch, pool)
    session_relay._active_relays.add(RELAY)
    try:
        assert _reconcile()["skipped"] == 1
    finally:
        session_relay._active_relays.discard(RELAY)
    assert deliveries == []


def test_relay_tag_path_when_execution_id_missing(monkeypatch):
    tagged = [
        # 다른 relay 의 표식만 있는 메시지는 건너뛴다(본문 인용 등).
        {"content": f"질문 (relay_id={OTHER_RELAY})", "execution_id": "ffffffff-1111-4222-8333-444444444444"},
        {"content": f"[CTO에게 — x(7fb5f50a)의 질문]\n질문\n\n(relay_id={RELAY})", "execution_id": EXEC},
    ]
    pool = _Pool(relay_overrides={"target_execution_id": None}, tagged=tagged,
                 messages=[_msg(ANSWER_ID, FINAL, "status_check")])
    deliveries = _harness(monkeypatch, pool)
    out = _reconcile()
    assert out["answered"] == 1 and len(deliveries) == 1
    assert pool.relays[RELAY]["target_execution_id"] == EXEC


def test_no_execution_info_waits_then_gives_up(monkeypatch):
    pool = _Pool(relay_overrides={"target_execution_id": None, "age_sec": 300})
    deliveries = _harness(monkeypatch, pool)
    assert _reconcile()["waiting"] == 1
    assert pool.relays[RELAY]["status"] == "pending"

    pool.relays[RELAY]["age_sec"] = session_relay._ORPHAN_GIVEUP_SEC + 1
    pool.target_busy = True
    assert _reconcile()["waiting"] == 1  # 대상이 바쁘면 그 실행일 수 있다

    pool.target_busy = False
    assert _reconcile()["failed"] == 1
    assert pool.relays[RELAY]["error"] == "orphaned_no_answer"
    assert deliveries == []


def test_works_when_execution_column_is_missing(monkeypatch):
    tagged = [{"content": f"질문\n\n(relay_id={RELAY})", "execution_id": EXEC}]
    pool = _Pool(tagged=tagged, no_exec_column=True,
                 messages=[_msg(ANSWER_ID, FINAL, "status_check")])
    deliveries = _harness(monkeypatch, pool)
    assert _reconcile()["answered"] == 1
    assert len(deliveries) == 1


def test_unrelated_session_messages_are_never_used(monkeypatch):
    # 대상 실행에 묶인 메시지가 없으면, 세션에 다른 최근 assistant 메시지가 있어도 답이 아니다.
    pool = _Pool(exec_status="completed", messages=[])
    pool.messages_other = [_msg("zzz", "무관한 중단 안내문", "status_check")]
    deliveries = _harness(monkeypatch, pool)
    assert _reconcile()["failed"] == 1
    assert deliveries == []


def test_delivery_is_not_blocking_the_cycle_and_blocked_goes_back(monkeypatch):
    pool = _Pool(messages=[_msg(ANSWER_ID, FINAL, "status_check")])
    _harness(monkeypatch, pool)

    async def paused(*_a, **_k):
        return False

    monkeypatch.setattr(session_relay, "_deliver_answer", paused)
    _reconcile()
    # 멈춘 목표면 회신을 붙잡아 두고(blocked) 기존 해제 경로가 이어받는다.
    assert pool.relays[RELAY]["status"] == "blocked"
    assert pool.relays[RELAY]["pending_reply"]


def test_too_old_pending_is_not_selected():
    sql = session_relay._ORPHAN_SELECT
    assert "created_at > now() - ($2::int * interval '1 hour')" in sql
    assert "pending_reply IS NULL" in sql and "status = 'pending'" in sql


def test_not_answer_intents_mirror_run_relay():
    src = inspect.getsource(session_relay._run_relay)
    for intent in session_relay._NOT_ANSWER_INTENTS:
        assert intent in src, f"{intent} 가 _run_relay 의 걸러내기 목록에 없다"


# ── _run_relay: 실행 id 기록 + 회신 선점 ─────────────────────────────────


class _RunPool(_Pool):
    def __init__(self):
        super().__init__(relay_overrides={"target_execution_id": None})
        self.events = []

    async def fetchrow(self, query, *args):
        return {"id": ANSWER_ID, "content": FINAL, "intent": "status_check"}

    async def execute(self, query, *args):
        if "SET target_execution_id" in query:
            self.events.append("recorded")
        return await super().execute(query, *args)


def _patch_stream(monkeypatch, pool, ids=(EXEC,)):
    from app.services import chat_service

    async def stream(**_kw):
        for eid in ids:
            yield "data: " + json.dumps({"type": "start", "execution_id": eid}) + "\n\n"
        pool.events.append("stream_end")

    monkeypatch.setattr(chat_service, "send_message_stream", stream)


def test_run_relay_records_execution_before_stream_ends(monkeypatch):
    pool = _RunPool()
    deliveries = _harness(monkeypatch, pool)
    _patch_stream(monkeypatch, pool)

    async def never_paused(*_a):
        return False

    monkeypatch.setattr(session_relay, "_relay_paused", never_paused)
    asyncio.run(session_relay._run_relay(RELAY, TARGET, "prompt", ORIGIN, "슬리피지 의견?"))
    assert pool.events == ["recorded", "stream_end"]
    assert pool.relays[RELAY]["target_execution_id"] == EXEC
    assert len(deliveries) == 1
    assert pool.relays[RELAY]["status"] == "answered"
    assert RELAY not in session_relay._active_relays


def test_run_relay_does_not_deliver_when_reply_already_claimed(monkeypatch):
    pool = _RunPool()
    deliveries = _harness(monkeypatch, pool)
    _patch_stream(monkeypatch, pool)

    async def never_paused(*_a):
        return False

    monkeypatch.setattr(session_relay, "_relay_paused", never_paused)
    pool.relays[RELAY]["pending_reply"] = "다른 경로가 이미 집었다"
    asyncio.run(session_relay._run_relay(RELAY, TARGET, "prompt", ORIGIN, "q"))
    assert deliveries == []
    assert pool.relays[RELAY]["status"] == "pending"
    assert RELAY not in session_relay._active_relays


def test_run_relay_unregisters_even_on_failure(monkeypatch):
    pool = _RunPool()
    _harness(monkeypatch, pool)
    _patch_stream(monkeypatch, pool, ids=())

    async def never_paused(*_a):
        return False

    monkeypatch.setattr(session_relay, "_relay_paused", never_paused)
    asyncio.run(session_relay._run_relay(RELAY, TARGET, "prompt", ORIGIN, "q"))
    assert pool.relays[RELAY]["status"] == "failed"
    assert RELAY not in session_relay._active_relays


# ── dispatch 연결 / 마이그레이션 ─────────────────────────────────────────


def test_dispatch_cycle_runs_reconcile_and_survives_its_failure(monkeypatch):
    from app.core import db_pool

    class Empty:
        async def fetch(self, *_a):
            return []

        async def fetchrow(self, *_a):
            return None

        async def fetchval(self, *_a):
            return None

        async def execute(self, *_a):
            return "UPDATE 0"

    monkeypatch.setattr(db_pool, "get_pool", lambda: Empty())
    calls = []

    async def ok():
        calls.append(1)
        return {"answered": 2, "failed": 1, "waiting": 0, "skipped": 0}

    monkeypatch.setattr(session_relay, "reconcile_orphan_relays", ok)
    out = asyncio.run(session_relay.dispatch_queued_relays())
    assert calls == [1]
    assert out["reclaimed"] == 2 and out["orphan_failed"] == 1

    async def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(session_relay, "reconcile_orphan_relays", boom)
    out = asyncio.run(session_relay.dispatch_queued_relays())
    assert out["reclaimed"] == 0 and "sent" in out and "expired" in out


def test_migration_is_idempotent_and_never_drops():
    sql = Path("migrations/20261002_session_relay_target_execution.sql").read_text(encoding="utf-8")
    assert "ADD COLUMN IF NOT EXISTS target_execution_id uuid" in sql
    statements = "\n".join(l for l in sql.splitlines() if not l.strip().startswith("--")).upper()
    assert "DROP COLUMN" not in statements and "DROP TABLE" not in statements
