import logging

import pytest
from fastapi import HTTPException
from app.services import preflight_checker as checker
from app.api import directives


@pytest.mark.parametrize(
    "duplicate,dependency,expected",
    [(False, True, "PROCEED"), (False, False, "WAIT"), (True, True, "BLOCKED")],
)
def test_info_logging_preserves_gate(monkeypatch, caplog, duplicate, dependency, expected):
    monkeypatch.setattr(
        checker,
        "_list_files",
        lambda path: ["AADS-123.md"] if duplicate and path == checker._PENDING_DIR else [],
    )
    monkeypatch.setattr(checker, "_extract_task_id_from_file", lambda path: "AADS-123")
    monkeypatch.setattr(checker, "_check_done_folder", lambda task: dependency)
    with caplog.at_level(logging.INFO):
        result = checker.run_preflight(task_id="AADS-123", depends_on="AADS-122")
    assert result["recommendation"] == expected
    assert any(
        record.message.startswith("preflight_result task_id=AADS-123")
        and record.recommendation == expected
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_api_failure_keeps_500(monkeypatch, caplog):
    def fail(**kwargs):
        raise RuntimeError("queue unavailable")

    monkeypatch.setattr(directives, "run_preflight", fail)
    with caplog.at_level(logging.ERROR), pytest.raises(HTTPException) as failure:
        await directives.get_directive_preflight(task_id="AADS-123", depends_on=None)
    assert failure.value.status_code == 500


def test_standard_logging_keyword_reproduces_preflight_500():
    logger = logging.getLogger('preflight_incident_reproduction')
    previous = logger.level
    logger.setLevel(logging.INFO)
    try:
        with pytest.raises(TypeError, match='task_id'):
            logger.info('preflight_result', task_id='AADS-123')
    finally:
        logger.setLevel(previous)


def test_current_formatter_keeps_task_in_message_without_extra():
    import io
    import structlog
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    handler.setFormatter(structlog.stdlib.ProcessorFormatter(
        processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                    structlog.processors.JSONRenderer()],
    ))
    record = logging.LogRecord('preflight', logging.INFO, __file__, 1,
                              'preflight_result task_id=%s', ('AADS-123',), None)
    record.task_id = 'AADS-123'
    handler.handle(record)
    import json
    rendered = json.loads(output.getvalue())
    assert rendered['event'] == 'preflight_result task_id=AADS-123'
    assert 'task_id' not in rendered
