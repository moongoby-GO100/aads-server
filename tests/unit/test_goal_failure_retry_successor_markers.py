"""find_valid_successor: RETRY_OF_LINK·AUTO_REWORK_OF·SUPERSEDES 후속을 승계로 인정한다."""
from __future__ import annotations

import asyncio

from app.services.goal_failure_retry import find_valid_successor

FAILED = "runner-4e3bab4f"
TENANT = "00000000-0000-0000-0000-000000000001"
MILESTONE = "76b1c939-05d2-4d15-a20b-42beb3b8157d"


class _Conn:
    def __init__(self, rows):
        self.rows = rows

    async def fetch(self, *_args):
        return self.rows


def _row(job_id, instruction, status="running", phase="running"):
    return {"job_id": job_id, "status": status, "phase": phase, "project": "AADS",
            "instruction": instruction}


def _find(*rows):
    return asyncio.run(find_valid_successor(_Conn(list(rows)), TENANT, MILESTONE, FAILED))


def test_auto_rework_of_running_successor_is_active():
    row = _row("runner-27e0fe43", f"AUTO_REWORK_OF: {FAILED}\nAUTO_REWORK_ROUND: 1/2\n본문")
    assert _find(row) == {"job_id": "runner-27e0fe43", "kind": "active"}


def test_supersedes_multiple_ids_active():
    row = _row("runner-27e0fe43", f"SUPERSEDES: runner-aaaaaaaa, {FAILED}\n본문")
    assert _find(row) == {"job_id": "runner-27e0fe43", "kind": "active"}


def test_supersedes_id_only_inside_parenthetical_is_not_successor():
    row = _row("runner-27e0fe43", f"SUPERSEDES: runner-aaaaaaaa (참고: {FAILED} 와 유사)\n본문")
    assert _find(row) is None


def test_auto_rework_of_other_job_with_body_mention_is_not_successor():
    row = _row("runner-27e0fe43", f"AUTO_REWORK_OF: runner-bbbbbbbb\n본문에서 {FAILED} 를 언급")
    assert _find(row) is None


def test_retry_of_link_active_regression():
    row = _row("runner-11111111", f"RETRY_OF_LINK: {FAILED}\n본문")
    assert _find(row) == {"job_id": "runner-11111111", "kind": "active"}


def test_retry_of_link_completed_regression():
    row = _row("runner-11111111", f"RETRY_OF_LINK: {FAILED}\n본문", status="done", phase="release_certified")
    assert _find(row) == {"job_id": "runner-11111111", "kind": "completed"}


def test_release_gate_withheld_done_is_still_active():
    row = _row("runner-11111111", f"AUTO_REWORK_OF: {FAILED}", status="done", phase="done")
    assert _find(row) == {"job_id": "runner-11111111", "kind": "active"}


def test_retry_of_link_failed_successor_is_invalid_regression():
    row = _row("runner-11111111", f"RETRY_OF_LINK: {FAILED}\n본문", status="error", phase="failed")
    assert _find(row) is None


def test_auto_rework_failed_successor_is_invalid():
    row = _row("runner-27e0fe43", f"AUTO_REWORK_OF: {FAILED}", status="error", phase="failed")
    assert _find(row) is None


def test_active_wins_over_completed():
    done = _row("runner-22222222", f"RETRY_OF_LINK: {FAILED}", status="done", phase="release_certified")
    active = _row("runner-27e0fe43", f"AUTO_REWORK_OF: {FAILED}")
    assert _find(done, active) == {"job_id": "runner-27e0fe43", "kind": "active"}
