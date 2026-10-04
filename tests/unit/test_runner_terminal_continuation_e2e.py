"""러너 종결 → 내구 큐 → 검토 턴 → 원 세션, 적재와 소비를 한 DB 로 잇는 격리 통합 테스트.

기존 `test_pipeline_terminal_followup.py` 는 적재(notify) 쪽만, `test_stale_approval_trigger_guard.py`
는 소비(drain) 쪽만 고정한다. 여기서는 같은 인메모리 DB 를 두 쪽이 공유해 계약을 끝에서 끝까지 본다.

한계(보고서에도 적는다): SQL 은 실행되지 않는다. fake 는 SQL 의 의미(후보 선택·청구·환불·펜싱 조건)를
파이썬으로 흉내 내며, SQL 문구 자체는 소스 계약 테스트로 따로 고정한다. 모델 호출은 스텁이다.
"""
from __future__ import annotations

import asyncio
import inspect
import os
import re
import socket
import uuid
from types import SimpleNamespace

import pytest

os.environ.setdefault("JWT_SECRET_KEY", "unit-test-secret")
os.environ.setdefault("E2B_API_KEY", "unit-test-e2b-key")

SESSION = "33333333-3333-4333-8333-333333333333"
SHA = "b" * 40
JOB = "runner-e2e00001"


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False


class _Db:
    """pipeline_jobs 한 줄 + chat_deferred_reactions + 세션 실행 lease 를 가진 DB 한 벌."""

    def __init__(self, *, status: str = "done", sha: str | None = SHA):
        self.job_status = status
        self.sha = sha
        self.queue: list[dict] = []
        self.live_sessions: set[str] = set()
        self.claim_blind_to_live = False
        self.active = True
        self.execution_writes: list[str] = []
        self.pipeline_status_reads = 0

    def row(self, key_fragment: str | None = None) -> dict:
        rows = [r for r in self.queue if key_fragment is None or key_fragment in (r["dedupe_key"] or "")]
        assert len(rows) == 1, [r["dedupe_key"] for r in self.queue]
        return rows[0]

    def pool(self):
        db = self

        class _Conn:
            def transaction(self):
                return _Tx()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_a):
                return False

            async def execute(self, query, *args):
                if "chat_turn_executions" in query:
                    db.execution_writes.append(query)
                if "SET status = 'failed'" in query and "attempts >= 8" in query:
                    for r in db.queue:
                        if r["status"] == "pending" and r["attempts"] >= 8:
                            r["status"] = "failed"
                elif "SET status = 'pending'" in query and "GREATEST(attempts - 1, 0)" in query:
                    for r in db.queue:
                        if r["id"] == str(args[0]) and r["claimed_by"] == args[1]:
                            r.update(status="pending", attempts=max(r["attempts"] - 1, 0),
                                     claimed_by=None, lease_expired=False)
                elif "'skipped_stale'" in query:
                    for r in db.queue:
                        if r["id"] == str(args[0]) and r["claimed_by"] == args[1]:
                            r.update(status="skipped_stale", claimed_by=None)
                elif "SET status = 'completed'" in query:
                    for r in db.queue:
                        if r["id"] == str(args[0]) and r["status"] != "completed":
                            r["status"] = "completed"

            async def fetch(self, query, *args):
                if "WITH candidates AS" not in query:
                    return []
                max_rows, owner = args
                out = []
                for r in sorted(db.queue, key=lambda x: x["seq"]):
                    if len(out) >= max_rows or r["attempts"] >= 8:
                        continue
                    claimable = r["status"] == "pending" or (
                        r["status"] == "claimed" and r["lease_expired"])
                    live = (not db.claim_blind_to_live) and r["session_id"] in db.live_sessions
                    if not claimable or live:
                        continue
                    r.update(status="claimed", claimed_by=owner, attempts=r["attempts"] + 1,
                             lease_expired=False)
                    out.append({
                        "id": r["id"], "session_id": r["session_id"],
                        "system_message": r["message"], "ohvis_task_id": None,
                        "attempts": r["attempts"], "runner_job_id": None,
                        "dedupe_key": r["dedupe_key"],
                    })
                return out

            async def fetchval(self, query, *args):
                if "information_schema.columns" in query:
                    return True
                if "FROM pipeline_jobs" in query:
                    db.pipeline_status_reads += 1
                    return db.job_status
                if "te.lease_expires_at > NOW()" in query:
                    return True if str(args[0]) in db.live_sessions else None
                if "'interrupted', 'retrying'" in query:
                    return None
                raise AssertionError(query)

            async def fetchrow(self, query, *args):
                if "SELECT status FROM pipeline_jobs" in query:
                    return {"status": db.job_status}
                if "SELECT job_id, project, status" in query:
                    return {
                        "job_id": args[0], "project": "AADS", "status": db.job_status,
                        "phase": db.job_status, "chat_session_id": SESSION,
                        "error_detail": "deploy_failed" if db.job_status == "error" else None,
                        "review_feedback": None,
                        "output_preview": "17 tests passed", "instruction_preview": "원 지시",
                    }
                if "AS session_ok" in query:
                    return {"commit_hash": db.sha, "session_ok": True}
                if "FROM chat_deferred_reactions" in query and "SELECT id::text AS id" in query:
                    for r in db.queue:
                        if r["session_id"] == args[0] and r["dedupe_key"] == args[1]:
                            return {"id": r["id"], "status": r["status"]}
                    return None
                if "INSERT INTO chat_deferred_reactions" in query:
                    item = {"id": str(uuid.uuid4()), "seq": len(db.queue), "session_id": args[0],
                            "message": args[1], "dedupe_key": args[2], "status": "pending",
                            "attempts": 0, "claimed_by": None, "lease_expired": False}
                    db.queue.append(item)
                    return {"id": item["id"], "status": "pending"}
                if "FROM chat_turn_executions te" in query:
                    return None
                raise AssertionError(query)

        class _Pool:
            def acquire(self):
                return _Conn()

        return _Pool()


@pytest.fixture
def env(monkeypatch):
    from app.api import pipeline_runner
    import app.core.db_pool as db_pool
    import app.services.chat_service as chat_service
    import app.services.pipeline_runner_service as pipeline_runner_service

    st = SimpleNamespace(db=None, model_calls=[], external=[], cs=chat_service)

    async def _noop(*_a, **_k):
        return None

    async def _no_orphans(*_a, **_k):
        return []

    async def _model(session_id, content):
        st.model_calls.append((session_id, content))
        return None

    def _use(db: _Db):
        st.db = db
        monkeypatch.setattr(db_pool, "get_pool", lambda: db.pool())
        monkeypatch.setattr(chat_service, "get_pool", lambda: db.pool())
        monkeypatch.setattr(chat_service, "_is_local_active_api_slot", lambda: db.active)
        return db

    def _record(name):
        async def _rec(*a, **k):
            st.external.append(name)
            return {"sent": 0}
        return _rec

    def _connect_blocked(self, *a, **k):
        st.external.append(f"socket.connect{a[:1]}")
        raise OSError("external network is blocked in this test")

    monkeypatch.setattr(pipeline_runner, "promote_next_queued", _noop)
    monkeypatch.setattr(pipeline_runner, "_cascade_cleanup_orphans_with_ids", _no_orphans)
    monkeypatch.setattr(pipeline_runner, "_record_terminal_failure_candidate", _noop)
    monkeypatch.setattr(pipeline_runner_service, "_update_linked_goal_state_with_phase", _noop)
    monkeypatch.setattr(chat_service, "_consume_internal_reaction_stream", _model)
    monkeypatch.setattr(
        pipeline_runner, "logger",
        SimpleNamespace(info=lambda *_a, **_k: None, warning=lambda *_a, **_k: None),
    )
    monkeypatch.setattr(socket.socket, "connect", _connect_blocked)
    try:
        import app.services.push_notifications as push
        for name in ("notify_chat_response_complete", "notify_pipeline_job_complete", "send_web_push_to_user"):
            monkeypatch.setattr(push, name, _record(f"push.{name}"))
    except ImportError:  # pragma: no cover - 의존성 없는 환경
        pass
    import app.services.telegram_bot as telegram_bot
    monkeypatch.setattr(telegram_bot, "get_telegram_bot", lambda: st.external.append("telegram") or None)

    for table in (chat_service._ai_reaction_active, chat_service._ai_reaction_queue,
                  chat_service._deferred_consecutive_count):
        table.clear()
    chat_service._active_bg_tasks.pop(SESSION, None)

    st.use = _use
    st.notify = pipeline_runner.notify_completion

    async def _drain() -> int:
        started = await chat_service._process_deferred_reactions_once()
        # 종료 콜백이 한 틱 뒤에 완료 기록 태스크를 새로 만들므로, 빈 것을 본 뒤에도 몇 틱 더 돈다.
        quiet = 0
        for _ in range(200):
            pending = [t for t in asyncio.all_tasks()
                       if t is not asyncio.current_task() and not t.done()]
            quiet = 0 if pending else quiet + 1
            if quiet >= 5:
                break
            await asyncio.sleep(0)
        return started

    st.drain = _drain
    yield st
    chat_service._active_bg_tasks.pop(SESSION, None)
    assert st.external == [], f"외부 발신 시도: {st.external}"


# ── 1. 종결 이벤트 → 큐 → 검토 턴 → 원 세션 ─────────────────────────────


@pytest.mark.asyncio
async def test_trace_terminal_event_to_session_review(env):
    db = env.use(_Db())

    result = await env.notify(JOB)
    assert result["status"] == "followup_queued"
    assert result["dedupe_key"] == f"runner_terminal:{JOB}:done:{SHA}"
    # 적재는 응답 전에 끝나 있다 — 배달 전에도 DB 에 남는다.
    assert db.row()["status"] == "pending" and db.row()["attempts"] == 0
    assert env.model_calls == []

    assert await env.drain() == 1
    assert len(env.model_calls) == 1
    sid, content = env.model_calls[0]
    assert sid == SESSION
    assert "종결 결과 검토 (완료)" in content and JOB in content and SHA in content
    assert "AI 검수 대기" not in content
    row = db.row()
    assert (row["status"], row["attempts"]) == ("completed", 1)

    assert await env.drain() == 0
    assert len(env.model_calls) == 1


@pytest.mark.asyncio
async def test_error_terminal_event_is_reviewed_as_failure(env):
    db = env.use(_Db(status="error"))
    await env.notify(JOB)
    await env.drain()
    assert len(env.model_calls) == 1
    assert "종결 결과 검토 (실패)" in env.model_calls[0][1]
    assert db.row()["dedupe_key"].startswith(f"runner_terminal:{JOB}:error:")


# ── 2. 중복 notify → 동일 효과 1건 ───────────────────────────────────────


@pytest.mark.asyncio
async def test_duplicate_notifies_yield_exactly_one_effect(env):
    db = env.use(_Db())

    results = await asyncio.gather(*(env.notify(JOB) for _ in range(5)))
    assert sorted(r["status"] for r in results) == ["followup_queued"] + ["skipped"] * 4
    assert len(db.queue) == 1

    await env.drain()
    await env.notify(JOB)  # 배달 뒤의 늦은 중복
    await env.drain()
    assert len(env.model_calls) == 1
    assert len(db.queue) == 1 and db.row()["status"] == "completed"


# ── 3. busy 사용자 턴 보존 ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_live_user_turn_is_never_touched_and_review_waits(env):
    db = env.use(_Db())
    await env.notify(JOB)
    db.live_sessions.add(SESSION)

    for _ in range(12):  # 상한 8 보다 많이 돌려도 busy 로는 예산이 줄지 않는다
        assert await env.drain() == 0
    row = db.row()
    assert (row["status"], row["attempts"]) == ("pending", 0)
    assert env.model_calls == []
    assert db.execution_writes == []  # 진행 중 실행 행은 읽기만, 쓰기 0

    db.live_sessions.clear()  # 사용자 턴이 끝난다
    assert await env.drain() == 1
    assert len(env.model_calls) == 1
    assert db.row()["attempts"] == 1


@pytest.mark.asyncio
async def test_turn_that_starts_between_claim_and_launch_refunds_the_attempt(env):
    db = env.use(_Db())
    await env.notify(JOB)
    db.live_sessions.add(SESSION)
    db.claim_blind_to_live = True  # 청구 쿼리는 못 봤지만 trigger 단계에서는 보인다

    for _ in range(10):
        assert await env.drain() == 0
        row = db.row()
        assert (row["status"], row["attempts"]) == ("pending", 0)
    assert env.model_calls == []

    db.live_sessions.clear()
    assert await env.drain() == 1
    assert len(env.model_calls) == 1


@pytest.mark.asyncio
async def test_in_process_bg_task_defers_without_burning_budget(env):
    db = env.use(_Db())
    await env.notify(JOB)
    env.cs._active_bg_tasks[SESSION] = SimpleNamespace(done=lambda: False)

    assert await env.drain() == 0
    row = db.row()
    assert (row["status"], row["attempts"]) == ("pending", 0)
    assert env.model_calls == []

    env.cs._active_bg_tasks.pop(SESSION)
    assert await env.drain() == 1


# ── 4. active 슬롯 / 재시작 내구성 / 소유 펜싱 ──────────────────────────


@pytest.mark.asyncio
async def test_standby_slot_never_claims_the_review(env):
    db = env.use(_Db())
    await env.notify(JOB)
    db.active = False

    assert await env.drain() == 0
    row = db.row()
    assert (row["status"], row["attempts"], row["claimed_by"]) == ("pending", 0, None)
    assert env.model_calls == []

    db.active = True
    assert await env.drain() == 1


@pytest.mark.asyncio
async def test_slot_replacement_between_notify_and_delivery_keeps_single_effect(env, monkeypatch):
    db = env.use(_Db())
    monkeypatch.setattr(env.cs, "_EXECUTION_OWNER_INSTANCE", "aads-server-blue")
    await env.notify(JOB)

    # 재시작/컷오버: 프로세스 메모리는 비고 소유자 이름이 바뀐다. DB 만 남는다.
    for table in (env.cs._ai_reaction_active, env.cs._ai_reaction_queue):
        table.clear()
    env.cs._active_bg_tasks.clear()
    monkeypatch.setattr(env.cs, "_EXECUTION_OWNER_INSTANCE", "aads-server-green")
    env.use(db)

    await env.notify(JOB)  # 쉘이 새 슬롯으로 notify 를 다시 보내도 같은 키
    assert len(db.queue) == 1
    assert await env.drain() == 1
    assert len(env.model_calls) == 1
    row = db.row()
    assert (row["status"], row["claimed_by"]) == ("completed", "aads-server-green")


@pytest.mark.asyncio
async def test_crashed_delivery_is_reclaimed_once_and_live_claim_is_not_stolen(env, monkeypatch):
    db = env.use(_Db())
    await env.notify(JOB)
    monkeypatch.setattr(env.cs, "_EXECUTION_OWNER_INSTANCE", "aads-server-blue")
    row = db.row()
    row.update(status="claimed", claimed_by="aads-server-blue", attempts=1, lease_expired=False)

    monkeypatch.setattr(env.cs, "_EXECUTION_OWNER_INSTANCE", "aads-server-green")
    assert await env.drain() == 0  # blue 의 리스가 살아 있으면 green 이 가로채지 않는다
    assert env.model_calls == []

    row["lease_expired"] = True  # blue 가 죽고 리스가 만료
    assert await env.drain() == 1
    assert len(env.model_calls) == 1
    assert (row["status"], row["attempts"], row["claimed_by"]) == ("completed", 2, "aads-server-green")


@pytest.mark.asyncio
async def test_refund_is_owner_fenced_so_old_slot_cannot_requeue_a_new_owners_claim(env, monkeypatch):
    db = env.use(_Db())
    await env.notify(JOB)
    row = db.row()
    row.update(status="claimed", claimed_by="aads-server-green", attempts=1)

    monkeypatch.setattr(env.cs, "_EXECUTION_OWNER_INSTANCE", "aads-server-blue")
    async with db.pool().acquire() as conn:
        await conn.execute(
            "UPDATE chat_deferred_reactions SET status = 'pending', "
            "attempts = GREATEST(attempts - 1, 0) WHERE id = $1 AND claimed_by = $2",
            uuid.UUID(row["id"]), "aads-server-blue",
        )
    assert (row["status"], row["attempts"], row["claimed_by"]) == ("claimed", 1, "aads-server-green")


# ── 5. 모델 호출 직전에만 재시도 예산 청구 (실행 단위 epoch 펜싱) ────────


class _ExecConn:
    """chat_turn_executions 한 행. `_claim_resume_model_attempt` 의 WHERE 조건을 그대로 흉내 낸다."""

    def __init__(self, *, owner, epoch, retry_count=0, status="running"):
        self.row = {"owner_instance": owner, "owner_epoch": epoch,
                    "retry_count": retry_count, "status": status}

    async def fetchval(self, query, _eid, owner, epoch, _lease, cap):
        r = self.row
        if (r["owner_instance"] == owner and r["owner_epoch"] == epoch
                and r["retry_count"] < cap and r["status"] in ("running", "retrying")):
            r["retry_count"] += 1
            return r["retry_count"]
        return None

    async def fetchrow(self, query, _eid):
        return dict(self.row)


@pytest.mark.asyncio
async def test_retry_budget_is_charged_only_by_the_epoch_holder_at_model_call():
    import app.services.chat_service as cs

    me = cs._EXECUTION_OWNER_INSTANCE
    eid = uuid.uuid4()

    conn = _ExecConn(owner=me, epoch=3)
    assert await cs._claim_resume_model_attempt(conn, eid, 3) == 1
    assert conn.row["retry_count"] == 1

    # 다른 프로세스가 epoch 를 올려 가져갔다 → 옛 보유자는 펜싱되고 예산은 그대로.
    conn.row["owner_epoch"] = 4
    with pytest.raises(cs.ResumeFencedOut):
        await cs._claim_resume_model_attempt(conn, eid, 3)
    assert conn.row["retry_count"] == 1

    # 소유자 이름이 달라도 마찬가지.
    other = _ExecConn(owner="aads-server-other", epoch=3)
    with pytest.raises(cs.ResumeFencedOut):
        await cs._claim_resume_model_attempt(other, eid, 3)
    assert other.row["retry_count"] == 0

    # 종결된 실행에는 청구되지 않는다.
    done = _ExecConn(owner=me, epoch=3, status="completed")
    with pytest.raises(cs.ResumeFencedOut):
        await cs._claim_resume_model_attempt(done, eid, 3)
    assert done.row["retry_count"] == 0

    # 진짜 소진은 펜싱과 구별된다.
    full = _ExecConn(owner=me, epoch=3, retry_count=cs._EXECUTION_RESUME_MAX_ATTEMPTS)
    with pytest.raises(cs.ResumeAttemptLimitExceeded):
        await cs._claim_resume_model_attempt(full, eid, 3)


def test_charge_site_is_single_and_sits_between_fence_check_and_model_call():
    import app.services.chat_service as cs

    src = inspect.getsource(cs)
    claim_src = inspect.getsource(cs._claim_resume_model_attempt)
    assert claim_src.count("retry_count = retry_count + 1") == 1
    assert src.count("retry_count = retry_count + 1") == 1  # 이 모듈의 유일한 청구 지점

    call_sites = [m.start() for m in re.finditer(r"await _claim_resume_model_attempt\(", src)]
    assert len(call_sites) == 1
    fence = src.rfind('_raise_if_fenced_out("between_model_attempts")', 0, call_sites[0])
    model_call = src.find('"resume_model_call session=', call_sites[0])
    assert 0 < fence < call_sites[0] < model_call
    assert call_sites[0] - fence < 400 and model_call - call_sites[0] < 2500


def test_deferred_claim_sql_contract_busy_session_and_budget():
    import app.services.chat_service as cs

    handler = inspect.getsource(cs._process_deferred_reactions_once)
    claim = handler.split("WITH candidates AS (", 1)[1].split("UPDATE chat_deferred_reactions q", 1)[0]
    for clause in ("te.status IN ('running', 'retrying')", "te.completed_at IS NULL",
                   "te.lease_expires_at > NOW()", "q.attempts < 8",
                   "FOR UPDATE SKIP LOCKED", "ORDER BY q.created_at"):
        assert clause in claim, clause
    # 청구 직후 시작하지 못한 경로는 전부 소유자 펜싱된 환불이다.
    assert handler.count("attempts = GREATEST(attempts - 1, 0)") >= 2
    assert handler.count("WHERE id = $1 AND claimed_by = $2") >= 3
    # 이 쿼리는 실행 행을 쓰지 않는다 — 사용자 턴을 끊을 수단 자체가 없다.
    assert "UPDATE chat_turn_executions" not in handler


# ── 6. 외부 알림 0 ─────────────────────────────────────────────────────


def test_terminal_followup_path_has_no_external_sender_reference():
    from app.api import pipeline_runner
    import app.services.chat_service as cs

    banned = re.compile(r"telegram|slack|smtp|sendgrid|twilio|kakao|requests\.|httpx|aiohttp|"
                        r"send_web_push|notify_chat_response_complete|notify_pipeline_job_complete",
                        re.I)
    for fn in (pipeline_runner._enqueue_terminal_followup,
               pipeline_runner._terminal_followup_message,
               pipeline_runner._terminal_followup_dedupe_key,
               cs.enqueue_next_step_reaction,
               cs._process_deferred_reactions_once):
        assert not banned.search(inspect.getsource(fn)), fn.__name__
