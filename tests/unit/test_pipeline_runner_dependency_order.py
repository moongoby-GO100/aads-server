"""Pipeline Runner must never create a later→earlier milestone dependency edge."""

import asyncio

import pytest
from fastapi import HTTPException

from app.api.pipeline_runner import (
    _VALID_PROJECTS,
    BatchJobItem,
    _dependency_order_is_inverted,
    _find_active_file_conflict,
    _validate_batch_dependency_graph,
)


GOAL_ID = "11111111-1111-4111-8111-111111111111"
M1_ID = "22222222-2222-4222-8222-222222222221"
M7_ID = "22222222-2222-4222-8222-222222222227"
TENANT_ID = "33333333-3333-4333-8333-333333333333"


def _instruction(milestone_id: str, name: str) -> str:
    return (
        f"TASK_ID: AADS-AAG-{name}\n"
        f"GOAL_ID: {GOAL_ID}\n"
        f"MILESTONE_ID: {milestone_id}\n"
        "TARGET: app/api/pipeline_runner.py\n"
    )


class _ConflictConn:
    def __init__(self, existing_milestone: str, existing_sequence: int):
        self.existing_instruction = _instruction(existing_milestone, "EXISTING")
        self.orders = {
            M1_ID: 10,
            M7_ID: existing_sequence,
        }

    async def fetchrow(self, query, milestone_id, goal_id, tenant_id):
        assert "AND project" not in query
        assert goal_id == GOAL_ID
        assert tenant_id == TENANT_ID
        sequence = self.orders.get(milestone_id)
        if sequence is None:
            return None
        return {"goal_id": goal_id, "milestone_id": milestone_id, "sequence_order": sequence}

    async def fetch(self, query, *args):
        if "FROM pipeline_jobs" in query:
            return [{
                "job_id": "runner-v11-7",
                "instruction": self.existing_instruction,
                "status": "queued",
                "phase": "queued",
            }]
        if "FROM chat_workspace_change_ledger" in query:
            return []
        raise AssertionError(query)


def test_same_goal_later_parent_is_inverted():
    assert _dependency_order_is_inverted(
        (GOAL_ID, M1_ID, 10),
        (GOAL_ID, M7_ID, 70),
    )
    assert not _dependency_order_is_inverted(
        (GOAL_ID, M7_ID, 70),
        (GOAL_ID, M1_ID, 10),
    )


def test_file_conflict_reports_inversion_instead_of_assigning_later_parent():
    conn = _ConflictConn(M7_ID, 70)
    result = asyncio.run(_find_active_file_conflict(
        conn,
        project="AADS",
        target_files={"server:app/api/pipeline_runner.py"},
        tenant_id=TENANT_ID,
        incoming_instruction=_instruction(M1_ID, "V11-1-RECOVERY"),
    ))

    assert result["job_id"] == "runner-v11-7"
    assert result["dependency_inversion"] is True
    assert result["incoming_sequence"] == 10
    assert result["parent_sequence"] == 70


def test_batch_graph_rejects_missing_self_cycle_and_duplicate_keys():
    invalid_batches = [
        [BatchJobItem(key="A", instruction="x"), BatchJobItem(key="A", instruction="y")],
        [BatchJobItem(key="A", instruction="x", depends_on_key="B")],
        [BatchJobItem(key="A", instruction="x", depends_on_key="A")],
        [
            BatchJobItem(key="A", instruction="x", depends_on_key="B"),
            BatchJobItem(key="B", instruction="y", depends_on_key="A"),
        ],
    ]

    for jobs in invalid_batches:
        with pytest.raises(HTTPException):
            _validate_batch_dependency_graph(jobs)


def test_all_executable_projects_share_the_central_guard():
    assert {"AADS", "KIS", "GO100", "SF", "NTV2"} <= _VALID_PROJECTS
