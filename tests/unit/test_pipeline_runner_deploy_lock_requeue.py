"""AADS-RUNNER-DEPLOY-LOCK-QUEUE-P0: deploy lock 재시도 3회 소진 시 terminal error 로
끝내면 이미 CEO 승인된 diff 가 push 되지 못하고 폐기된다 (2026-09-17 실측,
runner-1ceec46f/3961f858). 상한(재큐잉 5회 또는 누적대기 15분) 전까지는
status=queued 로 재큐잉해야 한다.
"""
import asyncio

import pytest

from app.services import deploy_lock, pipeline_runner_service
from app.services.pipeline_runner_service import (
    PipelineCJob,
    _DEPLOY_LOCK_MAX_REQUEUE,
)


def _make_job() -> PipelineCJob:
    job = PipelineCJob(
        project="KIS",
        instruction="테스트 지시",
        chat_session_id="",
    )
    job.status = "awaiting_approval"
    return job


def _silence_side_effects(job, monkeypatch):
    async def _noop_save():
        return None

    async def _noop_chat(*_a, **_k):
        return None

    async def _noop_push(*_a, **_k):
        return None

    monkeypatch.setattr(job, "_save_to_db", _noop_save)
    monkeypatch.setattr(job, "_post_to_chat", _noop_chat)
    monkeypatch.setattr(job, "_notify_push_status", _noop_push)


def _patch_instant_sleep(monkeypatch):
    orig_sleep = asyncio.sleep

    async def _instant_sleep(*_a, **_k):
        return await orig_sleep(0)

    monkeypatch.setattr(pipeline_runner_service.asyncio, "sleep", _instant_sleep)


def test_deploy_lock_miss_requeues_instead_of_terminal_error(monkeypatch):
    """3회 즉시 재시도가 소진돼도, 상한 전이면 error 가 아니라 queued 로 남는다."""
    job = _make_job()
    _silence_side_effects(job, monkeypatch)
    _patch_instant_sleep(monkeypatch)

    calls = {"n": 0}

    def _fake_acquire(project, session_id, timeout=600):
        calls["n"] += 1
        # 첫 사이클(1회 선시도 + 3회 백오프 재시도 = 4회)은 실패, 재큐잉 이후 두 번째
        # 사이클의 첫 시도에서 락을 획득한다.
        if calls["n"] <= 4:
            return {"acquired": False, "holder": "other-job-1"}
        return {"acquired": True, "holder": None}

    monkeypatch.setattr(deploy_lock, "acquire_deploy_lock", _fake_acquire)

    # 락 획득 이후 코드는 실제 git push를 시도한다 — 여기서는 그 이후 흐름을 검증
    # 대상이 아니므로 예외를 던져 approve()가 push 실패 분기로 즉시 빠지게 한다.
    async def _boom_ssh(*_a, **_k):
        raise RuntimeError("ssh not available in unit test")

    monkeypatch.setattr(job, "_ssh_command", _boom_ssh)

    result = asyncio.run(job.approve())

    # 락 미획득 상한(5회) 안에서는 terminal error 로 끝나지 않는다 — 이후 push 실패로
    # error 가 나더라도 그 사유는 deploy_lock_fail 이 아니어야 한다.
    assert "deploy_lock_fail" not in (job.error_msg or "")
    assert "재큐잉 1/5" in (job.review_feedback or "")
    assert result.get("status") == "error"  # push 실패로 인한 종료 (락 실패 아님)
    assert calls["n"] == 5


def test_deploy_lock_miss_exceeds_cap_becomes_terminal_error(monkeypatch):
    """재큐잉 상한(5회)을 넘기면 그때서야 deploy_lock_fail terminal error 로 확정한다."""
    job = _make_job()
    _silence_side_effects(job, monkeypatch)
    _patch_instant_sleep(monkeypatch)

    calls = {"n": 0}

    def _always_fail_acquire(project, session_id, timeout=600):
        calls["n"] += 1
        return {"acquired": False, "holder": "other-job-2"}

    monkeypatch.setattr(deploy_lock, "acquire_deploy_lock", _always_fail_acquire)

    result = asyncio.run(job.approve())

    assert job.status == "error"
    assert job.phase == "deploy_lock_fail"
    assert "deploy_lock_fail" in (job.error_msg or "")
    assert "deploy_lock_fail" in result.get("error", "")
    # 1회 선시도 + (5회 재큐잉을 넘길 때까지) 6 사이클 * 3회 백오프 재시도
    assert calls["n"] == 1 + (_DEPLOY_LOCK_MAX_REQUEUE + 1) * 3
