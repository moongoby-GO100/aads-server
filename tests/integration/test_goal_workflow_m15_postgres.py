"""Disposable PostgreSQL proof for M15 hierarchy details and bounded retry."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import pytest

asyncpg = pytest.importorskip("asyncpg")

from app.services.goal_work_hierarchy import ActorScope, get_goal_tree
from app.services.goal_workflow_approval import approval_preview, request_outbox_retry
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
W14S = (ROOT / "migrations/20260919_goal_work_hierarchy_m14_security.sql").read_text()
W14A = (ROOT / "migrations/20260919_goal_workflow_w14a.sql").read_text()
W14B = (ROOT / "migrations/20260919_goal_workflow_w14b.sql").read_text()


def _url() -> str:
    if not urlparse(DATABASE_URL).path.removeprefix("/").endswith("_test"):
        pytest.fail("M12_TEST_DATABASE_URL database name must end in _test")
    return DATABASE_URL


async def _exercise() -> None:
    conn = await asyncpg.connect(_url())
    tenant, actor_id = uuid4(), uuid4()
    goal_id, milestone_id, item_id, change_id = uuid4(), uuid4(), uuid4(), uuid4()
    actor = ActorScope(str(tenant), str(actor_id), "CEO", "AADS", "ceo_integrated")
    baseline = BASELINE.replace(
        "CREATE TABLE milestones(id UUID PRIMARY KEY DEFAULT gen_random_uuid(),goal_id UUID NOT NULL REFERENCES goals(id),title TEXT NOT NULL);",
        "CREATE TABLE milestones(id UUID PRIMARY KEY DEFAULT gen_random_uuid(),goal_id UUID NOT NULL REFERENCES goals(id),title TEXT NOT NULL,sequence_order INTEGER NOT NULL DEFAULT 0);",
    )
    try:
        assert await conn.fetchval(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'"
        ) == 0
        await conn.execute(baseline)
        await conn.execute("INSERT INTO tenants VALUES($1)", tenant)
        await conn.execute("INSERT INTO chat_sessions VALUES($1,$2)", actor_id, tenant)
        await conn.execute(_fresh_database_variant(M12))
        await conn.execute(_fresh_database_variant(M14))
        await conn.execute(UP)
        await conn.execute(W13)
        await conn.execute(W14F)
        await conn.execute(W14S)
        await conn.execute(W14A)
        await conn.execute(W14B)
        await conn.execute("SELECT set_config('app.current_tenant_id',$1,false)", str(tenant))

        await conn.execute(
            "INSERT INTO goals(id,title,project,status,tenant_id) VALUES($1,'Goal','AADS','active',$2)",
            goal_id, tenant,
        )
        await conn.execute(
            """INSERT INTO milestones(id,goal_id,title,sequence_order,tenant_id,project)
               VALUES($1,$2,'Milestone',15,$3,'AADS')""",
            milestone_id, goal_id, tenant,
        )
        await conn.execute(
            """INSERT INTO work_items
               (id,tenant_id,project,goal_id,milestone_id,type,title,status,progress,
                idempotency_key,created_by)
               VALUES($1,$2,'AADS',$3,$4,'epic','Epic','ready',25,'m15-item',$5)""",
            item_id, tenant, goal_id, milestone_id, actor_id,
        )
        await conn.execute(
            """INSERT INTO work_item_evidence
               (tenant_id,project,goal_id,work_item_id,evidence_type,uri,verified,
                work_item_version,criterion_key,artifact_hash,created_by)
               VALUES($1,'AADS',$2,$3,'test','urn:test:m15',true,1,'T15',$4,$5)""",
            tenant, goal_id, item_id, "sha256:" + "e" * 64, actor_id,
        )
        await conn.execute(
            """INSERT INTO work_item_change_sets
               (id,tenant_id,project,target_type,target_id,action,base_version,target_version,
                patch,patch_hash,body_hash,risk_factors,rationale,expected_effect,rollback_plan,
                risk_tier,state,idempotency_key,requested_by,environment)
               VALUES($1,$2,'AADS','epic',$3,'update',1,2,$4::jsonb,$5,$6,'{}',
                      'why','effect','undo','A2','executed','m15-change',$7,'dev')""",
            change_id, tenant, item_id,
            '[{"op":"replace","path":"/title","value":"Epic after"}]',
            "sha256:" + "a" * 64, "sha256:" + "b" * 64, actor_id,
        )
        await conn.execute(
            """INSERT INTO goal_workflow_outbox
               (tenant_id,project,change_set_id,execution_key,event_type,status,attempts,
                owner_instance,owner_epoch,last_error)
               VALUES($1,'AADS',$2,'m15-exec','change_set.execute','failed',2,'worker',1,'network timeout')""",
            tenant, change_id,
        )

        tree = await get_goal_tree(
            conn, tenant_id=str(tenant), goal_id=str(goal_id),
            includes=["approvals", "dependencies", "evidence"],
        )
        root = tree["items"][0]
        assert root["milestone_sequence"] == 15
        assert root["evidence"]["items"][0]["criterion_key"] == "T15"
        assert root["last_error"] == "network timeout"
        assert root["recovery"]["can_retry"] is True

        preview = await approval_preview(
            conn, tenant_id=str(tenant), item_id=str(item_id), actor=actor,
        )
        assert preview["approval"]["patch"][0]["value"] == "Epic after"
        assert preview["approval"]["rollback_plan"] == "undo"

        async with conn.transaction():
            retried = await request_outbox_retry(
                conn, tenant_id=str(tenant), item_id=str(item_id), actor=actor,
                reason="operator verified transient failure",
            )
        assert retried["state"] == "retry_pending"
        assert await conn.fetchval(
            "SELECT status FROM goal_workflow_outbox WHERE change_set_id=$1", change_id
        ) == "pending"
        assert await conn.fetchval(
            """SELECT count(*) FROM work_item_events
                 WHERE aggregate_id=$1 AND event_type='outbox_retry_requested'""",
            change_id,
        ) == 1
    finally:
        await conn.close()


def test_m15_hierarchy_diff_evidence_and_retry() -> None:
    asyncio.run(_exercise())
