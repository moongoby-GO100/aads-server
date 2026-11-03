"""Transactional milestone creation; replay returns the original response, not live status.

Migration 20261103 must precede keyed traffic. No runtime schema changes.
"""
from __future__ import annotations

import hashlib
import json
import uuid


class MilestoneConflict(Exception):
    """An existing scoped key has a different canonical payload."""


class MilestoneGoalNotFound(Exception):
    """The goal does not belong to the authenticated tenant."""


def fingerprint(payload: dict) -> str:
    return hashlib.sha256(json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
    ).encode("utf-8")).hexdigest()


async def require_goal(conn, goal_id, tenant_id):
    # Hold tenant ownership stable until the creation/reservation transaction commits.
    row = await conn.fetchrow(
        "SELECT id FROM goals WHERE id = $1::uuid AND tenant_id = $2::uuid FOR SHARE",
        goal_id, tenant_id,
    )
    if row is None:
        raise MilestoneGoalNotFound()


async def create_milestone(pool, *, tenant_id, goal_id, key, payload):
    if not tenant_id:
        raise ValueError("authenticated tenant_id is required for idempotency")
    if not isinstance(key, str) or not key.strip() or len(key) > 200:
        raise ValueError("idempotency_key must contain 1..200 characters and not be blank")
    # Explicit defaults are part of the versioned contract; unsupported hierarchy fields
    # must never be accepted and silently omitted from the fingerprint or persistence.
    if set(payload) != {"title", "sequence", "completion_criteria", "auto_advance",
                        "owner_session_id", "owner_role_key"}:
        raise ValueError("unsupported milestone payload")
    digest = fingerprint(payload)
    mid = str(uuid.uuid4())
    response = {"milestone_id": mid, "status": "pending",
                "owner_session_id": payload["owner_session_id"],
                "owner_role_key": payload["owner_role_key"]}
    async with pool.acquire() as conn, conn.transaction():
        await require_goal(conn, goal_id, tenant_id)
        reserved = await conn.fetchval(
            """INSERT INTO milestone_idempotency
               (tenant_id, goal_id, idempotency_key, payload_fingerprint, milestone_id, response)
               VALUES ($1::uuid, $2::uuid, $3, $4, $5::uuid, $6::jsonb)
               ON CONFLICT (tenant_id, goal_id, idempotency_key) DO NOTHING
               RETURNING milestone_id""",
            tenant_id, goal_id, key, digest, mid, json.dumps(response),
        )
        if reserved is None:
            # Under READ COMMITTED this statement sees the winning transaction after
            # ON CONFLICT waited for it. A failed winner frees the reservation instead.
            row = await conn.fetchrow(
                """SELECT payload_fingerprint, response FROM milestone_idempotency
                   WHERE tenant_id = $1::uuid AND goal_id = $2::uuid AND idempotency_key = $3""",
                tenant_id, goal_id, key,
            )
            if row is None:
                raise RuntimeError("idempotency reservation disappeared; retry transaction")
            if row["payload_fingerprint"] != digest:
                raise MilestoneConflict()
            return json.loads(row["response"]) if isinstance(row["response"], str) else dict(row["response"])
        await conn.execute(
            """INSERT INTO milestones
               (id, goal_id, tenant_id, title, sequence_order, completion_criteria,
                auto_advance, status, owner_session_id, owner_role_key)
               VALUES ($1::uuid, $2::uuid, $3::uuid, $4, $5, $6, $7, 'pending', $8::uuid, $9)""",
            mid, goal_id, tenant_id, payload["title"], payload["sequence"],
            payload["completion_criteria"], payload["auto_advance"],
            payload["owner_session_id"], payload["owner_role_key"],
        )
    return response
