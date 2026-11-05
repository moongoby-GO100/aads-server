"""반영·배포 단계의 대상 러너 done 을 stale 로 오판하지 않는다 (AADS-DEFERRED-STALE-DONE-MISJUDGE).

메시지는 손으로 줄여 쓰지 않고 next_step_proposals.propose() 가 실제로 큐에 넣는 본문을 캡처한다.
propose() 는 제목 줄 앞에 "[시스템] 사전 승인 범위에 ..." 안내문을 붙인다.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.services import chat_service, next_step_proposals
from app.services.deferred_reaction_freshness import (
    RELEASE_STEP_KEYWORDS,
    annotate_or_skip,
    is_release_step,
    step_title,
)

SESSION = "765160d6-facb-4de4-8b36-8591b9466d68"
JOB = "runner-378f9071"


class _FakeConn:
    def __init__(self, rows):
        self.rows = rows

    async def fetch(self, *_a):
        return self.rows


def _job(job_id, status):
    return {
        "job_id": job_id,
        "status": status,
        "updated_at": datetime(2026, 11, 5, 9, 0, tzinfo=timezone.utc),
    }


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *_):
        return False


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _Acquire(self.conn)


async def _queued_message(monkeypatch, title: str, detail: str = "") -> str:
    """propose() 가 auto 큐에 실제로 넣는 본문을 돌려준다."""
    from app.core import db_pool

    class _Conn:
        async def fetchval(self, *_):
            return "11111111-1111-4111-8111-111111111111"

    monkeypatch.setattr(db_pool, "get_pool", lambda: _Pool(_Conn()))

    async def bubble(*_):
        return "a297f497-5a1e-4f0d-9c06-f536717d36d4"

    async def project(*_):
        return "AADS"

    async def grant(*_):
        return "grant-id"

    monkeypatch.setattr(next_step_proposals, "_current_bubble_id", bubble)
    monkeypatch.setattr(next_step_proposals, "_session_project", project)
    monkeypatch.setattr(next_step_proposals, "_covered_by_existing_grant", grant)
    captured: list[str] = []

    async def fake_enqueue(_sid, prompt, **_kw):
        captured.append(prompt)
        return {"queue_id": "q", "queue_status": "pending", "created": True}

    monkeypatch.setattr(chat_service, "enqueue_next_step_reaction", fake_enqueue)
    step = {"title": title, "detail": detail, "tool": "pipeline_runner_submit"}
    await next_step_proposals.propose(SESSION, [step])
    assert len(captured) == 1
    return captured[0]


@pytest.mark.asyncio
async def test_real_queue_message_has_system_preamble_before_title(monkeypatch):
    msg = await _queued_message(monkeypatch, f"{JOB} 운영 반영(bluegreen 배포)")

    assert msg.startswith("[시스템] 사전 승인 범위에 포함된 다음 단계입니다.")
    assert step_title(msg) == f"{JOB} 운영 반영(bluegreen 배포)"
    assert is_release_step(msg)


@pytest.mark.asyncio
async def test_done_release_step_is_delivered_not_skipped(monkeypatch):
    msg = await _queued_message(monkeypatch, f"{JOB} 운영 반영(bluegreen 배포)")

    out, reason = await annotate_or_skip(_FakeConn([_job(JOB, "done")]), msg)

    assert reason is None
    assert out.startswith(msg)
    assert f"- {JOB}: done" in out


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["error", "cancelled", "rejected_done"])
async def test_non_done_terminal_release_step_is_still_skipped(monkeypatch, status):
    msg = await _queued_message(monkeypatch, f"{JOB} 운영 반영(bluegreen 배포)")

    out, reason = await annotate_or_skip(_FakeConn([_job(JOB, status)]), msg)

    assert out == msg
    assert reason == f"stale_jobs: {JOB}={status}"


@pytest.mark.asyncio
async def test_done_resubmit_step_keeps_existing_skip(monkeypatch):
    msg = await _queued_message(monkeypatch, f"{JOB} 실패 분석 후 러너 재제출")

    assert not is_release_step(msg)
    out, reason = await annotate_or_skip(_FakeConn([_job(JOB, "done")]), msg)

    assert out == msg
    assert reason == f"stale_jobs: {JOB}=done"


@pytest.mark.asyncio
async def test_release_keyword_only_in_detail_does_not_count(monkeypatch):
    msg = await _queued_message(
        monkeypatch, f"{JOB} 재제출", detail="배포는 별도 담당이 진행한다"
    )

    assert not is_release_step(msg)
    _out, reason = await annotate_or_skip(_FakeConn([_job(JOB, "done")]), msg)
    assert reason == f"stale_jobs: {JOB}=done"


@pytest.mark.asyncio
async def test_release_step_with_mixed_done_and_error_is_skipped(monkeypatch):
    other = "runner-4a51ed6d"
    msg = await _queued_message(monkeypatch, f"{JOB}, {other} 배포")

    _out, reason = await annotate_or_skip(
        _FakeConn([_job(JOB, "done"), _job(other, "error")]), msg
    )

    assert reason == f"stale_jobs: {JOB}=done, {other}=error"


@pytest.mark.asyncio
async def test_release_step_with_live_job_still_delivered_with_block(monkeypatch):
    msg = await _queued_message(monkeypatch, f"{JOB} 배포")

    out, reason = await annotate_or_skip(_FakeConn([_job(JOB, "running")]), msg)

    assert reason is None
    assert "[상태 갱신" in out


def test_keywords_cover_directive_terms():
    for word in ("배포", "deploy", "bluegreen", "릴리스", "release", "반영", "push 된", "체리픽"):
        assert word in RELEASE_STEP_KEYWORDS


@pytest.mark.asyncio
async def test_queue_row_release_step_done_is_delivered(monkeypatch):
    """_process_deferred_reactions_once 경유: 실제 큐 본문 + done 이면 배달된다."""
    from tests.unit.test_deferred_reaction_stale_jobs import _patch, _QueueFakes

    msg = await _queued_message(monkeypatch, f"{JOB} 운영 반영(bluegreen 배포)")
    fakes = _QueueFakes(msg, "next_step:auto:s:k", [_job(JOB, "done")])
    chat_svc, trigger = _patch(monkeypatch, fakes)

    await chat_svc._process_deferred_reactions_once()

    assert fakes.skipped() == []
    assert trigger.await_args.args[1].startswith(msg)
