import logging

import pytest
from fastapi import HTTPException
from app.services import preflight_checker as checker
from app.api import directives


@pytest.mark.parametrize(
    "duplicate,dependency,expected",
    [(False, True, "PROCEED"), (False, False, "WAIT"), (True, True, "BLOCKED")],
)
def test_structlog_info_preserves_gate(monkeypatch, duplicate, dependency, expected):
    from structlog.testing import capture_logs

    monkeypatch.setattr(
        checker,
        "_list_files",
        lambda path: ["AADS-123.md"] if duplicate and path == checker._PENDING_DIR else [],
    )
    monkeypatch.setattr(checker, "_extract_task_id_from_file", lambda path: "AADS-123")
    monkeypatch.setattr(checker, "_check_done_folder", lambda task: dependency)
    with capture_logs() as captured:
        result = checker.run_preflight(task_id="AADS-123", depends_on="AADS-122")
    assert result["recommendation"] == expected
    assert any(
        event["event"] == "preflight_result"
        and event["task_id"] == "AADS-123"
        and event["recommendation"] == expected
        for event in captured
    )


@pytest.mark.asyncio
async def test_api_failure_keeps_500(monkeypatch):
    from structlog.testing import capture_logs

    def fail(**kwargs):
        raise RuntimeError("queue unavailable")

    monkeypatch.setattr(directives, "run_preflight", fail)
    with capture_logs() as captured:
        with pytest.raises(HTTPException) as failure:
            await directives.get_directive_preflight(task_id="AADS-123", depends_on=None)
    assert failure.value.status_code == 500
    assert any(event["event"] == "preflight_check_failed" for event in captured)


def test_standard_logging_keyword_reproduces_preflight_500():
    logger = logging.getLogger('preflight_incident_reproduction')
    previous = logger.level
    logger.setLevel(logging.INFO)
    try:
        with pytest.raises(TypeError, match='task_id'):
            logger.info('preflight_result', task_id='AADS-123')
    finally:
        logger.setLevel(previous)


def test_structlog_keeps_task_as_structured_field():
    from structlog.testing import capture_logs

    with capture_logs() as captured:
        checker.logger.info("preflight_result", task_id="AADS-123")
    assert len(captured) == 1
    assert captured[0]["event"] == "preflight_result"
    assert captured[0]["task_id"] == "AADS-123"
