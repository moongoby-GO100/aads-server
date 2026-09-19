"""M12 work-hierarchy constants and fail-closed policy input validation.

This module has no router or database side effects. M13/M14 may import it when
the corresponding APIs and policy evaluator are introduced.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Collection, Mapping


FEATURE_FLAG_NAME = "GOAL_WORK_HIERARCHY_ENABLED"
_SHA256_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")


class WorkItemType(StrEnum):
    EPIC = "epic"
    STORY = "story"
    TASK = "task"


class WorkItemStatus(StrEnum):
    DRAFT = "draft"
    READY = "ready"
    IN_PROGRESS = "in_progress"
    IN_REVIEW = "in_review"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"
    CHANGES_REQUESTED = "changes_requested"


class PolicyDecision(StrEnum):
    AUTO = "AUTO"
    NOTIFY = "NOTIFY"
    PROJECT_APPROVAL = "PROJECT_APPROVAL"
    CEO_APPROVAL = "CEO_APPROVAL"
    DENY = "DENY"


PARENT_TYPE: Mapping[WorkItemType, WorkItemType | None] = {
    WorkItemType.EPIC: None,
    WorkItemType.STORY: WorkItemType.EPIC,
    WorkItemType.TASK: WorkItemType.STORY,
}

MANDATORY_HUMAN_RISK_FACTORS = frozenset(
    {
        "production_deploy",
        "routing_cutover",
        "database_schema",
        "bulk_data_change",
        "financial",
        "secret",
        "security_policy",
        "destructive",
        "cross_project_execution",
    }
)


def goal_work_hierarchy_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Return the feature state; absent and malformed values fail closed."""
    source = os.environ if environ is None else environ
    return source.get(FEATURE_FLAG_NAME, "false").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class PolicyInput:
    tenant_id: str
    project: str
    workspace_kind: str
    actor_session_id: str
    actor_role_key: str
    target_type: str
    target_id: str
    action: str
    base_version: int
    risk_factors: Collection[str]
    patch_hash: str
    environment: str
    policy_version: str


class PolicyInputError(ValueError):
    """Raised when an M13/M14 policy input is incomplete or unsafe."""


def validate_policy_input(value: PolicyInput) -> None:
    """Validate the stable PRD 4.2 boundary without making a decision."""
    required = {
        "tenant_id": value.tenant_id,
        "project": value.project,
        "actor_session_id": value.actor_session_id,
        "actor_role_key": value.actor_role_key,
        "target_id": value.target_id,
        "patch_hash": value.patch_hash,
        "policy_version": value.policy_version,
    }
    missing = sorted(name for name, item in required.items() if not str(item).strip())
    if missing:
        raise PolicyInputError(f"missing policy fields: {', '.join(missing)}")
    if value.workspace_kind not in {"project", "ceo_integrated"}:
        raise PolicyInputError("invalid workspace_kind")
    if value.target_type not in {"goal", "milestone", "epic", "story", "task"}:
        raise PolicyInputError("invalid target_type")
    if value.action not in {"create", "update", "assign", "cancel", "execute", "accept"}:
        raise PolicyInputError("invalid action")
    if value.base_version < 1:
        raise PolicyInputError("base_version must be positive")
    if value.environment not in {"dev", "staging", "production"}:
        raise PolicyInputError("invalid environment")
    if not _SHA256_PATTERN.fullmatch(value.patch_hash):
        raise PolicyInputError("patch_hash must be canonical sha256")
    if not _SHA256_PATTERN.fullmatch(value.policy_version):
        raise PolicyInputError("policy_version must be canonical sha256")


def requires_mandatory_human(environment: str, risk_factors: Collection[str]) -> bool:
    """Return the non-overridable automatic-approval boundary from PRD 4.7."""
    return environment == "production" or bool(MANDATORY_HUMAN_RISK_FACTORS.intersection(risk_factors))
