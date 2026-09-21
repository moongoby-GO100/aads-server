"""Runner terminal failures leave useful, project-scoped error-book candidates."""
from __future__ import annotations

import pytest


def _runner_failure_row(**overrides):
    row = {
        "job_id": "runner-3bf40e82",
        "project": "GO100",
        "status": "error",
        "phase": "error",
        "error_detail": "worktree file conflict forced terminal stop",
        "review_feedback": "review feedback " * 80,
    }
    row.update(overrides)
    return row


@pytest.mark.asyncio
async def test_runner_error_transition_records_project_candidate_once(monkeypatch):
    from app.api.pipeline_runner import _record_terminal_failure_candidate
    from app.services import error_book

    calls = []

    async def _no_match(_probe):
        return []

    async def _record(probe, source="", **kwargs):
        calls.append((probe, source, kwargs))
        return "auto.test"

    monkeypatch.setattr(error_book, "match_error", _no_match)
    monkeypatch.setattr(error_book, "record_candidate", _record)

    await _record_terminal_failure_candidate(_runner_failure_row())

    assert len(calls) == 1
    probe, source, kwargs = calls[0]
    assert "worktree file conflict" in probe
    assert "job_id: runner-3bf40e82" in probe
    assert source == "runner:GO100:error"
    assert kwargs["project"] == "GO100"
    assert kwargs["metadata"]["job_id"] == "runner-3bf40e82"


@pytest.mark.asyncio
async def test_known_runner_error_bumps_without_creating_candidate(monkeypatch):
    from app.api.pipeline_runner import _record_terminal_failure_candidate
    from app.services import error_book

    bumped = []

    async def _match(_probe):
        return [{"error_key": "runner.known"}]

    async def _bump(error_key):
        bumped.append(error_key)

    async def _unexpected_record(*_args, **_kwargs):
        raise AssertionError("known error must not create a candidate")

    monkeypatch.setattr(error_book, "match_error", _match)
    monkeypatch.setattr(error_book, "bump_recurrence", _bump)
    monkeypatch.setattr(error_book, "record_candidate", _unexpected_record)

    await _record_terminal_failure_candidate(_runner_failure_row(phase="review_failed"))

    assert bumped == ["runner.known"]


@pytest.mark.asyncio
async def test_review_hold_is_indexed_as_infrastructure_failure(monkeypatch):
    from app.api.pipeline_runner import _record_terminal_failure_candidate
    from app.services import error_book

    calls = []

    async def _no_match(_probe):
        return []

    async def _record(probe, source="", **kwargs):
        calls.append((probe, source, kwargs))
        return "auto.review"

    monkeypatch.setattr(error_book, "match_error", _no_match)
    monkeypatch.setattr(error_book, "record_candidate", _record)

    await _record_terminal_failure_candidate(
        _runner_failure_row(status="review_hold", phase="review_hold")
    )

    assert len(calls) == 1
    assert calls[0][1] == "runner:GO100:review_hold"


@pytest.mark.asyncio
async def test_html_error_page_probe_does_not_create_candidate(monkeypatch):
    from app.services import error_book

    class _Pool:
        async def execute(self, *_args, **_kwargs):
            raise AssertionError("noisy HTML probe must not reach the database")

    monkeypatch.setattr("app.core.db_pool.get_pool", lambda: _Pool())

    result = await error_book.record_candidate(
        "<!-- a padding to disable MSIE and Chrome friendly error page -->\n<html>"
    )

    assert result == ""


@pytest.mark.asyncio
async def test_candidate_record_exception_does_not_escape_runner_terminal_flow(monkeypatch):
    from app.api.pipeline_runner import _record_terminal_failure_candidate
    from app.services import error_book

    async def _no_match(_probe):
        return []

    async def _boom(*_args, **_kwargs):
        raise RuntimeError("error book unavailable")

    monkeypatch.setattr(error_book, "match_error", _no_match)
    monkeypatch.setattr(error_book, "record_candidate", _boom)

    await _record_terminal_failure_candidate(_runner_failure_row())
