"""Stable-key baseline gates and approval-controlled AAG rule lifecycle."""
from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from typing import Any
from uuid import UUID, uuid4

from app.core.db_pool import get_pool
from app.services.aag_governance import AAGWorkflowError, _audit, normalize_project
from tools.aag.v2_contract import (
    STABLE_KEY_VERSION,
    finding_key_set_digest,
    normalize_findings,
)


@dataclass(frozen=True)
class BaselineGateResult:
    passed: bool
    blocking_keys: tuple[str, ...]
    warning_keys: tuple[str, ...]
    excepted_keys: tuple[str, ...]
    resolved_keys: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def evaluate_baseline(
    *,
    current_findings: Iterable[Mapping[str, Any]],
    baseline_keys: Iterable[str],
    rule_modes: Mapping[str, str],
    active_exception_keys: Iterable[str] = (),
) -> BaselineGateResult:
    """Evaluate additions by stable key; unknown/new rules are warn-only."""
    current = {str(item.get("stable_finding_key") or ""): item for item in current_findings}
    if "" in current:
        raise AAGWorkflowError("current finding is missing stable_finding_key")
    baseline, excepted = set(baseline_keys), set(active_exception_keys)
    added = set(current) - baseline
    excepted_added = added & excepted
    blocking, warnings = [], []
    for key in sorted(added - excepted_added):
        rule = str(current[key].get("rule") or "").strip().upper()
        mode = str(rule_modes.get(rule) or "warn_only")
        (blocking if mode == "enforced" else warnings).append(key)
    return BaselineGateResult(
        passed=not blocking,
        blocking_keys=tuple(blocking),
        warning_keys=tuple(warnings),
        excepted_keys=tuple(sorted(excepted_added)),
        resolved_keys=tuple(sorted(baseline - set(current))),
    )


def validate_rule_promotion(
    *, proposer_id: str, approver_id: str, observation_count: int,
    fixture_digest: str, fixture_passed: bool,
) -> None:
    if proposer_id == approver_id:
        raise AAGWorkflowError("rule proposer cannot approve own promotion")
    if observation_count < 2:
        raise AAGWorkflowError("rule requires at least two warn-only observations")
    if not fixture_passed or len(str(fixture_digest or "")) != 64:
        raise AAGWorkflowError("rule promotion requires passing golden fixture evidence")


async def propose_baseline(
    *, project: str, repository_id: str, target_ref: str, governance_scope: str,
    snapshot_id: UUID, proposer_id: str, reason: str,
) -> dict[str, Any]:
    project = normalize_project(project)
    correlation_id = uuid4()
    async with get_pool().acquire() as conn, conn.transaction():
        snapshot = await conn.fetchrow(
            """SELECT s.findings,s.stable_key_version
                 FROM aag_graph_snapshots_v2 s
                 JOIN aag_snapshot_observations o ON o.snapshot_id=s.id
                WHERE s.id=$1 AND s.project=$2 AND s.repository_id=$3
                  AND o.target_ref=$4 AND o.governance_scope=$5
                  AND s.publish_status='ready' AND o.authoritative=TRUE
                  AND o.verification_status='verified'
                ORDER BY o.verified_at DESC LIMIT 1""",
            snapshot_id, project, repository_id, target_ref, governance_scope,
        )
        if not snapshot:
            raise AAGWorkflowError("baseline snapshot is not authoritative in scope")
        if snapshot["stable_key_version"] != STABLE_KEY_VERSION:
            raise AAGWorkflowError("baseline stable key version requires explicit migration")
        findings = normalize_findings(project, list(snapshot["findings"] or []))
        digest = finding_key_set_digest(findings)
        row = await conn.fetchrow(
            """INSERT INTO aag_baselines
                 (project,repository_id,target_ref,governance_scope,snapshot_id,
                  stable_key_version,key_set_digest,proposer_id,reason)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
               ON CONFLICT (project,repository_id,target_ref,governance_scope,key_set_digest)
               DO NOTHING RETURNING *""",
            project, repository_id, target_ref, governance_scope, snapshot_id,
            STABLE_KEY_VERSION, digest, proposer_id, reason,
        )
        if not row:
            raise AAGWorkflowError("identical baseline proposal already exists")
        for finding in findings:
            await conn.execute(
                """INSERT INTO aag_baseline_findings
                     (baseline_id,stable_finding_key,stable_key_version,rule,severity,finding)
                   VALUES ($1,$2,$3,$4,$5,$6::jsonb)""",
                row["id"], finding["stable_finding_key"], STABLE_KEY_VERSION,
                str(finding.get("rule") or "UNKNOWN").upper(),
                str(finding.get("severity") or "P2").upper(), json.dumps(finding),
            )
        await _audit(
            conn, project=project, actor_id=proposer_id, action="baseline.proposed",
            target_type="baseline", target_id=str(row["id"]), before=None,
            after=dict(row), reason=reason, correlation_id=correlation_id,
        )
    return dict(row)


async def approve_baseline(
    *, baseline_id: UUID, approver_id: str, reason: str,
) -> dict[str, Any]:
    correlation_id = uuid4()
    async with get_pool().acquire() as conn, conn.transaction():
        before = await conn.fetchrow(
            "SELECT * FROM aag_baselines WHERE id=$1 FOR UPDATE", baseline_id
        )
        if not before or before["status"] != "proposed":
            raise AAGWorkflowError("baseline is not proposed")
        if before["proposer_id"] == approver_id:
            raise AAGWorkflowError("baseline proposer cannot approve own proposal")
        await conn.execute(
            """UPDATE aag_baselines SET status='superseded',superseded_at=NOW()
                WHERE project=$1 AND repository_id=$2 AND target_ref=$3
                  AND governance_scope=$4 AND status='approved'""",
            before["project"], before["repository_id"], before["target_ref"],
            before["governance_scope"],
        )
        row = await conn.fetchrow(
            """UPDATE aag_baselines SET status='approved',approver_id=$2,approved_at=NOW()
                WHERE id=$1 AND status='proposed' RETURNING *""",
            baseline_id, approver_id,
        )
        await _audit(
            conn, project=row["project"], actor_id=approver_id,
            action="baseline.approved", target_type="baseline",
            target_id=str(baseline_id), before=dict(before), after=dict(row),
            reason=reason, correlation_id=correlation_id,
        )
    return dict(row)


async def register_warn_only_rule(
    *, project: str, rule: str, rule_version: str, proposer_id: str, reason: str,
) -> dict[str, Any]:
    project, rule = normalize_project(project), str(rule).strip().upper()
    async with get_pool().acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            """INSERT INTO aag_rule_lifecycle
                 (project,rule,rule_version,mode,status,proposer_id,reason)
               VALUES ($1,$2,$3,'warn_only','active',$4,$5) RETURNING *""",
            project, rule, rule_version, proposer_id, reason,
        )
        await _audit(
            conn, project=project, actor_id=proposer_id, action="rule.warn_only_started",
            target_type="rule", target_id=str(row["id"]), before=None,
            after=dict(row), reason=reason, correlation_id=uuid4(),
        )
    return dict(row)


async def promote_rule(
    *, lifecycle_id: UUID, approver_id: str, observation_count: int,
    fixture_digest: str, fixture_passed: bool, reason: str,
) -> dict[str, Any]:
    async with get_pool().acquire() as conn, conn.transaction():
        before = await conn.fetchrow(
            "SELECT * FROM aag_rule_lifecycle WHERE id=$1 FOR UPDATE", lifecycle_id
        )
        if not before or before["status"] != "active" or before["mode"] != "warn_only":
            raise AAGWorkflowError("rule is not an active warn-only candidate")
        validate_rule_promotion(
            proposer_id=before["proposer_id"], approver_id=approver_id,
            observation_count=observation_count, fixture_digest=fixture_digest,
            fixture_passed=fixture_passed,
        )
        row = await conn.fetchrow(
            """UPDATE aag_rule_lifecycle
                  SET mode='enforced',approver_id=$2,approved_at=NOW(),
                      observation_count=$3,fixture_digest=$4
                WHERE id=$1 AND mode='warn_only' RETURNING *""",
            lifecycle_id, approver_id, observation_count, fixture_digest,
        )
        await _audit(
            conn, project=row["project"], actor_id=approver_id,
            action="rule.enforced", target_type="rule", target_id=str(lifecycle_id),
            before=dict(before), after=dict(row), reason=reason, correlation_id=uuid4(),
        )
    return dict(row)
