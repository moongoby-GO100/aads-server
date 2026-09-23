"""AAG graph snapshot ingestion and read-only query API."""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from time import perf_counter
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from app.auth import get_current_user
from app.core.db_pool import get_pool
from app.services.aag_brief_v2 import build_brief_v2
from app.services.aag_governance import (
    AAGAuthorizationError,
    AAGWorkflowError,
    approve_override,
    authenticate_scanner,
    create_exception,
    decide_exception,
    normalize_project,
    record_override_verification,
    request_override,
    require_project_role,
)
from app.services.aag_ingest_v2 import IngestConflictError, ingest_graph
from app.services.aag_query_v2 import (
    AAGQueryError,
    consumer_telemetry,
    load_authoritative_snapshot,
    query_findings_page,
    record_consumer_event,
)
from app.services.aag_tools import filter_findings, stale_minutes
from tools.aag.v2_contract import CANONICALIZATION_VERSION, STABLE_KEY_VERSION

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/aag", tags=["aag"])
CurrentUser = Annotated[dict[str, Any], Depends(get_current_user)]

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


class RunnerBriefRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project: str = Field(min_length=1, max_length=100)
    target: str = Field(min_length=1, max_length=50_000)
    repository_id: str | None = Field(default=None, min_length=1, max_length=255)
    target_ref: str | None = Field(default=None, min_length=1, max_length=255)
    governance_scope: str = Field(default="default", min_length=1, max_length=255)


class ExceptionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project: str = Field(min_length=1, max_length=100)
    stable_finding_key: str = Field(min_length=1, max_length=500)
    reason: str = Field(min_length=3, max_length=2000)
    expires_at: datetime


class DecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approve: bool
    reason: str = Field(min_length=3, max_length=2000)


class OverrideApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project: str = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=3, max_length=2000)


class OverrideVerificationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project: str = Field(min_length=1, max_length=100)
    passed: bool
    verified_commit_sha: str = Field(pattern=r"^[0-9a-f]{7,64}$")
    evidence: dict[str, Any] = Field(min_length=1)
    reason: str = Field(min_length=3, max_length=2000)


class OverrideRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: UUID
    project: str = Field(min_length=1, max_length=100)
    repository_id: str = Field(min_length=1, max_length=255)
    target_ref: str = Field(min_length=1, max_length=255)
    commit_sha: str = Field(pattern=r"^[0-9a-f]{7,64}$")
    reason: str = Field(min_length=3, max_length=2000)
    expires_at: datetime
    previous_healthy_snapshot_id: UUID
    fallback_snapshot_id: UUID | None = None
    rollback_plan: dict[str, Any] = Field(min_length=1)
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


@router.get("/v2/latest")
async def get_latest_snapshot_v2(
    project: str,
    repository_id: str,
    target_ref: str,
    user: CurrentUser,
    governance_scope: str = "default",
) -> dict[str, Any]:
    """Return the project-authorized atomic pointer for one repository/ref/scope."""
    _require_v2_enabled()
    project = normalize_project(project)
    try:
        await require_project_role(_actor(user), project, {"viewer", "proposer", "approver", "admin"})
    except AAGAuthorizationError as exc:
        raise _governance_error(exc) from exc
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            """SELECT p.snapshot_id, p.observation_id, p.run_id,
                      p.project, p.repository_id, p.target_ref, p.governance_scope,
                      p.resolved_commit_sha, p.expected_target_ref_head_sha,
                      p.generated_at, p.verified_at, s.content_fingerprint,
                      r.scanner_version, r.ruleset_digest, r.scan_scope_digest,
                      s.canonicalization_version, s.stable_key_version,
                      s.stats, s.nodes, s.edges, s.findings, s.unresolved,
                      s.node_count, s.edge_count, s.finding_count,
                      s.first_published_at
                 FROM aag_latest_pointers p
                 JOIN aag_graph_snapshots_v2 s
                   ON s.id=p.snapshot_id AND s.project=p.project
                  AND s.repository_id=p.repository_id
                 JOIN aag_snapshot_observations o
                   ON o.id=p.observation_id AND o.snapshot_id=p.snapshot_id
                  AND o.run_id=p.run_id AND o.project=p.project
                  AND o.repository_id=p.repository_id AND o.target_ref=p.target_ref
                  AND o.governance_scope=p.governance_scope
                  AND o.resolved_commit_sha=p.resolved_commit_sha
                 JOIN aag_scan_runs r
                   ON r.id=p.run_id AND r.project=p.project
                  AND r.repository_id=p.repository_id AND r.target_ref=p.target_ref
                  AND r.governance_scope=p.governance_scope
                  AND r.resolved_commit_sha=p.resolved_commit_sha
                WHERE p.project=$1 AND p.repository_id=$2 AND p.target_ref=$3
                  AND p.governance_scope=$4 AND s.publish_status='ready'
                  AND o.authoritative=TRUE AND o.verification_status='verified'""",
            project, repository_id.strip(), target_ref.strip(), governance_scope.strip(),
        )
    if not row:
        raise HTTPException(status_code=404, detail="No authoritative AAG v2 snapshot")
    freshness_minutes = stale_minutes(row["verified_at"])
    return {
        "schema_version": "aag-v2",
        "snapshot": {
            "id": str(row["snapshot_id"]),
            "content_fingerprint": row["content_fingerprint"],
            "canonicalization_version": row["canonicalization_version"],
            "stable_key_version": row["stable_key_version"],
            "stats": row["stats"], "nodes": row["nodes"], "edges": row["edges"],
            "findings": row["findings"], "unresolved": row["unresolved"],
            "node_count": row["node_count"], "edge_count": row["edge_count"],
            "finding_count": row["finding_count"],
        },
        "observation_id": str(row["observation_id"]),
        "run_id": str(row["run_id"]),
        "ref": {
            "project": row["project"], "repository_id": row["repository_id"],
            "target_ref": row["target_ref"], "governance_scope": row["governance_scope"],
        },
        "commit": {
            "resolved": row["resolved_commit_sha"],
            "expected_ref_head": row["expected_target_ref_head_sha"],
        },
        "source": "central_db",
        "authoritative": True,
        "analyzer": {
            "name": "scan_aads",
            "version": row["scanner_version"],
            "ruleset_digest": row["ruleset_digest"],
            "scan_scope_digest": row["scan_scope_digest"],
        },
        "generated_at": row["generated_at"],
        "verified_at": row["verified_at"],
        "first_published_at": row["first_published_at"],
        "freshness": {"status": "fresh", "age_minutes": freshness_minutes},
        "limitation": [],
    }


@router.get("/v2/brief")
async def get_brief_v2(
    project: str,
    repository_id: str,
    target_ref: str,
    target: str,
    user: CurrentUser,
    governance_scope: str = "default",
) -> dict[str, Any]:
    """Return a commit-pinned brief without absence-of-impact claims."""
    latest = await get_latest_snapshot_v2(
        project=project,
        repository_id=repository_id,
        target_ref=target_ref,
        governance_scope=governance_scope,
        user=user,
    )
    snapshot = latest["snapshot"]
    target_key = target.strip().replace("\\", "/")
    matches = []
    for finding in snapshot.get("findings") or []:
        identities = (
            finding.get("file"), finding.get("module"), finding.get("path"),
            finding.get("key"), finding.get("semantic_target"),
        )
        if any(
            target_key and target_key in str(value or "").replace("\\", "/")
            for value in identities
        ):
            matches.append(finding)
    status = "detected" if matches else (
        "partial" if snapshot.get("unresolved") else "not_detected"
    )
    return build_brief_v2(
        snapshot={
            "snapshot_id": snapshot["id"],
            "observation_id": latest["observation_id"],
            "run_id": latest["run_id"],
            **latest["ref"],
            "resolved_commit_sha": latest["commit"]["resolved"],
            "expected_target_ref_head_sha": latest["commit"]["expected_ref_head"],
            "source": latest["source"],
            "authoritative": latest["authoritative"],
            "verified_at": latest["verified_at"],
            "analyzer": latest["analyzer"],
            "stats": snapshot.get("stats") or {},
            "truncated": False,
        },
        target=target_key,
        findings=matches,
        status=status,
    )


@router.post("/v2/runner-brief")
async def get_runner_brief_v2(
    body: RunnerBriefRequest,
    x_aag_scanner_token: str | None = Header(default=None, alias="X-AAG-Scanner-Token"),
) -> dict[str, Any]:
    """Return a project-scoped, commit-pinned brief for Pipeline Runner.

    Runners do not carry an interactive user session, so this route authenticates
    the same opaque project credential used by the v2 publisher.  A missing
    authoritative snapshot fails closed instead of falling back to a local graph.
    """
    _require_v2_enabled()
    normalized = normalize_project(body.project)
    started = perf_counter()
    try:
        principal = await authenticate_scanner(x_aag_scanner_token or "", normalized)
    except AAGAuthorizationError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    snapshot = await load_authoritative_snapshot(
        project=normalized,
        repository_id=body.repository_id,
        target_ref=body.target_ref,
        governance_scope=body.governance_scope,
    )
    if not snapshot:
        await record_consumer_event(
            consumer=f"pipeline-runner:{principal.credential_id}",
            api_version="v2",
            endpoint="/aag/v2/runner-brief",
            project=normalized,
            outcome="not_found",
            repository_id=body.repository_id,
            target_ref=body.target_ref,
            latency_ms=int((perf_counter() - started) * 1000),
        )
        raise HTTPException(status_code=404, detail="No authoritative AAG v2 snapshot")

    target_key = body.target.strip().replace("\\", "/")
    matches = []
    for finding in snapshot.get("findings") or []:
        identities = (
            finding.get("file"), finding.get("module"), finding.get("path"),
            finding.get("key"), finding.get("semantic_target"),
        )
        if any(
            target_key and target_key in str(value or "").replace("\\", "/")
            for value in identities
        ):
            matches.append(finding)
    status = "detected" if matches else (
        "partial" if snapshot.get("unresolved") else "not_detected"
    )
    brief = build_brief_v2(
        snapshot={
            "snapshot_id": str(snapshot["snapshot_id"]),
            "observation_id": str(snapshot["observation_id"]),
            "run_id": str(snapshot["run_id"]),
            "project": snapshot["project"],
            "repository_id": snapshot["repository_id"],
            "target_ref": snapshot["target_ref"],
            "governance_scope": snapshot["governance_scope"],
            "resolved_commit_sha": snapshot["resolved_commit_sha"],
            "expected_target_ref_head_sha": snapshot["expected_target_ref_head_sha"],
            "source": "central_db",
            "authoritative": True,
            "verified_at": snapshot["verified_at"],
            "analyzer": {
                "name": "scan_aads",
                "version": snapshot["scanner_version"],
                "ruleset_digest": snapshot["ruleset_digest"],
                "scan_scope_digest": snapshot["scan_scope_digest"],
            },
            "stats": snapshot.get("stats") or {},
            "truncated": False,
        },
        target=target_key,
        findings=matches,
        status=status,
    )
    await record_consumer_event(
        consumer=f"pipeline-runner:{principal.credential_id}",
        api_version="v2",
        endpoint="/aag/v2/runner-brief",
        project=normalized,
        outcome="success",
        repository_id=snapshot["repository_id"],
        target_ref=snapshot["target_ref"],
        snapshot_id=str(snapshot["snapshot_id"]),
        latency_ms=int((perf_counter() - started) * 1000),
    )
    return {"brief": brief, "fallback_used": False}


@router.get("/v2/findings")
async def get_findings_v2(
    project: str,
    user: CurrentUser,
    repository_id: str | None = None,
    target_ref: str | None = None,
    governance_scope: str = "default",
    snapshot_id: UUID | None = None,
    cursor: str | None = None,
    page_size: int = Query(50, ge=1, le=200),
    rule: str | None = None,
    severity: str | None = None,
    path_prefix: str | None = None,
    stable_finding_key: str | None = None,
    status: str | None = None,
    owner: str | None = None,
    x_request_id: str | None = Header(default=None, alias="X-Request-ID"),
    x_aag_consumer: str | None = Header(default=None, alias="X-AAG-Consumer"),
) -> dict[str, Any]:
    """Return deterministic findings pages pinned to one immutable snapshot."""
    _require_v2_enabled()
    normalized = normalize_project(project)
    try:
        await require_project_role(
            _actor(user), normalized, {"viewer", "proposer", "approver", "admin"},
        )
    except AAGAuthorizationError as exc:
        raise _governance_error(exc) from exc
    started = perf_counter()
    try:
        result = await query_findings_page(
            project=normalized,
            repository_id=repository_id,
            target_ref=target_ref,
            governance_scope=governance_scope,
            snapshot_id=str(snapshot_id) if snapshot_id else None,
            cursor=cursor,
            page_size=page_size,
            rule=rule,
            severity=severity,
            path_prefix=path_prefix,
            stable_finding_key=stable_finding_key,
            status=status,
            owner=owner,
        )
    except AAGQueryError as exc:
        await record_consumer_event(
            consumer=x_aag_consumer or "http-unknown", api_version="v2",
            endpoint="/aag/v2/findings", project=normalized, outcome="query_error",
            request_id=x_request_id, repository_id=repository_id,
            target_ref=target_ref, latency_ms=int((perf_counter() - started) * 1000),
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await record_consumer_event(
        consumer=x_aag_consumer or "http-unknown", api_version="v2",
        endpoint="/aag/v2/findings", project=normalized, outcome="success",
        request_id=x_request_id, repository_id=result["ref"]["repository_id"],
        target_ref=result["ref"]["target_ref"], snapshot_id=result["snapshot_id"],
        latency_ms=int((perf_counter() - started) * 1000),
    )
    return result


@router.get("/v2/consumer-telemetry")
async def get_consumer_telemetry(
    project: str,
    user: CurrentUser,
    hours: int = Query(24, ge=1, le=24 * 90),
) -> dict[str, Any]:
    """Report residual v1 consumers before any retirement decision."""
    _require_v2_enabled()
    normalized = normalize_project(project)
    try:
        await require_project_role(_actor(user), normalized, {"admin"})
    except AAGAuthorizationError as exc:
        raise _governance_error(exc) from exc
    rows = await consumer_telemetry(normalized, hours)
    return {
        "project": normalized,
        "window_hours": hours,
        "consumers": rows,
        "v1_requests": sum(row["requests"] for row in rows if row["api_version"] == "v1"),
        "v1_retirement_ready": bool(rows) and not any(
            row["api_version"] == "v1" and row["requests"] > 0 for row in rows
        ),
    }


@router.post("/v2/exceptions", status_code=201)
async def request_exception(
    body: ExceptionRequest, user: CurrentUser,
) -> dict[str, Any]:
    _require_v2_enabled()
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
    user: CurrentUser,
) -> dict[str, Any]:
    _require_v2_enabled()
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


@router.post("/v2/overrides/{override_id}/verify")
async def override_verification(
    override_id: UUID, body: OverrideVerificationRequest, user: CurrentUser,
) -> dict[str, Any]:
    _require_v2_enabled()
    actor = _actor(user)
    try:
        await require_project_role(actor, body.project, {"approver", "admin"})
        return await record_override_verification(
            override_id=override_id, project=body.project, verifier_id=actor,
            verified_commit_sha=body.verified_commit_sha, passed=body.passed,
            evidence=body.evidence, reason=body.reason,
        )
    except (AAGAuthorizationError, AAGWorkflowError) as exc:
        raise _governance_error(exc) from exc


@router.post("/v2/overrides/{override_id}/approve")
async def override_approval(
    override_id: UUID, body: OverrideApprovalRequest,
    user: CurrentUser,
) -> dict[str, Any]:
    _require_v2_enabled()
    actor = _actor(user)
    try:
        await require_project_role(actor, body.project, {"approver", "admin"})
        return await approve_override(override_id=override_id, project=body.project,
                                      approver_id=actor, reason=body.reason)
    except (AAGAuthorizationError, AAGWorkflowError) as exc:
        raise _governance_error(exc) from exc


@router.post("/v2/overrides", status_code=201)
async def create_override_request(
    body: OverrideRequest, user: CurrentUser,
) -> dict[str, Any]:
    _require_v2_enabled()
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
    x_request_id: str | None = Header(default=None, alias="X-Request-ID"),
    x_aag_consumer: str | None = Header(default=None, alias="X-AAG-Consumer"),
) -> dict[str, Any]:
    started = perf_counter()
    await _ensure_table()
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT generated_at, findings FROM aag_graph_snapshots WHERE lower(project)=lower($1) ORDER BY generated_at DESC LIMIT 1",
            project,
        )
    if not row:
        await record_consumer_event(
            consumer=x_aag_consumer or "http-unknown", api_version="v1",
            endpoint="/aag/findings", project=project, outcome="not_found",
            request_id=x_request_id, latency_ms=int((perf_counter() - started) * 1000),
        )
        return {"project": project.upper(), "generated_at": None, "stale_minutes": None, "findings": [], "reason": "스냅샷이 없습니다."}
    raw = row["findings"]
    findings = json.loads(raw) if isinstance(raw, str) else raw
    result = {"project": project.upper(), "generated_at": row["generated_at"], "stale_minutes": stale_minutes(row["generated_at"]), "findings": filter_findings(findings, rule=rule, severity=severity, path_prefix=path_prefix, limit=limit)}
    await record_consumer_event(
        consumer=x_aag_consumer or "http-unknown", api_version="v1",
        endpoint="/aag/findings", project=project, outcome="success",
        request_id=x_request_id, latency_ms=int((perf_counter() - started) * 1000),
    )
    return result


@router.get("/projects")
async def get_projects(
    x_request_id: str | None = Header(default=None, alias="X-Request-ID"),
    x_aag_consumer: str | None = Header(default=None, alias="X-AAG-Consumer"),
) -> dict[str, Any]:
    started = perf_counter()
    await _ensure_table()
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            """SELECT DISTINCT ON (project) project, host, generated_at, finding_count,
                      node_count, edge_count, commit_sha
                 FROM aag_graph_snapshots ORDER BY project, generated_at DESC"""
        )
    await record_consumer_event(
        consumer=x_aag_consumer or "http-unknown", api_version="v1",
        endpoint="/aag/projects", project="ALL", outcome="success",
        request_id=x_request_id, latency_ms=int((perf_counter() - started) * 1000),
    )
    return {"projects": [{**dict(row), "stale_minutes": stale_minutes(row["generated_at"])} for row in rows]}
