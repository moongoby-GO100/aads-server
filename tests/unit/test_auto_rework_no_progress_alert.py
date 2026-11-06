"""진전 없는 자동 재작업의 실제 세션 보고 저장 회귀 검증."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

JOB = "runner-abcd1234"
SESSION = "11111111-1111-4111-8111-111111111111"


@pytest.fixture
def alert(monkeypatch):
    from app.api import pipeline_runner as api
    from app.core import db_pool
    from app.services import pipeline_auto_rework as rework
    from app.services import pipeline_runner_service as runner
    from app.services import session_reporter as reporter

    state = SimpleNamespace(
        session=SESSION, skipped="no_progress", messages={}, reports=[], warnings=[],
    )

    class Conn:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def transaction(self):
            return self

        async def fetchrow(self, query, *args):
            if "SELECT status FROM pipeline_jobs" in query:
                return {"status": "error"}
            if "SELECT job_id, project, status" in query:
                return dict(job_id=JOB, project="AADS", status="error", phase="review_failed",
                            chat_session_id=state.session, review_feedback=None,
                            error_detail="review_failed: verdict=REQUEST_CHANGES score=0.68",
                            output_preview="", instruction_preview="")
            if "INSERT INTO chat_messages" in query:
                key = args[5]
                if key in state.messages:
                    return None
                state.messages[key] = args[1]
                return {"id": "message-1"}
            raise AssertionError(query)

        async def fetchval(self, query, *_args):
            assert "FROM chat_sessions" in query
            return 1

        async def execute(self, query, *_args):
            assert "UPDATE chat_sessions" in query

    pool = SimpleNamespace(acquire=Conn)
    monkeypatch.setattr(db_pool, "get_pool", lambda: pool)
    monkeypatch.setattr(reporter, "get_pool", lambda: pool)
    monkeypatch.setattr(api, "_record_terminal_failure_candidate", AsyncMock())
    monkeypatch.setattr(api, "_cascade_cleanup_orphans_with_ids", AsyncMock(return_value=[]))
    monkeypatch.setattr(api, "promote_next_queued", AsyncMock(return_value=None))
    monkeypatch.setattr(api, "_enqueue_terminal_followup", AsyncMock(return_value=None))
    monkeypatch.setattr(runner, "_update_linked_goal_state_with_phase", AsyncMock())

    async def submit(*_args):
        return {"skipped": state.skipped, "parent": JOB, "reason": "same_issues"}

    original_report = reporter.post_session_report

    async def report(**kwargs):
        state.reports.append(kwargs)
        return await original_report(**kwargs)

    monkeypatch.setattr(rework, "maybe_submit_auto_rework", submit)
    monkeypatch.setattr(reporter, "post_session_report", report)
    monkeypatch.setattr(api, "logger", SimpleNamespace(
        info=lambda *_a, **_k: None,
        warning=lambda *a, **k: state.warnings.append((a, k)),
    ))
    state.notify = lambda: api.notify_completion(JOB)
    return state


@pytest.mark.asyncio
async def test_no_progress_posts_body_with_report_header(alert):
    await alert.notify()
    content = alert.messages[f"{JOB}:no_progress"]
    assert alert.reports[0]["body"] in content
    assert "[세션 자동보고]" in content
    assert "자동 재작업에 진전이 없어" in content
    assert "CEO 검토가 필요합니다" in content
    assert "same_issues" in content


@pytest.mark.asyncio
async def test_duplicate_notify_stores_only_one_alert(alert):
    await alert.notify()
    await alert.notify()
    assert list(alert.messages) == [f"{JOB}:no_progress"]
    assert alert.reports[0]["body"] in next(iter(alert.messages.values()))


@pytest.mark.asyncio
async def test_missing_session_skips_alert(alert):
    alert.session = None
    await alert.notify()
    assert alert.reports == []
    assert alert.messages == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("skipped", ["cap_reached", "opt_out", "already_submitted"])
async def test_other_skipped_values_do_not_alert(alert, skipped):
    alert.skipped = skipped
    await alert.notify()
    assert alert.reports == []


@pytest.mark.asyncio
async def test_report_exception_warns_and_preserves_terminal_result(alert, monkeypatch):
    from app.services import session_reporter

    monkeypatch.setattr(session_reporter, "post_session_report",
                        AsyncMock(side_effect=RuntimeError("report unavailable")))
    result = await alert.notify()
    assert result["status"] == "skipped"
    assert any(args[0] == "pipeline_runner.no_progress_alert_fail"
               for args, _kwargs in alert.warnings)
