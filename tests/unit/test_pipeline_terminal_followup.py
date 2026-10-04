"""러너 종결(done/error) 결과 검토가 내구 큐로 한 번만 전달된다.

배경: notify 가 TERMINAL_JOB_STATUSES 에서 조기 return 해서 done/error 후속 검토 branch 에
도달할 수 없었고, 2026-10-04 07:45 에 끝난 execution 뒤로 자동 턴이 이어지지 않았다.
승인 재검수 트리거의 stale 억제는 그대로 두고, 종결 결과 검토만 별도 event type
(dedupe_key `runner_terminal:<job>:<status>:<sha>`) 으로 chat_deferred_reactions 에 넣는다.
"""
from __future__ import annotations

import os
import uuid
from types import SimpleNamespace

import pytest

os.environ.setdefault("JWT_SECRET_KEY", "unit-test-secret")
os.environ.setdefault("E2B_API_KEY", "unit-test-e2b-key")

SESSION = "22222222-2222-4222-8222-222222222222"
SHA = "a" * 40
JOB = "runner-abcd1234"


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False


class _World:
    """DB 한 벌. 여러 '프로세스(슬롯)' 가 같은 큐 행을 본다."""

    def __init__(self, status: str, *, sha: str | None = SHA, session_ok: bool = True,
                 session_id: str | None = SESSION):
        self.status = status
        self.sha = sha
        self.session_ok = session_ok
        self.session_id = session_id
        self.queue: list[dict] = []
        self.queries: list[str] = []

    def pool(self):
        world = self

        class _Conn:
            def transaction(self):
                return _Tx()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_a):
                return False

            async def execute(self, query, *_a):
                world.queries.append(query)

            async def fetch(self, *_a):
                return []

            async def fetchval(self, query, *_a):
                if "information_schema.columns" in query:
                    return True
                raise AssertionError(query)

            async def fetchrow(self, query, *args):
                world.queries.append(query)
                if "SELECT status FROM pipeline_jobs" in query:
                    return {"status": world.status}
                if "SELECT job_id, project, status" in query:
                    return {
                        "job_id": JOB, "project": "AADS", "status": world.status,
                        "phase": world.status, "chat_session_id": world.session_id,
                        "error_detail": "deploy_failed" if world.status == "error" else None,
                        "review_feedback": None,
                        "output_preview": "17 tests passed", "instruction_preview": "원 지시",
                    }
                if "AS session_ok" in query:
                    return {"commit_hash": world.sha, "session_ok": world.session_ok}
                if "FROM chat_deferred_reactions" in query:
                    sid, key = args[0], args[1]
                    for item in world.queue:
                        if item["session_id"] == sid and item["dedupe_key"] == key:
                            return {"id": item["id"], "status": item["status"]}
                    return None
                if "INSERT INTO chat_deferred_reactions" in query:
                    item = {"id": str(uuid.uuid4()), "session_id": args[0],
                            "message": args[1], "dedupe_key": args[2], "status": "pending"}
                    world.queue.append(item)
                    return {"id": item["id"], "status": item["status"]}
                raise AssertionError(query)

        class _Pool:
            def acquire(self):
                return _Conn()

        return _Pool()


@pytest.fixture
def wired(monkeypatch):
    from app.api import pipeline_runner
    import app.core.db_pool as db_pool
    import app.services.chat_service as chat_service
    import app.services.pipeline_runner_service as pipeline_runner_service

    state = SimpleNamespace(world=None, direct_triggers=[])

    async def _noop(*_a, **_k):
        return None

    async def _no_orphans(*_a, **_k):
        return []

    async def _trigger(*a, **k):
        state.direct_triggers.append((a, k))

    def _use(world: _World):
        state.world = world
        monkeypatch.setattr(db_pool, "get_pool", lambda: world.pool())
        monkeypatch.setattr(chat_service, "get_pool", lambda: world.pool())

    monkeypatch.setattr(pipeline_runner, "promote_next_queued", _noop)
    monkeypatch.setattr(pipeline_runner, "_cascade_cleanup_orphans_with_ids", _no_orphans)
    monkeypatch.setattr(pipeline_runner, "_record_terminal_failure_candidate", _noop)
    monkeypatch.setattr(pipeline_runner_service, "_update_linked_goal_state_with_phase", _noop)
    monkeypatch.setattr(chat_service, "trigger_ai_reaction", _trigger)
    monkeypatch.setattr(
        pipeline_runner, "logger",
        SimpleNamespace(info=lambda *_a, **_k: None, warning=lambda *_a, **_k: None),
    )
    state.use = _use
    state.notify = pipeline_runner.notify_completion
    return state


@pytest.mark.asyncio
async def test_done_job_queues_one_completion_review(wired):
    world = _World("done")
    wired.use(world)

    result = await wired.notify(JOB)

    assert result["status"] == "followup_queued"
    assert result["followup_kind"] == "completed"
    assert result["dedupe_key"] == f"runner_terminal:{JOB}:done:{SHA}"
    assert len(world.queue) == 1
    queued = world.queue[0]
    assert queued["session_id"] == SESSION
    assert queued["dedupe_key"] == f"runner_terminal:{JOB}:done:{SHA}"
    assert JOB in queued["message"] and "종결 결과 검토 (완료)" in queued["message"]
    assert wired.direct_triggers == []


@pytest.mark.asyncio
async def test_error_job_queues_one_failure_review(wired):
    world = _World("error")
    wired.use(world)

    result = await wired.notify(JOB)

    assert result["status"] == "followup_queued"
    assert result["followup_kind"] == "failed"
    assert "종결 결과 검토 (실패)" in world.queue[0]["message"]
    assert "deploy_failed" in world.queue[0]["message"]


@pytest.mark.asyncio
async def test_duplicate_notify_does_not_queue_a_second_review(wired):
    world = _World("done")
    wired.use(world)

    first = await wired.notify(JOB)
    second = await wired.notify(JOB)

    assert first["status"] == "followup_queued"
    assert second["status"] == "skipped"
    assert second["reason"] == "terminal_followup_already_queued"
    assert second["queue_id"] == first["queue_id"]
    assert len(world.queue) == 1


@pytest.mark.asyncio
async def test_slot_replacement_between_notifies_keeps_single_durable_event(wired):
    """notify 가 서로 다른 API 슬롯(프로세스)에 도착해도 같은 DB 행을 본다."""
    world = _World("done")
    wired.use(world)
    first = await wired.notify(JOB)

    wired.use(world)  # 새 슬롯: 풀·프로세스 메모리는 새것, DB 는 그대로
    second = await wired.notify(JOB)

    assert first["queue_id"] == second["queue_id"]
    assert len(world.queue) == 1
    assert world.queue[0]["status"] == "pending"


@pytest.mark.asyncio
async def test_done_and_error_of_same_job_each_get_one_review(wired):
    world = _World("done")
    wired.use(world)
    await wired.notify(JOB)
    world.status = "error"
    await wired.notify(JOB)
    await wired.notify(JOB)

    keys = sorted(item["dedupe_key"] for item in world.queue)
    assert keys == [f"runner_terminal:{JOB}:done:{SHA}", f"runner_terminal:{JOB}:error:{SHA}"]


@pytest.mark.asyncio
async def test_new_sha_is_a_new_event(wired):
    world = _World("done")
    wired.use(world)
    await wired.notify(JOB)
    world.sha = "b" * 40
    await wired.notify(JOB)

    assert len(world.queue) == 2


@pytest.mark.asyncio
async def test_missing_sha_still_queues_with_stable_key(wired):
    world = _World("done", sha=None)
    wired.use(world)

    first = await wired.notify(JOB)
    second = await wired.notify(JOB)

    assert first["dedupe_key"] == f"runner_terminal:{JOB}:done:nosha"
    assert second["status"] == "skipped"
    assert len(world.queue) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["cancelled", "rejected_done"])
async def test_cancelled_and_rejected_done_stay_suppressed(wired, status):
    world = _World(status)
    wired.use(world)

    result = await wired.notify(JOB)

    assert result["status"] == "skipped"
    assert result["reason"] == f"terminal status: {status}"
    assert world.queue == []


@pytest.mark.asyncio
async def test_session_of_another_tenant_is_never_queued(wired):
    world = _World("done", session_ok=False)
    wired.use(world)

    result = await wired.notify(JOB)

    assert result == {"status": "skipped", "reason": "session_tenant_mismatch", "promoted_job_id": None}
    assert world.queue == []


@pytest.mark.asyncio
async def test_job_without_chat_session_is_skipped(wired):
    world = _World("done", session_id=None)
    wired.use(world)

    result = await wired.notify(JOB)

    assert result["status"] == "skipped"
    assert result["reason"] == "session_id 없음"
    assert world.queue == []


@pytest.mark.asyncio
async def test_enqueue_failure_is_reported_not_raised(wired, monkeypatch):
    import app.services.chat_service as chat_service

    world = _World("done")
    wired.use(world)

    async def _boom(*_a, **_k):
        raise RuntimeError("queue unavailable")

    monkeypatch.setattr(chat_service, "enqueue_next_step_reaction", _boom)

    result = await wired.notify(JOB)

    assert result["status"] == "error"
    assert result["reason"] == "terminal_followup_enqueue_failed"


def test_terminal_review_bypasses_stale_approval_and_next_step_freshness_guards():
    """종결 검토 문구는 stale 승인 가드의 표식을 쓰지 않고, 접두도 next_step: 이 아니다."""
    import asyncio

    from app.api.pipeline_runner import _terminal_followup_dedupe_key, _terminal_followup_message
    from app.services.chat_service import _find_stale_approval_job_status

    row = {"job_id": JOB, "project": "AADS", "status": "done",
           "output_preview": "ok", "instruction_preview": "x", "error_detail": None}
    message = _terminal_followup_message(row, kind="completed", commit_sha=SHA)
    assert "AI 검수 대기" not in message
    assert not _terminal_followup_dedupe_key(JOB, "done", SHA).startswith("next_step:")

    class _Conn:
        async def fetchval(self, *_a):
            return "done"

    async def _run():
        terminal_review = await _find_stale_approval_job_status(_Conn(), JOB, message)
        approval = await _find_stale_approval_job_status(
            _Conn(), JOB, f"[시스템] Pipeline Runner 작업이 AI 검수 대기 상태입니다.\n{JOB}"
        )
        return terminal_review, approval

    terminal_review, approval = asyncio.run(_run())
    assert terminal_review is None  # 종결 검토는 배달된다
    assert approval == "done"  # 승인 재검수 트리거는 여전히 폐기된다


def test_notify_enqueues_before_returning_and_keeps_approval_suppression():
    from pathlib import Path

    source = (Path(__file__).resolve().parents[2] / "app/api/pipeline_runner.py").read_text(encoding="utf-8")
    notify_fn = source.split("async def notify_completion", 1)[1].split(
        '@router.post("/pipeline/jobs/{job_id}/approve"', 1
    )[0]
    guard = notify_fn.split("if status in TERMINAL_JOB_STATUSES:", 1)[1].split("session_id = row", 1)[0]

    assert "await _enqueue_terminal_followup(" in guard
    assert "asyncio.create_task" not in guard  # 프로세스 메모리 태스크에 의존하지 않는다
    assert 'logger.info("pipeline_runner.notify_terminal_suppressed"' in guard
    # 승인 재검수 트리거 직전 재조회 가드는 그대로
    assert "notify_ai_suppressed" in notify_fn
