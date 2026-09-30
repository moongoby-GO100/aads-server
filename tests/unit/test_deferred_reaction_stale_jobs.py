"""지연 배달 직전 러너 job 상태 재검증 (deferred_reaction_freshness)."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.deferred_reaction_freshness import annotate_or_skip, extract_job_ids

SESSION_ID = "33333333-3333-4333-8333-333333333333"


class _FakeConn:
    def __init__(self, rows=None, error=None):
        self.rows = rows or []
        self.error = error
        self.queries: list[str] = []

    async def fetch(self, query, *_args):
        self.queries.append(query)
        if self.error:
            raise self.error
        return self.rows


def _job(job_id, status, updated_at=None):
    return {
        "job_id": job_id,
        "status": status,
        "updated_at": updated_at or datetime(2026, 9, 30, 18, 19, tzinfo=timezone.utc),
    }


def test_extract_job_ids_dedupes_in_order():
    msg = "runner-b67a8cf5 와 runner-6820287d 승인, 다시 runner-b67a8cf5 확인"
    assert extract_job_ids(msg) == ["runner-b67a8cf5", "runner-6820287d"]


@pytest.mark.asyncio
async def test_all_terminal_returns_reason_and_unchanged_message():
    msg = "runner-b67a8cf5·runner-6820287d 승인"
    conn = _FakeConn([_job("runner-b67a8cf5", "rejected_done"), _job("runner-6820287d", "done")])

    out, reason = await annotate_or_skip(conn, msg)

    assert out == msg
    assert reason == "stale_jobs: runner-b67a8cf5=rejected_done, runner-6820287d=done"
    assert len(conn.queries) == 1
    assert "ANY($1::text[])" in conn.queries[0]


@pytest.mark.asyncio
async def test_partial_terminal_appends_status_block():
    msg = "runner-b67a8cf5·runner-6820287d 승인"
    conn = _FakeConn(
        [_job("runner-b67a8cf5", "rejected_done"), _job("runner-6820287d", "awaiting_approval")]
    )

    out, reason = await annotate_or_skip(conn, msg)

    assert reason is None
    assert out.startswith(msg)
    assert "\n\n[상태 갱신 — 배달 시각 기준]\n" in out
    # 2026-09-30 18:19 UTC == 10-01 03:19 KST
    assert "- runner-b67a8cf5: rejected_done (10-01 03:19 KST)" in out
    assert "- runner-6820287d: awaiting_approval (10-01 03:19 KST)" in out


@pytest.mark.asyncio
async def test_missing_job_is_unknown_and_not_terminal():
    msg = "runner-aaaaaa01 승인"
    out, reason = await annotate_or_skip(_FakeConn([]), msg)

    assert reason is None
    assert "- runner-aaaaaa01: unknown" in out


@pytest.mark.asyncio
async def test_no_job_id_passes_through_without_query():
    conn = _FakeConn()
    msg = "[시스템] 다음 단계를 수행하세요."

    assert await annotate_or_skip(conn, msg) == (msg, None)
    assert conn.queries == []


@pytest.mark.asyncio
async def test_query_failure_passes_through_without_raising():
    msg = "runner-b67a8cf5 승인"
    out, reason = await annotate_or_skip(_FakeConn(error=RuntimeError("db down")), msg)

    assert (out, reason) == (msg, None)


# ── 호출 지점: _process_deferred_reactions_once ────────────────────────────


class _QueueFakes:
    def __init__(self, message, dedupe_key, job_rows):
        self.deferred_id = str(uuid.uuid4())
        self.message = message
        self.dedupe_key = dedupe_key
        self.job_rows = job_rows
        self.executed: list[tuple[str, tuple]] = []

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
                if "FROM pipeline_jobs" in query:
                    return fakes.job_rows
                return [
                    {
                        "id": fakes.deferred_id,
                        "session_id": str(uuid.uuid4()),
                        "system_message": fakes.message,
                        "ohvis_task_id": None,
                        "attempts": 1,
                        "runner_job_id": None,
                        "dedupe_key": fakes.dedupe_key,
                    }
                ]

            async def fetchval(self, *_a):
                return None

        class _Pool:
            def acquire(self):
                return _Conn()

        return _Pool()

    def skipped(self):
        return [a for q, a in self.executed if "'skipped_stale'" in q]


def _patch(monkeypatch, fakes):
    import app.services.chat_service as chat_service

    trigger = AsyncMock(return_value=MagicMock())
    monkeypatch.setattr(chat_service, "get_pool", lambda: fakes.pool())
    monkeypatch.setattr(chat_service, "_is_local_active_api_slot", lambda: True)
    monkeypatch.setattr(chat_service, "trigger_ai_reaction", trigger)
    return chat_service, trigger


@pytest.mark.asyncio
async def test_next_step_row_with_all_terminal_jobs_is_skipped(monkeypatch):
    fakes = _QueueFakes(
        "[시스템] runner-70643263 승인",
        "next_step:approval:s:k",
        [_job("runner-70643263", "rejected_done")],
    )
    chat_service, trigger = _patch(monkeypatch, fakes)

    assert await chat_service._process_deferred_reactions_once() == 0

    trigger.assert_not_awaited()
    updates = fakes.skipped()
    assert len(updates) == 1
    assert updates[0][2] == "stale_jobs: runner-70643263=rejected_done"


@pytest.mark.asyncio
async def test_next_step_row_with_live_job_is_delivered_with_status_block(monkeypatch):
    fakes = _QueueFakes(
        "[시스템] runner-70643263 승인",
        "next_step:auto:s:k",
        [_job("runner-70643263", "awaiting_approval")],
    )
    chat_service, trigger = _patch(monkeypatch, fakes)

    await chat_service._process_deferred_reactions_once()

    assert fakes.skipped() == []
    delivered = trigger.await_args.args[1]
    assert "[상태 갱신" in delivered and "awaiting_approval" in delivered


@pytest.mark.asyncio
async def test_non_next_step_row_is_not_checked(monkeypatch):
    fakes = _QueueFakes(
        "[시스템] Pipeline Runner 작업 배포 완료 runner-70643263",
        None,
        [_job("runner-70643263", "done")],
    )
    chat_service, trigger = _patch(monkeypatch, fakes)

    await chat_service._process_deferred_reactions_once()

    assert fakes.skipped() == []
    assert trigger.await_args.args[1] == fakes.message
