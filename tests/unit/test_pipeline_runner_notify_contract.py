import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]


def _source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_awaiting_approval_notifies_session_ai_without_visible_runner_chat_dependency():
    source = _source("app/api/pipeline_runner.py")
    notify_endpoint = source.split("async def notify_completion", 1)[1]
    branch = notify_endpoint.split('if status == "awaiting_approval":', 1)[1].split(
        'elif status == "done":', 1
    )[0]

    assert "awaiting_approval already notified" in branch
    assert "msg = (" in branch
    assert "작업 패널의 diff·테스트·변경 파일·승인 메타데이터" in branch
    assert "notify_ai_suppressed" not in branch
    assert "chat trigger suppressed" not in branch


def test_runner_notify_endpoint_triggers_or_defers_ai_reaction():
    source = _source("app/api/pipeline_runner.py")
    endpoint = source.split("async def notify_completion", 1)[1].split(
        '@router.post("/pipeline/jobs/{job_id}/approve"', 1
    )[0]

    assert "await trigger_ai_reaction(session_id, msg" in endpoint
    assert 'return {"status": "triggered"' in endpoint


def test_chat_interrupt_and_bluegreen_drain_contracts_remain_enabled():
    chat_source = _source("app/services/chat_service.py")
    deploy_source = _source("deploy.sh")

    assert "_apply_deferred_interrupts_to_state" in chat_source
    assert "deferred_interrupt_apply" in chat_source
    assert 'AADS_EXECUTION_RESUME_MAX_ATTEMPTS", "5"' in chat_source
    assert 'deploy_phase_start "active_slot_drain" "running"' in deploy_source
    assert "while [[ $DRAIN_ELAPSED -lt 60 ]]" in deploy_source
    assert "sync_standby_slot_after_drain" in deploy_source


@pytest.mark.asyncio
async def test_awaiting_approval_dispatches_internal_ai_trigger(monkeypatch):
    from app.api import pipeline_runner
    import app.core.db_pool as db_pool
    import app.services.chat_service as chat_service
    import app.services.ohvis_task_manager as ohvis_task_manager

    session_id = "11111111-1111-4111-8111-111111111111"
    calls: list[tuple[str, str, str | None]] = []

    class _Acquire:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def fetchrow(self, query, *_args):
            if "SELECT status FROM pipeline_jobs" in query:
                return {"status": "awaiting_approval"}
            if "SELECT job_id, project, status" in query:
                return {
                    "job_id": "runner-abcd1234",
                    "project": "AADS",
                    "status": "awaiting_approval",
                    "phase": "awaiting_approval",
                    "chat_session_id": session_id,
                    "error_detail": None,
                    "output_preview": "17 tests passed",
                    "instruction_preview": "Fix runner notification continuity",
                }
            if "RETURNING job_id" in query:
                return {"job_id": "runner-abcd1234"}
            raise AssertionError(query)

    class _Pool:
        def acquire(self):
            return _Acquire()

    async def _fake_create_task(**_kwargs):
        return "ohvis-test-task"

    async def _fake_trigger(sid, message, ohvis_task_id=None):
        calls.append((sid, message, ohvis_task_id))

    monkeypatch.setattr(db_pool, "get_pool", lambda: _Pool())
    monkeypatch.setattr(ohvis_task_manager, "create_task", _fake_create_task)
    monkeypatch.setattr(chat_service, "trigger_ai_reaction", _fake_trigger)
    monkeypatch.setattr(
        pipeline_runner,
        "logger",
        SimpleNamespace(info=lambda *_args, **_kwargs: None, warning=lambda *_args, **_kwargs: None),
    )

    result = await pipeline_runner.notify_completion("runner-abcd1234")
    await asyncio.sleep(0)

    assert result["status"] == "triggered"
    assert len(calls) == 1
    assert calls[0][0] == session_id
    assert calls[0][2] == "ohvis-test-task"
    assert "AI 검수 대기 상태" in calls[0][1]
    assert "작업 패널의 diff·테스트·변경 파일·승인 메타데이터" in calls[0][1]
