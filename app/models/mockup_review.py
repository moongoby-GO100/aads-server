"""Request contracts for mockup reviews. Hashes in requests are assertions; the server recomputes them."""
from __future__ import annotations

import re
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
VIEWPORT = re.compile(r"^(desktop|mobile|tablet|[0-9]{3,4}x[0-9]{3,4})$")
KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,63}$")

ChangeType = Literal["new", "modify", "none"]
Phase = Literal["before", "mockup", "implemented"]
STATES = ("default", "loading", "empty", "error", "permission", "session-expired", "offline")

MAX_SCREENS = 50
MAX_ASSETS = 500
MAX_EVIDENCE = 200


def _id(value: str, what: str) -> str:
    if not ID.fullmatch(value):
        raise ValueError(f"invalid {what}")
    return value


class IdempotentWrite(BaseModel):
    idempotency_key: str
    expected_generation: int = Field(ge=0)

    @field_validator("idempotency_key")
    @classmethod
    def valid_key(cls, value: str) -> str:
        if not KEY.fullmatch(value):
            raise ValueError("idempotency_key must be 8-64 chars of [A-Za-z0-9._:-]")
        return value


class ScreenInput(BaseModel):
    screen_id: str
    title: str = Field(min_length=1, max_length=200)
    route: str = Field(min_length=1, max_length=300)
    requirement_ids: list[str] = Field(min_length=1, max_length=50)
    states_not_applicable: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def valid(self):
        _id(self.screen_id, "screen_id")
        for requirement in self.requirement_ids:
            _id(requirement, "requirement_id")
        for state, reason in self.states_not_applicable.items():
            if state not in STATES or state == "default" or len(reason.strip()) < 5:
                raise ValueError("states_not_applicable needs a non-default state and a reason of 5+ chars")
        return self


class AssetInput(BaseModel):
    asset_id: str
    role: Literal["primary", "child"] = "primary"
    uri: str = Field(min_length=1, max_length=512)
    sha256: str
    byte_size: int = Field(ge=1)
    mime: str = Field(max_length=100)
    screen_id: str | None = None
    phase: Phase | None = None
    viewport: str | None = None
    fixture_id: str | None = Field(default=None, max_length=128)
    state: str | None = None
    captured_at: datetime | None = None
    capture_source: str | None = Field(default=None, max_length=100)
    redaction_status: Literal["not_required", "masked", "pending"] = "not_required"

    @model_validator(mode="after")
    def valid(self):
        _id(self.asset_id, "asset_id")
        if not SHA256.fullmatch(self.sha256):
            raise ValueError("sha256 must be 64 lowercase hex chars")
        if self.role == "primary":
            missing = [name for name in ("screen_id", "phase", "viewport", "fixture_id", "state",
                                         "captured_at", "capture_source") if getattr(self, name) is None]
            if missing:
                raise ValueError("primary asset requires " + ", ".join(missing))
            if not VIEWPORT.fullmatch(self.viewport) or self.state not in STATES:
                raise ValueError("invalid viewport or state")
            _id(self.screen_id, "screen_id")
            if self.captured_at.tzinfo is None:
                raise ValueError("captured_at must carry a timezone")
        return self


class EvidenceInput(BaseModel):
    evidence_id: str
    kind: Literal["browser_capture", "snapshot", "http_fallback", "api_health", "process_check"]
    screen_id: str | None = None
    route: str = Field(min_length=1, max_length=300)
    success: bool
    login_used: bool = False
    detail: str = Field(default="", max_length=500)
    recorded_at: datetime

    @model_validator(mode="after")
    def valid(self):
        _id(self.evidence_id, "evidence_id")
        if self.recorded_at.tzinfo is None:
            raise ValueError("recorded_at must carry a timezone")
        return self


class ManifestInput(BaseModel):
    screens: list[ScreenInput] = Field(default_factory=list, max_length=MAX_SCREENS)
    assets: list[AssetInput] = Field(default_factory=list, max_length=MAX_ASSETS)
    evidence: list[EvidenceInput] = Field(default_factory=list, max_length=MAX_EVIDENCE)
    design_tokens_version: str | None = Field(default=None, max_length=64)
    source_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{7,64}$")
    backend_only_rationale: str | None = Field(default=None, max_length=1000)


class DocRefInput(BaseModel):
    role: Literal["plan", "prd", "spec"]
    document_key: str | None = None
    revision_id: UUID | None = None
    content_hash: str | None = None
    exempt_reason: str | None = Field(default=None, max_length=500)
    exempt_policy_ref: str | None = Field(default=None, max_length=300)

    @model_validator(mode="after")
    def valid(self):
        exempt = bool(self.exempt_reason or self.exempt_policy_ref)
        if exempt:
            if self.document_key or self.revision_id or self.content_hash:
                raise ValueError("an exempt document reference cannot also name a revision")
            if len((self.exempt_reason or "").strip()) < 10 or not (self.exempt_policy_ref or "").strip():
                raise ValueError("exemption needs a reason of 10+ chars and an approved policy reference")
        else:
            if not (self.document_key and self.revision_id and self.content_hash):
                raise ValueError("document_key, revision_id and content_hash are required")
            _id(self.document_key, "document_key")
            if not SHA256.fullmatch(self.content_hash):
                raise ValueError("content_hash must be 64 lowercase hex chars")
        return self


class ReviewCreate(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    change_type: ChangeType
    goal_id: UUID | None = None
    session_id: UUID | None = None
    idempotency_key: str

    @field_validator("idempotency_key")
    @classmethod
    def valid_key(cls, value: str) -> str:
        if not KEY.fullmatch(value):
            raise ValueError("idempotency_key must be 8-64 chars of [A-Za-z0-9._:-]")
        return value


class Resolution(BaseModel):
    change_request_id: UUID
    outcome: Literal["applied", "not_applied"]
    reason: str = Field(min_length=3, max_length=1000)


class RevisionCreate(IdempotentWrite):
    manifest: ManifestInput
    doc_refs: list[DocRefInput] = Field(default_factory=list, max_length=3)
    parent_revision_id: UUID | None = None
    manifest_hash: str | None = None
    resolves: list[Resolution] = Field(default_factory=list, max_length=100)
    archive_if_stale: bool = False

    @model_validator(mode="after")
    def valid(self):
        if self.manifest_hash is not None and not SHA256.fullmatch(self.manifest_hash):
            raise ValueError("manifest_hash must be 64 lowercase hex chars")
        roles = [ref.role for ref in self.doc_refs]
        if len(roles) != len(set(roles)):
            raise ValueError("duplicate document role")
        ids = [str(item.change_request_id) for item in self.resolves]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate change_request_id in resolves")
        return self


class RevisingStart(IdempotentWrite):
    pass


class SubmitReview(IdempotentWrite):
    revision_id: UUID


class ChangeCreate(IdempotentWrite):
    change_request_id: UUID
    base_revision_id: UUID
    source_message_id: UUID
    comment: str = Field(max_length=4000)
    screen_id: str | None = None

    @field_validator("screen_id")
    @classmethod
    def valid_screen(cls, value: str | None) -> str | None:
        return _id(value, "screen_id") if value is not None else None


class ApproveReview(IdempotentWrite):
    revision_id: UUID
    manifest_hash: str
    confirm: Literal[True]

    @field_validator("manifest_hash")
    @classmethod
    def valid_hash(cls, value: str) -> str:
        if not SHA256.fullmatch(value):
            raise ValueError("manifest_hash must be 64 lowercase hex chars")
        return value


class RevokeReview(IdempotentWrite):
    approval_id: int = Field(ge=1)
    reason: str = Field(min_length=3, max_length=1000)


class VerifyBundle(BaseModel):
    task_id: str = Field(min_length=1, max_length=200)
    revision_id: UUID
    manifest_hash: str
    phase: Literal["submit", "pre_execution", "checkpoint"] = "pre_execution"

    @field_validator("manifest_hash")
    @classmethod
    def valid_hash(cls, value: str) -> str:
        if not SHA256.fullmatch(value):
            raise ValueError("manifest_hash must be 64 lowercase hex chars")
        return value
