"""명시 승인한 다음 단계는 종결 러너를 언급해도 배달한다."""
from unittest.mock import MagicMock

import pytest

from tests.unit.test_deferred_reaction_stale_jobs import _QueueFakes, _job, _patch
from tests.unit.test_stale_approval_trigger_guard import (
    _DeferredFakes,
    _approval_message,
    _patch_dequeue,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["done", "rejected_done"])
async def test_explicit_next_step_approval_is_delivered(monkeypatch, status):
    fakes = _QueueFakes(
        "[시스템] runner-4e2ea5f6 결과를 바탕으로 다음 단계를 실행하세요.",
        "next_step:approval:session:proposal",
        [_job("runner-4e2ea5f6", status)],
    )
    chat_service, trigger = _patch(monkeypatch, fakes)

    assert await chat_service._process_deferred_reactions_once() == 1

    trigger.assert_awaited_once()
    assert trigger.await_args.args[1] == fakes.message
    assert fakes.skipped() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["done", "rejected_done"])
async def test_automatic_trigger_without_dedupe_key_is_still_skipped(monkeypatch, status):
    job_id = "runner-4e2ea5f6"
    fakes = _DeferredFakes(_approval_message(job_id), status, runner_job_id=job_id)
    chat_service, trigger = _patch_dequeue(monkeypatch, fakes, MagicMock())

    assert await chat_service._process_deferred_reactions_once() == 0

    trigger.assert_not_awaited()
    updates = fakes.skipped_stale_updates()
    assert len(updates) == 1
    assert updates[0][2] == f"stale approval trigger: job status={status}"
