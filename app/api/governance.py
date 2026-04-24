from __future__ import annotations

from typing import Any

import asyncpg
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

from app.core.db_pool import get_pool
from app.core.feature_flags import list_flags, set_flag
from app.services.model_selector import invalidate_intent_policy_cache

router = APIRouter(prefix="/governance", tags=["governance"])


def _iso(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _serialize_row(row: dict[str, Any]) -> dict[str, Any]:
    return {key: _iso(value) for key, value in row.items()}


class IntentPolicyUpsertRequest(BaseModel):
    intent: str
    allowed_models: list[str]
    default_model: str
    cascade_downgrade: bool = False
    tool_allowlist: list[str] | None = None
    description: str | None = None
    updated_by: str = "governance_api"

    @field_validator("intent")
    @classmethod
    def validate_intent(cls, value: str) -> str:
        normalized = (value or "").strip()
        if not normalized:
            raise ValueError("intent is required")
        return normalized

    @field_validator("default_model")
    @classmethod
    def validate_default_model(cls, value: str) -> str:
        normalized = (value or "").strip()
        if not normalized:
            raise ValueError("default_model is required")
        return normalized

    @field_validator("allowed_models")
    @classmethod
    def validate_allowed_models(cls, value: list[str]) -> list[str]:
        items: list[str] = []
        for item in value or []:
            normalized = (item or "").strip()
            if normalized and normalized not in items:
                items.append(normalized)
        if not items:
            raise ValueError("allowed_models is required")
        return items


class FeatureFlagUpdateRequest(BaseModel):
    enabled: bool = Field(..., description="true/false")
    changed_by: str = Field("governance_api", description="변경 주체")


@router.get("/intent-policies")
async def list_intent_policies() -> dict[str, Any]:
    pool = get_pool()
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, intent, allowed_models, default_model, cascade_downgrade,
                       tool_allowlist, description, updated_by, updated_at
                FROM intent_policies
                ORDER BY intent
                """
            )
    except asyncpg.UndefinedTableError:
        return {"policies": [], "total": 0}

    policies = [_serialize_row(dict(row)) for row in rows]
    return {"policies": policies, "total": len(policies)}


@router.post("/intent-policies")
async def upsert_intent_policy(body: IntentPolicyUpsertRequest) -> dict[str, Any]:
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO intent_policies (
                intent, allowed_models, default_model, cascade_downgrade,
                tool_allowlist, description, updated_by, updated_at
            )
            VALUES ($1, $2::text[], $3, $4, $5::text[], $6, $7, NOW())
            ON CONFLICT (intent) DO UPDATE SET
                allowed_models = EXCLUDED.allowed_models,
                default_model = EXCLUDED.default_model,
                cascade_downgrade = EXCLUDED.cascade_downgrade,
                tool_allowlist = EXCLUDED.tool_allowlist,
                description = EXCLUDED.description,
                updated_by = EXCLUDED.updated_by,
                updated_at = NOW()
            RETURNING id, intent, allowed_models, default_model, cascade_downgrade,
                      tool_allowlist, description, updated_by, updated_at
            """,
            body.intent,
            body.allowed_models,
            body.default_model,
            body.cascade_downgrade,
            body.tool_allowlist,
            body.description,
            (body.updated_by or "governance_api").strip() or "governance_api",
        )

    invalidate_intent_policy_cache()
    if row is None:
        raise HTTPException(status_code=500, detail="Intent policy upsert failed")
    return _serialize_row(dict(row))


@router.get("/feature-flags")
async def get_feature_flags() -> dict[str, Any]:
    flags = [_serialize_row(row) for row in await list_flags()]
    return {"flags": flags, "total": len(flags)}


@router.post("/feature-flags/{flag_key}")
async def update_feature_flag(flag_key: str, body: FeatureFlagUpdateRequest) -> dict[str, Any]:
    row = await set_flag(
        flag_key=(flag_key or "").strip(),
        enabled=body.enabled,
        changed_by=body.changed_by,
    )
    return _serialize_row(row)


@router.get("/audit-log")
async def get_governance_audit_log(
    limit: int = Query(100, ge=1, le=100),
) -> dict[str, Any]:
    pool = get_pool()
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, at, event, mode, legacy_result, db_result, diff_summary, trace_id
                FROM governance_audit_log
                ORDER BY at DESC
                LIMIT $1
                """,
                limit,
            )
    except asyncpg.UndefinedTableError:
        return {"items": [], "total": 0}

    items = [_serialize_row(dict(row)) for row in rows]
    return {"items": items, "total": len(items)}
