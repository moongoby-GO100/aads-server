"""Contracts for the OHVIS main chat: report envelope, grants, controls and the pure helpers around them.

Everything a client sends is an assertion. Tenant, destination session, route revision and every approval
are decided server side, so the request models forbid those fields instead of ignoring them.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,159}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
COMMIT = re.compile(r"^[0-9a-f]{7,40}$")
PROJECT = re.compile(r"^[A-Z0-9][A-Z0-9_-]{0,63}$")

SCHEMA_VERSIONS = (1,)
EVENT_TYPES = ("runner_completed", "runner_failed", "implementation_report", "report_only", "revision_report")
# ohvis_main_chat is deliberately absent: the main chat must never be able to react to its own output.
SOURCE_KINDS = ("runner", "agent")
IMPLEMENTATION_EVENT_TYPES = ("runner_completed", "implementation_report")

EVENT_STATES = ("received", "routed", "pending_review", "reviewing", "reviewed", "waiting_session", "waiting_route",
                "waiting_evidence", "quarantined", "dead_letter", "obsolete")
DECISIONS = ("verified", "needs_rework", "needs_decision", "unverifiable", "obsolete")
EFFECT_STATES = ("pending", "authorized", "dispatching", "confirmed", "verified", "unknown", "blocked", "failed")

# Grant types never promote into each other: only an execute_followup grant can authorize a follow-up job,
# and design/code-review approvals are mirrors that no action maps to.
GRANT_TYPES = ("design_approval", "code_review", "execute_followup", "push", "deploy")
CREATABLE_GRANT_TYPES = ("execute_followup", "push", "deploy")
ACTION_REQUIRES_GRANT = {"submit_followup_job": "execute_followup", "push": "push", "deploy": "deploy"}
# This unit can only dispatch a runner job. push/deploy stay modelled but have no adapter, so they never run.
SUPPORTED_ACTIONS = ("submit_followup_job",)

CARD_LABELS = {
    "report_arrived": "보고 도착", "reviewing": "검토 중", "verified": "검증 통과", "needs_rework": "수정 필요",
    "needs_decision": "결정 필요", "unverifiable": "확인 불가", "waiting_route": "보고방 지정 필요",
    "waiting_session": "대화 종료 후 검토", "obsolete": "최신 보고에 대체됨", "quarantined": "격리됨",
}


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def sha256_hex(value: Any) -> str:
    data = value if isinstance(value, bytes) else (value if isinstance(value, str) else canonical_json(value))
    return hashlib.sha256(data if isinstance(data, bytes) else data.encode("utf-8")).hexdigest()


def effect_key(tenant_id: str, project: str, root_task_id: str, action: str, target_hash: str,
               generation_id: str | None) -> str:
    """Semantic identity of one effect: re-delivery of the same report can never mint a second key."""
    return sha256_hex({"tenant": str(tenant_id), "project": project, "root_task": root_task_id, "action": action,
                       "target": target_hash, "generation": generation_id or ""})


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReportEvent(_Strict):
    """Structured report. No tenant, destination session or route revision field exists on purpose."""

    schema_version: int
    event_type: Literal[EVENT_TYPES]  # type: ignore[valid-type]
    source_kind: Literal[SOURCE_KINDS]  # type: ignore[valid-type]
    source_event_id: str
    source_revision: int = Field(ge=1)
    project_key: str
    goal_id: UUID | None = None
    root_task_id: str
    correlation_id: str
    causation_id: str | None = None
    runner_job_id: str | None = None
    source_session_id: UUID | None = None
    generation_id: str | None = None
    commit_sha: str | None = None
    diff_sha256: str | None = None
    artifact_refs: list[str] = Field(default_factory=list, max_length=50)
    summary: str = Field(default="", max_length=2000)
    occurred_at: datetime

    @field_validator("schema_version")
    @classmethod
    def known_schema(cls, value: int) -> int:
        if value not in SCHEMA_VERSIONS:
            raise ValueError("unsupported schema_version")
        return value

    @model_validator(mode="after")
    def valid(self):
        for name in ("source_event_id", "root_task_id", "correlation_id"):
            if not ID.fullmatch(getattr(self, name)):
                raise ValueError(f"invalid {name}")
        for name in ("causation_id", "runner_job_id", "generation_id"):
            value = getattr(self, name)
            if value is not None and not ID.fullmatch(value):
                raise ValueError(f"invalid {name}")
        self.project_key = self.project_key.upper()
        if not PROJECT.fullmatch(self.project_key):
            raise ValueError("invalid project_key")
        if self.commit_sha is not None and not COMMIT.fullmatch(self.commit_sha.lower()):
            raise ValueError("invalid commit_sha")
        if self.commit_sha:
            self.commit_sha = self.commit_sha.lower()
        if self.diff_sha256 is not None and not SHA256.fullmatch(self.diff_sha256):
            raise ValueError("invalid diff_sha256")
        for ref in self.artifact_refs:
            if len(ref) > 300 or "\x00" in ref:
                raise ValueError("invalid artifact ref")
        return self

    def payload_hash(self) -> str:
        """Digest of what the producer asserted; receipt time is not part of it, so redelivery hashes the same."""
        return sha256_hex(self.model_dump(mode="json"))


class RouteUpsert(_Strict):
    goal_id: UUID | None = None
    main_session_id: UUID
    accepts_goal_events: bool = False
    expected_revision: int = Field(ge=0)


class GrantScope(_Strict):
    """What a grant allows. job_spec is the exact job that may be submitted; target_hash is derived from it."""

    after_event_type: Literal[EVENT_TYPES]  # type: ignore[valid-type]
    job_spec: dict[str, Any]
    allowed_files: list[str] = Field(min_length=1, max_length=100)
    environment: Literal["repo_push_only"] = "repo_push_only"

    @field_validator("job_spec")
    @classmethod
    def spec_has_body(cls, value: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(value.get("instruction"), str) or not value["instruction"].strip():
            raise ValueError("job_spec.instruction is required")
        if len(canonical_json(value)) > 100_000:
            raise ValueError("job_spec too large")
        return value

    @field_validator("allowed_files")
    @classmethod
    def safe_files(cls, value: list[str]) -> list[str]:
        for path in value:
            if (not path or path.startswith("/") or ".." in path.split("/") or "\\" in path or len(path) > 300
                    or path.strip() in ("*", "**", ".")):
                raise ValueError("allowed_files must be explicit relative paths")
        return value


class GrantCreate(_Strict):
    grant_type: Literal[CREATABLE_GRANT_TYPES]  # type: ignore[valid-type]
    project_key: str
    goal_id: UUID | None = None
    root_task_id: str
    scope: GrantScope
    generation_id: str | None = None
    commit_sha: str | None = None
    expires_at: datetime
    approval_ref: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def valid(self):
        self.project_key = self.project_key.upper()
        if not PROJECT.fullmatch(self.project_key) or not ID.fullmatch(self.root_task_id):
            raise ValueError("invalid project_key or root_task_id")
        if self.commit_sha is not None:
            if not COMMIT.fullmatch(self.commit_sha.lower()):
                raise ValueError("invalid commit_sha")
            self.commit_sha = self.commit_sha.lower()
        if not self.commit_sha and not self.generation_id:
            raise ValueError("a grant must bind commit_sha or generation_id; null is not a wildcard")
        if self.generation_id is not None and not ID.fullmatch(self.generation_id):
            raise ValueError("invalid generation_id")
        return self

    def target_hash(self) -> str:
        return sha256_hex(self.scope.job_spec)


class GrantRevoke(_Strict):
    reason: str = Field(min_length=3, max_length=500)


class ControlRequest(_Strict):
    project_key: str
    action: Literal["pause", "resume"]
    expected_revision: int = Field(ge=0)
    reason: str = Field(default="", max_length=500)

    @field_validator("project_key")
    @classmethod
    def valid_project(cls, value: str) -> str:
        value = value.upper()
        if not PROJECT.fullmatch(value):
            raise ValueError("invalid project_key")
        return value
