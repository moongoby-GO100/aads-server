"""Fail-closed AAG project authorization and approval workflows."""
from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from app.core.db_pool import get_pool


class AAGAuthorizationError(PermissionError):
    pass


class AAGWorkflowError(ValueError):
    pass


def normalize_project(project: str) -> str:
    value = str(project or "").strip().upper()
    if not value:
        raise AAGAuthorizationError("project is required")
    return value


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _require_future(value: datetime, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise AAGWorkflowError(f"{field} must include a timezone")
    if value <= datetime.now(UTC):
        raise AAGWorkflowError(f"{field} must be in the future")


@dataclass(frozen=True)
class ScannerPrincipal:
    credential_id: UUID
    project: str
    actor_id: str


async def authenticate_scanner(token: str, project: str) -> ScannerPrincipal:
    """Resolve an opaque scanner token and bind it to exactly one project."""
    if not 16 <= len(token) <= 1024:
        raise AAGAuthorizationError("scanner credential required")
    project = normalize_project(project)
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            """SELECT id, project FROM aag_scanner_credentials
                WHERE token_hash=$1 AND revoked_at IS NULL AND expires_at > NOW()""",
            token_digest(token),
        )
        if not row or not hmac.compare_digest(str(row["project"]).upper(), project):
            raise AAGAuthorizationError("scanner credential is not valid for project")
        await conn.execute(
            "UPDATE aag_scanner_credentials SET last_used_at=NOW() WHERE id=$1", row["id"]
        )
    return ScannerPrincipal(row["id"], project, f"scanner:{row['id']}")


async def require_project_role(actor_id: str, project: str, roles: set[str]) -> None:
    project = normalize_project(project)
    if not str(actor_id or "").strip():
        raise AAGAuthorizationError("authenticated actor is required")
    async with get_pool().acquire() as conn:
        allowed = await conn.fetchval(
            """SELECT EXISTS(SELECT 1 FROM aag_project_grants
                 WHERE project=$1 AND principal_id=$2 AND role=ANY($3::text[])
                   AND revoked_at IS NULL)""",
            project, actor_id, sorted(roles),
        )
    if not allowed:
        raise AAGAuthorizationError("project role required")


async def _audit(conn: Any, *, project: str, actor_id: str, action: str,
                 target_type: str, target_id: str, before: Any, after: Any,
                 reason: str, correlation_id: UUID, source: str = "api") -> None:
    await conn.execute(
        """INSERT INTO aag_audit_events
             (project,actor_id,action,target_type,target_id,before_state,after_state,
              reason,correlation_id,source)
           VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7::jsonb,$8,$9,$10)""",
        project, actor_id, action, target_type, target_id,
        json.dumps(before, default=str) if before is not None else None,
        json.dumps(after, default=str) if after is not None else None,
        reason, correlation_id, source,
    )


async def create_exception(*, project: str, stable_finding_key: str,
                           proposer_id: str, reason: str, expires_at: datetime,
                           correlation_id: UUID | None = None) -> dict[str, Any]:
    project = normalize_project(project)
    _require_future(expires_at, "exception expiry")
    correlation_id = correlation_id or uuid4()
    async with get_pool().acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            """INSERT INTO aag_finding_exceptions
                 (project,stable_finding_key,proposer_id,reason,expires_at)
               VALUES ($1,$2,$3,$4,$5) RETURNING *""",
            project, stable_finding_key, proposer_id, reason, expires_at,
        )
        await _audit(conn, project=project, actor_id=proposer_id,
                     action="exception.requested", target_type="exception",
                     target_id=str(row["id"]), before=None, after=dict(row),
                     reason=reason, correlation_id=correlation_id)
    return dict(row)


async def decide_exception(*, exception_id: UUID, approver_id: str, approve: bool,
                           reason: str, correlation_id: UUID | None = None) -> dict[str, Any]:
    correlation_id = correlation_id or uuid4()
    async with get_pool().acquire() as conn, conn.transaction():
        before = await conn.fetchrow(
            "SELECT * FROM aag_finding_exceptions WHERE id=$1 FOR UPDATE", exception_id
        )
        if not before or before["status"] != "pending":
            raise AAGWorkflowError("exception is not pending")
        if before["proposer_id"] == approver_id:
            raise AAGWorkflowError("proposer cannot approve own exception")
        if approve and before["expires_at"] <= datetime.now(UTC):
            raise AAGWorkflowError("exception has expired")
        status = "approved" if approve else "rejected"
        row = await conn.fetchrow(
            """UPDATE aag_finding_exceptions SET status=$2,approver_id=$3,decided_at=NOW()
                WHERE id=$1 RETURNING *""", exception_id, status, approver_id,
        )
        await _audit(conn, project=row["project"], actor_id=approver_id,
                     action=f"exception.{status}", target_type="exception",
                     target_id=str(exception_id), before=dict(before), after=dict(row),
                     reason=reason, correlation_id=correlation_id)
    return dict(row)


async def request_override(*, request_id: UUID, project: str, repository_id: str,
                           target_ref: str, commit_sha: str, requester_id: str,
                           reason: str, expires_at: datetime,
                           previous_healthy_snapshot_id: UUID,
                           fallback_snapshot_id: UUID | None, rollback_plan: dict[str, Any],
                           replay_nonce: str,
                           correlation_id: UUID | None = None) -> dict[str, Any]:
    project = normalize_project(project)
    _require_future(expires_at, "override expiry")
    if not replay_nonce:
        raise AAGWorkflowError("replay nonce is required")
    if not rollback_plan:
        raise AAGWorkflowError("rollback plan is required")
    correlation_id = correlation_id or uuid4()
    async with get_pool().acquire() as conn, conn.transaction():
        healthy = await conn.fetchval(
            """SELECT EXISTS(
                 SELECT 1 FROM aag_graph_snapshots_v2 s
                 JOIN aag_snapshot_observations o ON o.snapshot_id=s.id
                WHERE s.id=$1 AND s.project=$2 AND s.repository_id=$3
                  AND o.target_ref=$4
                  AND s.publish_status='ready' AND o.authoritative=TRUE
                  AND o.verification_status='verified')""",
            previous_healthy_snapshot_id, project, repository_id, target_ref,
        )
        if not healthy:
            raise AAGWorkflowError("previous healthy snapshot is not authoritative")
        if fallback_snapshot_id is not None:
            fallback_scoped = await conn.fetchval(
                """SELECT EXISTS(
                     SELECT 1 FROM aag_graph_snapshots_v2 s
                     JOIN aag_snapshot_observations o ON o.snapshot_id=s.id
                    WHERE s.id=$1 AND s.project=$2 AND s.repository_id=$3
                      AND o.target_ref=$4)""",
                fallback_snapshot_id, project, repository_id, target_ref,
            )
            if not fallback_scoped:
                raise AAGWorkflowError("fallback snapshot is outside project scope")
        row = await conn.fetchrow(
            """INSERT INTO aag_central_outage_overrides
                 (request_id,project,repository_id,target_ref,commit_sha,requester_id,
                  reason,expires_at,previous_healthy_snapshot_id,fallback_snapshot_id,
                  rollback_plan,replay_nonce_hash)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11::jsonb,$12) RETURNING *""",
            request_id, project, repository_id, target_ref, commit_sha, requester_id,
            reason, expires_at, previous_healthy_snapshot_id, fallback_snapshot_id,
            json.dumps(rollback_plan), token_digest(replay_nonce),
        )
        await _audit(conn, project=project, actor_id=requester_id,
                     action="override.requested", target_type="override",
                     target_id=str(row["id"]), before=None, after=dict(row),
                     reason=reason, correlation_id=correlation_id)
    return dict(row)


async def record_override_verification(
    *, override_id: UUID, project: str, verifier_id: str,
    verified_commit_sha: str, passed: bool, evidence: dict[str, Any], reason: str,
    correlation_id: UUID | None = None,
) -> dict[str, Any]:
    """Record one authenticated independent verification before approval."""
    correlation_id = correlation_id or uuid4()
    async with get_pool().acquire() as conn, conn.transaction():
        before = await conn.fetchrow(
            """SELECT * FROM aag_central_outage_overrides
                WHERE id=$1 AND project=$2 FOR UPDATE""",
            override_id, normalize_project(project),
        )
        if not before or before["status"] != "requested":
            raise AAGWorkflowError("override is not requestable (replay rejected)")
        if before["expires_at"] <= datetime.now(UTC):
            raise AAGWorkflowError("override has expired")
        if before["requester_id"] == verifier_id:
            raise AAGWorkflowError("requester cannot verify own override")
        if before["independent_verifier_id"] is not None:
            raise AAGWorkflowError("override verification already recorded")
        if not hmac.compare_digest(str(before["commit_sha"]), verified_commit_sha):
            raise AAGWorkflowError("verified commit does not match override commit")
        verification = {
            "passed": bool(passed),
            "verified_commit_sha": verified_commit_sha,
            "evidence": evidence,
        }
        row = await conn.fetchrow(
            """UPDATE aag_central_outage_overrides
                  SET independent_verifier_id=$2,independent_verification=$3::jsonb
                WHERE id=$1 AND status='requested' AND independent_verifier_id IS NULL
                RETURNING *""",
            override_id, verifier_id, json.dumps(verification),
        )
        if not row:
            raise AAGWorkflowError("override verification replay rejected")
        await _audit(conn, project=row["project"], actor_id=verifier_id,
                     action="override.verification_recorded", target_type="override",
                     target_id=str(override_id), before=dict(before), after=dict(row),
                     reason=reason, correlation_id=correlation_id)
    return dict(row)


async def approve_override(*, override_id: UUID, project: str, approver_id: str,
                           reason: str, correlation_id: UUID | None = None) -> dict[str, Any]:
    """Approve once after a separately authenticated independent verification."""
    correlation_id = correlation_id or uuid4()
    async with get_pool().acquire() as conn, conn.transaction():
        before = await conn.fetchrow(
            """SELECT * FROM aag_central_outage_overrides
                WHERE id=$1 AND project=$2 FOR UPDATE""",
            override_id, normalize_project(project),
        )
        if not before or before["status"] != "requested":
            raise AAGWorkflowError("override is not requestable (replay rejected)")
        if before["expires_at"] <= datetime.now(UTC):
            raise AAGWorkflowError("override has expired")
        verifier_id = before["independent_verifier_id"]
        verification = before["independent_verification"]
        if isinstance(verification, str):
            verification = json.loads(verification)
        if len({before["requester_id"], approver_id, verifier_id}) != 3:
            raise AAGWorkflowError("requester, approver and verifier must be distinct")
        if not isinstance(verification, dict) or not verification.get("passed"):
            raise AAGWorkflowError("independent verification did not pass")
        if not hmac.compare_digest(
            str(before["commit_sha"]), str(verification.get("verified_commit_sha") or "")
        ):
            raise AAGWorkflowError("verified commit does not match override commit")
        row = await conn.fetchrow(
            """UPDATE aag_central_outage_overrides
                  SET status='active',approver_id=$2,approved_at=NOW(),activated_at=NOW()
                WHERE id=$1 AND status='requested' RETURNING *""",
            override_id, approver_id,
        )
        await _audit(conn, project=row["project"], actor_id=approver_id,
                     action="override.activated", target_type="override",
                     target_id=str(override_id), before=dict(before), after=dict(row),
                     reason=reason, correlation_id=correlation_id)
    return dict(row)


async def expire_exceptions() -> int:
    """Expire pending/approved exceptions and retain an append-only audit trail."""
    async with get_pool().acquire() as conn, conn.transaction():
        rows = await conn.fetch(
            """SELECT * FROM aag_finding_exceptions
                WHERE status IN ('pending','approved') AND expires_at <= NOW()
                FOR UPDATE SKIP LOCKED"""
        )
        for before in rows:
            row = await conn.fetchrow(
                """UPDATE aag_finding_exceptions
                      SET status='expired',decided_at=COALESCE(decided_at,NOW())
                    WHERE id=$1 AND status IN ('pending','approved') RETURNING *""",
                before["id"],
            )
            if not row:
                continue
            await _audit(conn, project=row["project"], actor_id="system:expiration",
                         action="exception.expired", target_type="exception",
                         target_id=str(row["id"]), before=dict(before), after=dict(row),
                         reason="exception validity window elapsed",
                         correlation_id=uuid4(), source="scheduler")
    return len(rows)


async def expire_overrides_and_queue_revalidation() -> int:
    """Recovery job: expire overrides and idempotently require authoritative revalidation."""
    async with get_pool().acquire() as conn, conn.transaction():
        rows = await conn.fetch(
            """SELECT * FROM aag_central_outage_overrides
                WHERE status='active' AND expires_at <= NOW()
                FOR UPDATE SKIP LOCKED"""
        )
        for before in rows:
            row = await conn.fetchrow(
                """UPDATE aag_central_outage_overrides
                      SET status='expired',closed_at=NOW()
                    WHERE id=$1 AND status='active' RETURNING *""",
                before["id"],
            )
            if not row:
                continue
            await conn.execute(
                """INSERT INTO aag_revalidation_jobs
                     (override_id,project,repository_id,target_ref,expected_commit_sha)
                   VALUES ($1,$2,$3,$4,$5) ON CONFLICT (override_id) DO NOTHING""",
                row["id"], row["project"], row["repository_id"],
                row["target_ref"], row["commit_sha"],
            )
            await _audit(conn, project=row["project"], actor_id="system:revalidation",
                         action="override.expired", target_type="override",
                         target_id=str(row["id"]), before=dict(before),
                         after={"status": "expired", "authoritative": False},
                         reason="override expired; authoritative revalidation queued",
                         correlation_id=uuid4(), source="scheduler")
    return len(rows)
