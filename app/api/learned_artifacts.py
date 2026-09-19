"""Admin activation endpoints for Page Template and Recovery learned versions."""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.auth import TenantRole, require_tenant_role
from app.core.db_pool import get_pool
from app.services.golden_promotion_gate import evaluate_promotion_gate

router = APIRouter(prefix="/browser-learning", tags=["browser-learning"])
admin_dependency = Depends(require_tenant_role(TenantRole.ADMIN))


class PromotionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: str = Field(min_length=8, max_length=200)
    evidence: list[dict[str, Any]] = Field(min_length=1, max_length=100)
    focused_results: dict[str, Any]
    affected_regressions: dict[str, Any]
    candidate_metrics: dict[str, Any]
    active_metrics: dict[str, Any]


class RollbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: str = Field(min_length=8, max_length=200)
    reason: str = Field(min_length=1, max_length=1000)
    evidence: list[dict[str, Any]] = Field(min_length=1, max_length=100)


def _tenant(context: dict[str, Any]) -> str:
    return str(context["tenant"]["id"])


def _actor(context: dict[str, Any]) -> str:
    user = context.get("user") or {}
    return str(user.get("email") or user.get("id") or "artifact_admin")


def _evidence_valid(evidence: list[dict[str, Any]]) -> bool:
    types = {str(item.get("type")) for item in evidence}
    release = next((item for item in evidence if item.get("type") == "release_state"), {})
    return "aads_handover_db" in types and all(key in release for key in ("commit", "push", "deploy"))


@router.post("/{artifact_type}/{artifact_id}/versions/{version}/promote")
async def promote_learned_artifact(
    artifact_type: str, artifact_id: str, version: str, req: PromotionRequest,
    context: dict[str, Any] = admin_dependency,
):
    if artifact_type not in {"page_template", "recovery"}:
        raise HTTPException(422, "unsupported_artifact_type")
    if not _evidence_valid(req.evidence):
        raise HTTPException(422, "release_state_and_aads_handover_db_evidence_required")
    gate = evaluate_promotion_gate(
        focused_results=req.focused_results, affected_regressions=req.affected_regressions,
        candidate_metrics=req.candidate_metrics, active_metrics=req.active_metrics,
    )
    tenant_id = _tenant(context)
    async with get_pool().acquire() as conn, conn.transaction():
        replay = await conn.fetchrow(
            "SELECT decision,to_status,reason_codes FROM browser_promotion_ledgers WHERE tenant_id=$1::uuid AND idempotency_key=$2 FOR UPDATE",
            tenant_id, req.idempotency_key,
        )
        if replay:
            return {"idempotent": True, **dict(replay)}
        row = await conn.fetchrow(
            """SELECT v.* FROM browser_learned_artifact_versions v
               JOIN browser_learned_artifacts a ON a.id=v.artifact_id
               WHERE a.tenant_id=$1::uuid AND a.artifact_type=$2 AND a.id=$3::uuid
                 AND v.version=$4 FOR UPDATE""",
            tenant_id, artifact_type, artifact_id, version,
        )
        if not row:
            raise HTTPException(404, "artifact_version_not_found")
        if row["status"] not in {"candidate", "shadow"}:
            raise HTTPException(409, "artifact_version_not_promotable")
        source_status = row["status"]
        target = "shadow" if source_status == "candidate" else "active"
        reasons = list(gate.reasons)
        previous_id = None
        if not gate.passed:
            target = "quarantined"
        elif target == "active":
            active = await conn.fetchrow(
                "SELECT id FROM browser_learned_artifact_versions WHERE artifact_id=$1::uuid AND status='active' FOR UPDATE",
                artifact_id,
            )
            if active:
                previous_id = active["id"]
                await conn.execute("UPDATE browser_learned_artifact_versions SET status='deprecated' WHERE id=$1", previous_id)
        updated = await conn.fetchrow(
            """UPDATE browser_learned_artifact_versions SET status=$2,previous_active_id=$3,
               quarantine_reason=$4,activated_at=CASE WHEN $2='active' THEN clock_timestamp() ELSE activated_at END
               WHERE id=$1 RETURNING *""",
            row["id"], target, previous_id, ";".join(reasons) or None,
        )
        await conn.execute(
            """INSERT INTO browser_promotion_ledgers
               (tenant_id,artifact_type,artifact_id,version_id,idempotency_key,requested_by,
                from_status,to_status,decision,reason_codes,focused_results,affected_regressions,
                candidate_metrics,active_metrics,evidence)
               VALUES($1::uuid,$2,$3::uuid,$4::uuid,$5,$6,$7,$8,$9,$10::jsonb,$11::jsonb,
                      $12::jsonb,$13::jsonb,$14::jsonb,$15::jsonb)""",
            tenant_id, artifact_type, artifact_id, str(row["id"]), req.idempotency_key,
            _actor(context), source_status, target, "promoted" if gate.passed else "blocked",
            json.dumps(reasons), json.dumps(req.focused_results), json.dumps(req.affected_regressions),
            json.dumps(req.candidate_metrics), json.dumps(req.active_metrics), json.dumps(req.evidence),
        )
        return dict(updated)


@router.post("/{artifact_type}/{artifact_id}/rollback")
async def rollback_learned_artifact(
    artifact_type: str, artifact_id: str, req: RollbackRequest,
    context: dict[str, Any] = admin_dependency,
):
    tenant_id = _tenant(context)
    async with get_pool().acquire() as conn, conn.transaction():
        replay = await conn.fetchrow(
            "SELECT decision,to_status,reason_codes FROM browser_promotion_ledgers WHERE tenant_id=$1::uuid AND idempotency_key=$2 FOR UPDATE",
            tenant_id, req.idempotency_key,
        )
        if replay:
            return {"idempotent": True, **dict(replay)}
        current = await conn.fetchrow(
            """SELECT v.* FROM browser_learned_artifact_versions v JOIN browser_learned_artifacts a ON a.id=v.artifact_id
               WHERE a.tenant_id=$1::uuid AND a.artifact_type=$2 AND a.id=$3::uuid AND v.status='active' FOR UPDATE""",
            tenant_id, artifact_type, artifact_id,
        )
        if not current or not current["previous_active_id"]:
            raise HTTPException(409, "previous_active_not_available")
        previous = await conn.fetchrow(
            "SELECT * FROM browser_learned_artifact_versions WHERE id=$1 AND artifact_id=$2::uuid FOR UPDATE",
            current["previous_active_id"], artifact_id,
        )
        if not previous or previous["status"] != "deprecated":
            raise HTTPException(409, "previous_active_invalid")
        await conn.execute("UPDATE browser_learned_artifact_versions SET status='deprecated' WHERE id=$1", current["id"])
        restored = await conn.fetchrow(
            "UPDATE browser_learned_artifact_versions SET status='active',activated_at=clock_timestamp() WHERE id=$1 RETURNING *",
            previous["id"],
        )
        await conn.execute(
            """INSERT INTO browser_promotion_ledgers
               (tenant_id,artifact_type,artifact_id,version_id,idempotency_key,requested_by,
                from_status,to_status,decision,reason_codes,evidence)
               VALUES($1::uuid,$2,$3::uuid,$4::uuid,$5,$6,'active','active','rolled_back',$7::jsonb,$8::jsonb)""",
            tenant_id, artifact_type, artifact_id, str(current["id"]), req.idempotency_key,
            _actor(context), json.dumps([req.reason]), json.dumps(req.evidence),
        )
        return dict(restored)
