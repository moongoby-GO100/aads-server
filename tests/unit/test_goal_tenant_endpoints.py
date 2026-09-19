from __future__ import annotations

import asyncio
import inspect

import pytest
from fastapi import HTTPException
from fastapi.params import Depends

from app.routers import goals


TENANT_ENDPOINTS = (
    "goal_board",
    "goal_documents",
    "add_goal_document",
    "goals_for_session",
    "goal_owner_candidates",
    "add_goal_owner",
    "get_goal_approval_policy",
    "set_goal_approval_policy",
    "set_goal_lead",
    "remove_goal_owner",
    "pause_owner",
    "resume_owner",
    "restart_owner",
    "goal_halt",
    "goal_direct",
    "confirm_milestone_api",
    "goal_rewind",
    "advance_active_goals",
    "update_task_status",
    "reconcile_goal_links",
    "reconcile_release_evidence",
)


@pytest.mark.parametrize("endpoint_name", TENANT_ENDPOINTS)
def test_all_21_tenant_endpoints_require_tenant_context(endpoint_name):
    endpoint = getattr(goals, endpoint_name)
    parameter = inspect.signature(endpoint).parameters.get("context")

    assert parameter is not None, f"{endpoint_name} has no tenant context"
    assert isinstance(parameter.default, Depends)
    assert parameter.default.dependency in {
        goals.require_tenant_viewer,
        goals.require_tenant_member,
    }


def test_tenant_helper_rejects_direct_call_depends_object():
    with pytest.raises(HTTPException) as exc:
        goals._tenant_id(inspect.signature(goals.restart_owner).parameters["context"].default)

    assert exc.value.status_code == 401
    assert exc.value.detail == "tenant_context_required"


def test_restart_owner_direct_call_without_context_fails_closed():
    with pytest.raises(HTTPException) as exc:
        asyncio.run(goals.restart_owner("goal-id", "session-id"))

    assert exc.value.status_code == 401
    assert exc.value.detail == "tenant_context_required"
