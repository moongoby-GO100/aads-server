import pytest

from app.core.goal_work_hierarchy_policy import (
    FEATURE_FLAG_NAME,
    PARENT_TYPE,
    PolicyInput,
    PolicyInputError,
    WorkItemStatus,
    WorkItemType,
    goal_work_hierarchy_enabled,
    requires_mandatory_human,
    validate_policy_input,
)


def _input(**overrides):
    values = dict(
        tenant_id="tenant", project="AADS", workspace_kind="project",
        actor_session_id="session", actor_role_key="project_lead",
        target_type="task", target_id="target", action="execute", base_version=1,
        risk_factors=(), patch_hash="sha256:" + "a" * 64,
        environment="dev", policy_version="sha256:" + "b" * 64,
    )
    values.update(overrides)
    return PolicyInput(**values)


def test_feature_flag_is_disabled_by_default_and_fails_closed():
    assert goal_work_hierarchy_enabled({}) is False
    assert goal_work_hierarchy_enabled({FEATURE_FLAG_NAME: "garbage"}) is False
    assert goal_work_hierarchy_enabled({FEATURE_FLAG_NAME: "true"}) is True


def test_prd_statuses_and_parent_policy_are_canonical():
    assert {item.value for item in WorkItemStatus} == {
        "draft", "ready", "in_progress", "in_review", "completed", "blocked",
        "cancelled", "changes_requested",
    }
    assert PARENT_TYPE == {
        WorkItemType.EPIC: None,
        WorkItemType.STORY: WorkItemType.EPIC,
        WorkItemType.TASK: WorkItemType.STORY,
    }


def test_policy_input_validation_accepts_complete_input_and_rejects_unsafe_shape():
    validate_policy_input(_input())
    with pytest.raises(PolicyInputError, match="tenant_id"):
        validate_policy_input(_input(tenant_id=""))
    with pytest.raises(PolicyInputError, match="base_version"):
        validate_policy_input(_input(base_version=0))
    with pytest.raises(PolicyInputError, match="patch_hash"):
        validate_policy_input(_input(patch_hash="changed"))
    with pytest.raises(PolicyInputError, match="policy_version"):
        validate_policy_input(_input(policy_version="latest"))


def test_mandatory_human_boundary_cannot_be_auto_approved():
    assert requires_mandatory_human("production", ())
    assert requires_mandatory_human("dev", ("database_schema",))
    assert not requires_mandatory_human("staging", ("cross_project_coordination",))
