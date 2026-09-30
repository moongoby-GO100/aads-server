"""종결 잡에는 검수 대기 트리거를 발행·배달하지 않는다 (발행 직전 + 큐 배달 직전)."""
from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

SESSION_ID = "22222222-2222-4222-8222-222222222222"


def _approval_message(job_id: str) -> str:
    return (
        "[시스템] Pipeline Runner 작업이 AI 검수 대기 상태입니다.\n\n"
        f"**Job**: {job_id}\n**프로젝트**: AADS\n"
    )


def _deploy_done_message(job_id: str) -> str:
    return f"[시스템] Pipeline Runner 작업 배포 완료\n\n**Job**: {job_id}\n**프로젝트**: AADS\n"


# ── A. notify_completion 발행 가드 ────────────────────────────────────────


def test_terminal_status_set_is_single_definition():
    from app.services.pipeline_runner_service import TERMINAL_JOB_STATUSES

    assert TERMINAL_JOB_STATUSES == frozenset(
        {"done", "error", "rejected_done", "cancelled", "completed"}
    )
    assert "awaiting_approval" not in TERMINAL_JOB_STATUSES


async def _run_notify(monkeypatch, job_id: str, status: str) -> tuple[dict, list]:
    from app.api import pipeline_runner
    import app.core.db_pool as db_pool
    import app.services.chat_service as chat_service
    import app.services.pipeline_runner_service as pipeline_runner_service

    triggers: list = []

    class _Acquire:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def fetchrow(self, query, *_args):
            if "SELECT status FROM pipeline_jobs" in query:
                return {"status": status}
            if "SELECT job_id, project, status" in query:
                return {
                    "job_id": job_id,
                    "project": "AADS",
                    "status": status,
                    "phase": status,
                    "chat_session_id": SESSION_ID,
                    "error_detail": None,
                    "review_feedback": None,
                    "output_preview": "x",
                    "instruction_preview": "x",
                }
            raise AssertionError(query)

        async def fetch(self, *_args):
            return []

    class _Pool:
        def acquire(self):
            return _Acquire()

    async def _noop(*_args, **_kwargs):
        return None

    async def _no_orphans(*_args, **_kwargs):
        return []

    async def _record_trigger(*args, **kwargs):
        triggers.append((args, kwargs))

    monkeypatch.setattr(db_pool, "get_pool", lambda: _Pool())
    monkeypatch.setattr(pipeline_runner, "promote_next_queued", _noop)
    monkeypatch.setattr(pipeline_runner, "_cascade_cleanup_orphans_with_ids", _no_orphans)
    monkeypatch.setattr(pipeline_runner_service, "_update_linked_goal_state_with_phase", _noop)
    monkeypatch.setattr(chat_service, "trigger_ai_reaction", _record_trigger)
    monkeypatch.setattr(
        pipeline_runner,
        "logger",
        SimpleNamespace(info=lambda *_a, **_k: None, warning=lambda *_a, **_k: None),
    )

    result = await pipeline_runner.notify_completion(job_id)
    return result, triggers


@pytest.mark.asyncio
async def test_notify_rejected_done_is_suppressed(monkeypatch):
    result, triggers = await _run_notify(monkeypatch, "runner-rej00001", "rejected_done")

    assert result["status"] == "skipped"
    assert result["reason"] == "terminal status: rejected_done"
    assert triggers == []


@pytest.mark.asyncio
async def test_notify_cancelled_is_suppressed(monkeypatch):
    result, triggers = await _run_notify(monkeypatch, "runner-can00001", "cancelled")

    assert result["status"] == "skipped"
    assert result["reason"] == "terminal status: cancelled"
    assert triggers == []


# ── B. 큐 배달 직전 가드 ──────────────────────────────────────────────────


class _DeferredFakes:
    """_process_deferred_reactions_once 용 가짜 풀. 청구된 행 1개를 돌려준다."""

    def __init__(self, message: str, job_status, runner_job_id=None, raise_on_status=False):
        self.deferred_id = str(uuid.uuid4())
        self.message = message
        self.job_status = job_status
        self.runner_job_id = runner_job_id
        self.raise_on_status = raise_on_status
        self.executed: list[tuple[str, tuple]] = []
        self.status_queries = 0

    def pool(self):
        fakes = self

        class _Tx:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_a):
                return False

        class _Conn:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_a):
                return False

            def transaction(self):
                return _Tx()

            async def execute(self, query, *args):
                fakes.executed.append((query, args))

            async def fetch(self, query, *_args):
                return [
                    {
                        "id": fakes.deferred_id,
                        "session_id": str(uuid.uuid4()),
                        "system_message": fakes.message,
                        "ohvis_task_id": None,
                        "attempts": 1,
                        "runner_job_id": fakes.runner_job_id,
                    }
                ]

            async def fetchval(self, query, *_args):
                assert "FROM pipeline_jobs" in query
                fakes.status_queries += 1
                if fakes.raise_on_status:
                    raise RuntimeError("db down")
                return fakes.job_status

        class _Pool:
            def acquire(self):
                return _Conn()

        return _Pool()

    def skipped_stale_updates(self):
        return [a for q, a in self.executed if "'skipped_stale'" in q]


def _patch_dequeue(monkeypatch, fakes: _DeferredFakes, trigger_result):
    import app.services.chat_service as chat_service

    trigger = AsyncMock(return_value=trigger_result)
    monkeypatch.setattr(chat_service, "get_pool", lambda: fakes.pool())
    monkeypatch.setattr(chat_service, "_is_local_active_api_slot", lambda: True)
    monkeypatch.setattr(chat_service, "trigger_ai_reaction", trigger)
    return chat_service, trigger


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_status", ["done", "rejected_done", "cancelled"])
async def test_dequeue_skips_stale_approval_trigger(monkeypatch, terminal_status):
    job_id = "runner-stale001"
    fakes = _DeferredFakes(_approval_message(job_id), terminal_status, runner_job_id=job_id)
    chat_service, trigger = _patch_dequeue(monkeypatch, fakes, MagicMock())

    started = await chat_service._process_deferred_reactions_once()

    assert started == 0
    trigger.assert_not_awaited()
    updates = fakes.skipped_stale_updates()
    assert len(updates) == 1
    assert updates[0][0] == uuid.UUID(fakes.deferred_id)
    assert updates[0][2] == f"stale approval trigger: job status={terminal_status}"
    # 재시도 대상으로 남기지 않는다: pending 으로 되돌리는 UPDATE 가 없어야 한다.
    assert not [q for q, _ in fakes.executed if "SET status = 'pending'" in q]


@pytest.mark.asyncio
async def test_dequeue_falls_back_to_message_regex_for_legacy_rows(monkeypatch):
    fakes = _DeferredFakes(_approval_message("runner-legacy01"), "done", runner_job_id=None)
    chat_service, trigger = _patch_dequeue(monkeypatch, fakes, MagicMock())

    await chat_service._process_deferred_reactions_once()

    trigger.assert_not_awaited()
    assert len(fakes.skipped_stale_updates()) == 1


@pytest.mark.asyncio
async def test_dequeue_delivers_approval_trigger_when_job_awaiting_approval(monkeypatch):
    job_id = "runner-live0001"
    fakes = _DeferredFakes(_approval_message(job_id), "awaiting_approval", runner_job_id=job_id)
    chat_service, trigger = _patch_dequeue(monkeypatch, fakes, MagicMock())

    started = await chat_service._process_deferred_reactions_once()

    assert started == 1
    trigger.assert_awaited_once()
    assert fakes.skipped_stale_updates() == []


@pytest.mark.asyncio
async def test_dequeue_never_blocks_deploy_done_report_for_done_job(monkeypatch):
    job_id = "runner-deploy001"
    fakes = _DeferredFakes(_deploy_done_message(job_id), "done", runner_job_id=job_id)
    chat_service, trigger = _patch_dequeue(monkeypatch, fakes, MagicMock())

    started = await chat_service._process_deferred_reactions_once()

    assert started == 1
    trigger.assert_awaited_once()
    assert fakes.status_queries == 0
    assert fakes.skipped_stale_updates() == []


@pytest.mark.asyncio
async def test_dequeue_delivers_when_job_row_missing(monkeypatch):
    job_id = "runner-gone0001"
    fakes = _DeferredFakes(_approval_message(job_id), None, runner_job_id=job_id)
    chat_service, trigger = _patch_dequeue(monkeypatch, fakes, MagicMock())

    assert await chat_service._process_deferred_reactions_once() == 1
    trigger.assert_awaited_once()


@pytest.mark.asyncio
async def test_dequeue_delivers_when_status_lookup_fails(monkeypatch):
    job_id = "runner-err00001"
    fakes = _DeferredFakes(
        _approval_message(job_id), "done", runner_job_id=job_id, raise_on_status=True
    )
    chat_service, trigger = _patch_dequeue(monkeypatch, fakes, MagicMock())

    assert await chat_service._process_deferred_reactions_once() == 1
    trigger.assert_awaited_once()
    assert fakes.skipped_stale_updates() == []


# ── 배선 계약 ─────────────────────────────────────────────────────────────


def test_enqueue_extracts_first_runner_job_id():
    from app.services.chat_service import _extract_runner_job_id

    msg = "**Job**: runner-abc123_X-9 그리고 runner-second"
    assert _extract_runner_job_id(msg) == "runner-abc123_X-9"
    assert _extract_runner_job_id("job 없음") is None


def test_main_migrates_runner_job_id_column():
    from pathlib import Path

    main = (Path(__file__).resolve().parents[2] / "app/main.py").read_text(encoding="utf-8")
    assert "runner_job_id TEXT," in main
    assert "ADD COLUMN IF NOT EXISTS runner_job_id TEXT" in main
