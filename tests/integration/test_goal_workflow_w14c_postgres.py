"""Disposable PostgreSQL proof for W-14c replay, promotion, and rollback."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import pytest
from fastapi import HTTPException

asyncpg = pytest.importorskip("asyncpg")

from app.services.goal_policy_shadow import promote_policy, replay_policy, rollback_policy
from app.services.goal_work_hierarchy import ActorScope
from tests.integration.test_goal_policy_foundation_migration import (
    BASELINE,
    M12,
    M14,
    UP,
    W13,
    W14F,
    _fresh_database_variant,
)

DATABASE_URL = os.getenv("M12_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="M12_TEST_DATABASE_URL is not configured")
ROOT = Path(__file__).parents[2]
W14A = (ROOT / "migrations/20260919_goal_workflow_w14a.sql").read_text()
W14B = (ROOT / "migrations/20260919_goal_workflow_w14b.sql").read_text()
W14C = (ROOT / "migrations/20260919_goal_workflow_w14c.sql").read_text()


def _url() -> str:
    if not urlparse(DATABASE_URL).path.removeprefix("/").endswith("_test"):
        pytest.fail("M12_TEST_DATABASE_URL database name must end in _test")
    return DATABASE_URL


async def _exercise() -> None:
    conn = await asyncpg.connect(_url())
    tenant, actor_id = uuid4(), uuid4()
    actor = ActorScope(str(tenant), str(actor_id), "CEO", "AADS", "ceo_integrated")
    try:
        assert await conn.fetchval(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'"
        ) == 0
        await conn.execute(BASELINE)
        await conn.execute("INSERT INTO tenants VALUES($1)", tenant)
        await conn.execute("INSERT INTO chat_sessions VALUES($1,$2)", actor_id, tenant)
        await conn.execute(_fresh_database_variant(M12))
        await conn.execute(_fresh_database_variant(M14))
        await conn.execute(UP)
        await conn.execute(W13)
        await conn.execute(W14F)
        await conn.execute(W14A)
        await conn.execute(W14B)
        await conn.execute(W14C)
        await conn.execute(W14C)

        expanding_policy = await conn.fetchval(
            """INSERT INTO goal_approval_policy_versions
               (tenant_id,project,policy,policy_hash,created_by,mode)
               VALUES($1,'AADS',$2::jsonb,$3,$4,'audit_only') RETURNING id""",
            tenant, '{"default_decision":"AUTO"}', "sha256:" + "c" * 64, actor_id,
        )
        await conn.execute(
            """INSERT INTO goal_approval_decision_logs
               (tenant_id,decision,reason_codes,input_context)
               VALUES($1,'PROJECT_APPROVAL','{}',$2::jsonb)""",
            tenant,
            '{"project":"AADS","action":"update","environment":"dev",'
            '"target_type":"task","risk_tier":"A1","token":"raw-secret-value"}',
        )
        expanding_run = await replay_policy(
            conn, tenant_id=str(tenant), policy_id=str(expanding_policy), actor=actor,
        )
        assert expanding_run["metrics"]["privilege_expansion_count"] == 1
        assert expanding_run["operational_counts"]["before"] == expanding_run["operational_counts"]["after"]
        evidence = await conn.fetchrow(
            """SELECT masked_context::text,erased,masked FROM goal_policy_simulation_items
                 WHERE run_id=$1::uuid""",
            expanding_run["run_id"],
        )
        assert "raw-secret-value" not in evidence["masked_context"]
        assert evidence["erased"] or evidence["masked"]
        with pytest.raises(HTTPException) as blocked:
            await promote_policy(
                conn, tenant_id=str(tenant), policy_id=str(expanding_policy),
                replay_run_id=expanding_run["run_id"], target_mode="canary",
                reason="must fail", actor=actor,
            )
        assert blocked.value.status_code == 409

        safe_policy = await conn.fetchval(
            """INSERT INTO goal_approval_policy_versions
               (tenant_id,project,policy,policy_hash,created_by,mode)
               VALUES($1,'AADS',$2::jsonb,$3,$4,'audit_only') RETURNING id""",
            tenant, '{"default_decision":"DENY"}', "sha256:" + "d" * 64, actor_id,
        )
        safe_run = await replay_policy(
            conn, tenant_id=str(tenant), policy_id=str(safe_policy), actor=actor,
        )
        assert safe_run["metrics"]["privilege_expansion_count"] == 0
        promoted = await promote_policy(
            conn, tenant_id=str(tenant), policy_id=str(safe_policy),
            replay_run_id=safe_run["run_id"], target_mode="canary",
            reason="zero expansion canary", actor=actor,
        )
        assert promoted["mode"] == "canary"
        rolled_back = await rollback_policy(
            conn, tenant_id=str(tenant), policy_id=promoted["policy_version"],
            rollback_policy_id=str(safe_policy), reason="canary regression", actor=actor,
        )
        assert rolled_back["mode"] == "audit_only"
        assert await conn.fetchval(
            "SELECT count(*) FROM goal_policy_promotion_events WHERE tenant_id=$1", tenant
        ) == 2
        with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError):
            await conn.execute(
                "UPDATE goal_policy_simulation_runs SET input_count=99 WHERE id=$1::uuid",
                safe_run["run_id"],
            )
    finally:
        await conn.close()


def test_w14c_historical_replay_masking_canary_and_rollback() -> None:
    asyncio.run(_exercise())
