"""AAG graph snapshot ingestion and read-only query API."""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field

from app.core.db_pool import get_pool
from app.auth import get_current_user
from app.services.aag_governance import (
    AAGAuthorizationError,
    AAGWorkflowError,
    approve_override,
    authenticate_scanner,
    create_exception,
    decide_exception,
    normalize_project,
    require_project_role,
    request_override,
)
from app.services.aag_ingest_v2 import IngestConflictError, ingest_graph
from app.services.aag_tools import filter_findings, stale_minutes
from tools.aag.v2_contract import CANONICALIZATION_VERSION, STABLE_KEY_VERSION

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/aag", tags=["aag"])

_AAG_SNAPSHOT_DDL = """
CREATE TABLE IF NOT EXISTS aag_graph_snapshots (
    id BIGSERIAL PRIMARY KEY, project TEXT NOT NULL, host TEXT NOT NULL,
    generated_at TIMESTAMPTZ NOT NULL, commit_sha TEXT,
    stats JSONB NOT NULL DEFAULT '{}'::jsonb,
    findings JSONB NOT NULL DEFAULT '[]'::jsonb,
    unresolved JSONB NOT NULL DEFAULT '[]'::jsonb,
    node_count INTEGER NOT NULL DEFAULT 0, edge_count INTEGER NOT NULL DEFAULT 0,
    finding_count INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (project, generated_at)
);
CREATE INDEX IF NOT EXISTS idx_aag_graph_snapshots_project_created_at
    ON aag_graph_snapshots (project, created_at DESC);
"""
_table_ready = False


class SnapshotIn(BaseModel):
    project: str = Field(min_length=1, max_length=100)
    host: str = Field(min_length=1, max_length=255)
    generated_at: datetime
    stats: dict[str, Any] = Field(default_factory=dict)
    findings: list[dict[str, Any]] = Field(default_factory=list)
    unresolved: list[dict[str, Any]] = Field(default_factory=list)
    node_count: int = Field(default=0, ge=0)
    edge_count: int = Field(default=0, ge=0)
    commit_sha: str | None = None


class SnapshotV2In(BaseModel):
    run_id: UUID | None = None
    project: str = Field(min_length=1, max_length=100)
    repository_id: str = Field(min_length=1, max_length=255)
    target_ref: str = Field(min_length=1, max_length=255)
    governance_scope: str = Field(default="default", min_length=1, max_length=255)
    resolved_commit_sha: str = Field(min_length=7, max_length=64)
    expected_target_ref_head_sha: str = Field(min_length=7, max_length=64)
    host: str = Field(min_length=1, max_length=255)
    generated_at: datetime
    scanner_version: str = Field(min_length=1, max_length=100)
    ruleset_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    scan_scope_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    parser_versions: dict[str, Any] = Field(default_factory=dict)
    worktree_digest: str | None = Field(default=None, max_length=128)
    canonicalization_version: str = Field(default=CANONICALIZATION_VERSION, min_length=1, max_length=100)
    stable_key_version: str = Field(default=STABLE_KEY_VERSION, min_length=1, max_length=100)
    stats: dict[str, Any] = Field(default_factory=dict)
    nodes: list[dict[str, Any]] = Field(default_factory=list)
    edges: list[dict[str, Any]] = Field(default_factory=list)
    findings: list[dict[str, Any]] = Field(default_factory=list)
    unresolved: list[dict[str, Any]] = Field(default_factory=list)


class ExceptionRequest(BaseModel):
    project: str = Field(min_length=1, max_length=100)
    stable_finding_key: str = Field(min_length=1, max_length=500)
    reason: str = Field(min_length=3, max_length=2000)
    expires_at: datetime


class DecisionRequest(BaseModel):
    approve: bool
    reason: str = Field(min_length=3, max_length=2000)


class OverrideApprovalRequest(BaseModel):
    project: str = Field(min_length=1, max_length=100)
    verifier_id: str = Field(min_length=1, max_length=255)
    verification: dict[str, Any]
    reason: str = Field(min_length=3, max_length=2000)


class OverrideRequest(BaseModel):
    request_id: UUID
    project: str = Field(min_length=1, max_length=100)
    repository_id: str = Field(min_length=1, max_length=255)
    target_ref: str = Field(min_length=1, max_length=255)
    commit_sha: str = Field(min_length=7, max_length=64)
    reason: str = Field(min_length=3, max_length=2000)
    expires_at: datetime
    previous_healthy_snapshot_id: UUID
    fallback_snapshot_id: UUID | None = None
    rollback_plan: dict[str, Any]
    replay_nonce: str = Field(min_length=16, max_length=500)


def _actor(user: dict[str, Any]) -> str:
    return str(user.get("user_id") or "")


def _governance_error(exc: Exception) -> HTTPException:
    if isinstance(exc, AAGAuthorizationError):
        return HTTPException(status_code=403, detail=str(exc))
    return HTTPException(status_code=409, detail=str(exc))


def _require_v2_enabled() -> None:
    if os.getenv("AAG_V2_ENABLED", "0").strip().lower() not in {"1", "true", "yes", "on"}:
        raise HTTPException(status_code=404, detail="AAG v2 is disabled")


async def _ensure_table() -> None:
    global _table_ready
    if _table_ready:
        return
    async with get_pool().acquire() as conn:
        await conn.execute(_AAG_SNAPSHOT_DDL)
    _table_ready = True


@router.post("/snapshot")
async def store_snapshot(body: SnapshotIn) -> dict[str, Any]:
    await _ensure_table()
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO aag_graph_snapshots
                   (project, host, generated_at, commit_sha, stats, findings, unresolved,
                    node_count, edge_count, finding_count)
                 VALUES ($1,$2,$3,$4,$5::jsonb,$6::jsonb,$7::jsonb,$8,$9,$10)
                 ON CONFLICT (project, generated_at) DO UPDATE SET
                   host=EXCLUDED.host, commit_sha=EXCLUDED.commit_sha, stats=EXCLUDED.stats,
                   findings=EXCLUDED.findings, unresolved=EXCLUDED.unresolved,
                   node_count=EXCLUDED.node_count, edge_count=EXCLUDED.edge_count,
                   finding_count=EXCLUDED.finding_count
                 RETURNING id""",
            body.project.upper(), body.host, body.generated_at, body.commit_sha,
            json.dumps(body.stats), json.dumps(body.findings), json.dumps(body.unresolved),
            body.node_count, body.edge_count, len(body.findings),
        )
    return {"ok": True, "id": row["id"]}


@router.post("/v2/snapshots", status_code=201)
async def store_snapshot_v2(
    body: SnapshotV2In,
    x_aag_scanner_token: str | None = Header(default=None, alias="X-AAG-Scanner-Token"),
) -> dict[str, Any]:
    """Publish immutable graph content and record a ref-specific observation."""
    _require_v2_enabled()
    try:
        await authenticate_scanner(x_aag_scanner_token or "", body.project)
        result = await ingest_graph(
            project=body.project,
            repository_id=body.repository_id,
            target_ref=body.target_ref,
            governance_scope=body.governance_scope,
            resolved_commit_sha=body.resolved_commit_sha,
            expected_target_ref_head_sha=body.expected_target_ref_head_sha,
            host=body.host,
            generated_at=body.generated_at,
            scanner_version=body.scanner_version,
            ruleset_digest=body.ruleset_digest,
            scan_scope_digest=body.scan_scope_digest,
            parser_versions=body.parser_versions,
            worktree_digest=body.worktree_digest,
            canonicalization_version=body.canonicalization_version,
            stable_key_version=body.stable_key_version,
            run_id=body.run_id,
            graph={
                "stats": body.stats,
                "nodes": body.nodes,
                "edges": body.edges,
                "findings": body.findings,
                "unresolved": body.unresolved,
            },
        )
    except AAGAuthorizationError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except IngestConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("AAG v2 snapshot ingest failed")
        raise HTTPException(status_code=500, detail="AAG v2 snapshot ingest failed") from exc
    return {
        "ok": True,
        "schema_version": "aag-v2",
        "run_id": str(result.run_id),
        "snapshot_id": str(result.snapshot_id) if result.snapshot_id else None,
        "observation_id": str(result.observation_id) if result.observation_id else None,
        "result": result.result,
        "input_fingerprint": result.input_fingerprint,
        "content_fingerprint": result.content_fingerprint,
        "authoritative": result.authoritative,
        "verification_status": result.verification_status,
    }


@router.get("/v2/snapshots/latest")
async def get_latest_snapshot_v2(
    project: str,
    repository_id: str,
    target_ref: str,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Return only an authoritative snapshot inside the caller's project grant."""
    _require_v2_enabled()
    project = normalize_project(project)
    try:
        await require_project_role(_actor(user), project, {"viewer", "proposer", "approver", "admin"})
    except AAGAuthorizationError as exc:
        raise _governance_error(exc) from exc
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            """SELECT s.id AS snapshot_id,o.id AS observation_id,o.project,o.repository_id,
                      o.target_ref,o.resolved_commit_sha,o.authoritative,o.verified_at,
                      s.generated_at,s.stats,s.nodes,s.edges,s.findings,s.unresolved
                 FROM aag_snapshot_observations o
                 JOIN aag_graph_snapshots_v2 s ON s.id=o.snapshot_id
                WHERE o.project=$1 AND o.repository_id=$2 AND o.target_ref=$3
                  AND o.authoritative=TRUE AND o.verification_status='verified'
                  AND s.publish_status='ready'
                ORDER BY o.verified_at DESC LIMIT 1""",
            project, repository_id, target_ref,
        )
    if not row:
        raise HTTPException(status_code=404, detail="authoritative snapshot not found")
    return {"schema_version": "aag-v2", "source": "central", **dict(row)}


@router.post("/v2/exceptions", status_code=201)
async def request_exception(
    body: ExceptionRequest, user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    actor = _actor(user)
    try:
        await require_project_role(actor, body.project, {"proposer", "admin"})
        row = await create_exception(project=body.project,
                                     stable_finding_key=body.stable_finding_key,
                                     proposer_id=actor, reason=body.reason,
                                     expires_at=body.expires_at)
    except (AAGAuthorizationError, AAGWorkflowError) as exc:
        raise _governance_error(exc) from exc
    return dict(row)


@router.post("/v2/exceptions/{exception_id}/decision")
async def exception_decision(
    exception_id: UUID, body: DecisionRequest,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    actor = _actor(user)
    async with get_pool().acquire() as conn:
        project = await conn.fetchval(
            "SELECT project FROM aag_finding_exceptions WHERE id=$1", exception_id
        )
    if not project:
        raise HTTPException(status_code=404, detail="exception not found")
    try:
        await require_project_role(actor, project, {"approver", "admin"})
        return await decide_exception(exception_id=exception_id, approver_id=actor,
                                      approve=body.approve, reason=body.reason)
    except (AAGAuthorizationError, AAGWorkflowError) as exc:
        raise _governance_error(exc) from exc


@router.post("/v2/overrides/{override_id}/approve")
async def override_approval(
    override_id: UUID, body: OverrideApprovalRequest,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    actor = _actor(user)
    try:
        await require_project_role(actor, body.project, {"approver", "admin"})
        return await approve_override(override_id=override_id, project=body.project,
                                      approver_id=actor,
                                      verifier_id=body.verifier_id,
                                      verification=body.verification, reason=body.reason)
    except (AAGAuthorizationError, AAGWorkflowError) as exc:
        raise _governance_error(exc) from exc


@router.post("/v2/overrides", status_code=201)
async def create_override_request(
    body: OverrideRequest, user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    actor = _actor(user)
    try:
        await require_project_role(actor, body.project, {"proposer", "admin"})
        return await request_override(
            request_id=body.request_id, project=body.project,
            repository_id=body.repository_id, target_ref=body.target_ref,
            commit_sha=body.commit_sha, requester_id=actor, reason=body.reason,
            expires_at=body.expires_at,
            previous_healthy_snapshot_id=body.previous_healthy_snapshot_id,
            fallback_snapshot_id=body.fallback_snapshot_id,
            rollback_plan=body.rollback_plan, replay_nonce=body.replay_nonce,
        )
    except (AAGAuthorizationError, AAGWorkflowError) as exc:
        raise _governance_error(exc) from exc


@router.get("/findings")
async def get_findings(
    project: str, rule: str | None = None, severity: str | None = None,
    path_prefix: str | None = None, limit: int = Query(50, ge=1, le=200),
) -> dict[str, Any]:
    await _ensure_table()
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT generated_at, findings FROM aag_graph_snapshots WHERE lower(project)=lower($1) ORDER BY generated_at DESC LIMIT 1",
            project,
        )
    if not row:
        return {"project": project.upper(), "generated_at": None, "stale_minutes": None, "findings": [], "reason": "스냅샷이 없습니다."}
    raw = row["findings"]
    findings = json.loads(raw) if isinstance(raw, str) else raw
    return {"project": project.upper(), "generated_at": row["generated_at"], "stale_minutes": stale_minutes(row["generated_at"]), "findings": filter_findings(findings, rule=rule, severity=severity, path_prefix=path_prefix, limit=limit)}


@router.get("/projects")
async def get_projects() -> dict[str, Any]:
    await _ensure_table()
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            """SELECT DISTINCT ON (project) project, host, generated_at, finding_count,
                      node_count, edge_count, commit_sha
                 FROM aag_graph_snapshots ORDER BY project, generated_at DESC"""
        )
    return {"projects": [{**dict(row), "stale_minutes": stale_minutes(row["generated_at"])} for row in rows]}
