"""Disposable PostgreSQL proof for W-14a persistence invariants."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import pytest

asyncpg = pytest.importorskip("asyncpg")

from app.services.goal_policy_rollout import promote_policy, simulate_policy
from app.services.goal_work_hierarchy import ActorScope
from tests.integration.test_goal_policy_foundation_migration import (
    BASELINE,
    M12,
    M14,
    UP,
    W13,
    W14F,
    _fresh_database_variant,
    _seed_all_foundation_stores,
)

DATABASE_URL = os.getenv("M12_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="M12_TEST_DATABASE_URL is not configured")
ROOT = Path(__file__).parents[2]
W14A = (ROOT / "migrations/20260919_goal_workflow_w14a.sql").read_text()
W14B = (ROOT / "migrations/20260919_goal_workflow_w14b.sql").read_text()
W14B_RLS = (ROOT / "migrations/20260919_goal_workflow_w14b_rls_fix.sql").read_text()
W14C = (ROOT / "migrations/20260919_goal_policy_rollout_w14c.sql").read_text()


def _url() -> str:
    if not urlparse(DATABASE_URL).path.removeprefix("/").endswith("_test"):
        pytest.fail("M12_TEST_DATABASE_URL database name must end in _test")
    return DATABASE_URL


async def _exercise() -> None:
    conn = await asyncpg.connect(_url())
    tenant = uuid4()
    principal, reviewer_a, reviewer_b = uuid4(), uuid4(), uuid4()
    try:
        assert await conn.fetchval(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'"
        ) == 0
        await conn.execute(BASELINE)
        await conn.execute("INSERT INTO tenants VALUES($1)", tenant)
        await conn.execute(
            "INSERT INTO chat_sessions VALUES($1,$4),($2,$4),($3,$4)",
            principal, reviewer_a, reviewer_b, tenant,
        )
        await conn.execute(_fresh_database_variant(M12))
        await conn.execute(_fresh_database_variant(M14))
        await conn.execute(UP)
        await conn.execute(W13)
        await conn.execute(W14F)

        policy_id = await conn.fetchval(
            """INSERT INTO goal_approval_policy_versions
               (tenant_id,policy,policy_hash,created_by)
               VALUES($1,'{}',$2,$3) RETURNING id""",
            tenant, "sha256:" + "1" * 64, principal,
        )
        decision_id = uuid4()
        await conn.execute(
            """INSERT INTO goal_policy_decisions
               (id,tenant_id,project,workspace_kind,principal_session_id,target_type,target_id,
                action,base_version,environment,boundary_decision,approval_route,
                automation_eligibility,risk_tier,result,policy_version,decision_input_hash,
                precondition_snapshot_hash,canonicalization_version,hash_algorithm,
                effective_application_result,masking_policy_version,kill_switch_epoch,
                deny_policy_epoch,assignment_epoch,grant_revocation_epoch,target_version,
                signature_algorithm,signature_key_id,signature_key_version,signature)
               VALUES($1,$2,'AADS','project',$3,'task',$4,'update',1,'dev','DENY','NONE',
                      'NOT_EXECUTABLE','A1','DENY',$5,$6,$7,'RFC8785','SHA-256','DENY',1,
                      0,0,0,0,1,'test','test-key',1,'signature')""",
            decision_id, tenant, principal, uuid4(), policy_id,
            "sha256:" + "2" * 64, "sha256:" + "3" * 64,
        )
        await _seed_all_foundation_stores(
            conn,
            tenant_id=tenant,
            principal_session_id=principal,
            reviewer_session_id=reviewer_a,
            policy_id=policy_id,
            decision_id=decision_id,
        )
        await conn.execute(W14A)
        await conn.execute(W14A)
        await conn.execute(W14B)
        await conn.execute(W14B)
        await conn.execute(W14B_RLS)
        await conn.execute(W14B_RLS)
        await conn.execute(W14C)
        await conn.execute(W14C)
        assert await conn.fetchval(
            "SELECT count(*) FROM information_schema.tables WHERE table_name LIKE 'goal_policy_simulation_%'"
        ) == 2
        rls = await conn.fetchrow(
            "SELECT relrowsecurity,relforcerowsecurity FROM pg_class WHERE relname='goal_auto_approval_usage_events'"
        )
        assert rls["relrowsecurity"] is True and rls["relforcerowsecurity"] is True
        project_actor = ActorScope(str(tenant), str(principal), "project_lead", "AADS", "project")
        simulation = await simulate_policy(
            conn, tenant_id=str(tenant), actor=project_actor,
            candidate_policy_id=str(policy_id), decision_ids=[str(decision_id)],
        )
        assert simulation["sample_count"] == 1
        assert simulation["widened_count"] == 0
        ceo_actor = ActorScope(str(tenant), str(principal), "ceo", "AADS", "ceo_integrated")
        rollout = await promote_policy(
            conn, tenant_id=str(tenant), actor=ceo_actor, policy_id=str(policy_id),
            target_mode="canary", reason="w14c integration proof",
        )
        assert rollout["mode"] == "canary"
        assert await conn.fetchval(
            "SELECT count(*) FROM goal_policy_rollout_events WHERE policy_version_id=$1", policy_id
        ) == 1
        change_set = await conn.fetchval(
            "SELECT id FROM work_item_change_sets WHERE tenant_id=$1 LIMIT 1", tenant
        )

        permission_a, permission_b = uuid4(), uuid4()
        await conn.execute(
            "INSERT INTO agent_permission_requests(id,tenant_id) VALUES($1,$3),($2,$3)",
            permission_a, permission_b, tenant,
        )
        route_a, route_b = await conn.fetch(
            """INSERT INTO work_item_change_set_approval_routes
               (tenant_id,project,change_set_id,route_key,required_role,approval_request_id)
               VALUES($1,'AADS',$2,'project_lead','project_lead',$3),
                     ($1,'AADS',$2,'independent_reviewer','independent_reviewer',$4)
               RETURNING id""",
            tenant, change_set, permission_a, permission_b,
        )
        await conn.execute(
            """INSERT INTO work_item_change_set_approval_decisions
               (tenant_id,project,change_set_id,route_id,actor_session_id,decision)
               VALUES($1,'AADS',$2,$3,$5,'approved'),
                     ($1,'AADS',$2,$4,$6,'approved')""",
            tenant, change_set, route_a["id"], route_b["id"], reviewer_a, reviewer_b,
        )
        assert await conn.fetchval(
            "SELECT count(*) FROM work_item_change_set_approval_decisions WHERE change_set_id=$1",
            change_set,
        ) == 2

        execution_key = "w14a-rollback-proof"
        with pytest.raises(RuntimeError, match="force rollback"):
            async with conn.transaction():
                await conn.execute(
                    """INSERT INTO goal_workflow_effects
                       (tenant_id,project,execution_key,change_set_id,owner_instance,owner_epoch,
                        effect_kind,state)
                       VALUES($1,'AADS',$2,$3,'test-owner',1,'internal','applying')""",
                    tenant, execution_key, change_set,
                )
                raise RuntimeError("force rollback")
        assert await conn.fetchval(
            "SELECT count(*) FROM goal_workflow_effects WHERE execution_key=$1", execution_key
        ) == 0

        await conn.execute(
            """INSERT INTO goal_workflow_effects
               (tenant_id,project,execution_key,change_set_id,owner_instance,owner_epoch,
                effect_kind,state)
               VALUES($1,'AADS',$2,$3,'test-owner',1,'internal','applied')""",
            tenant, execution_key, change_set,
        )
        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute(
                """INSERT INTO goal_workflow_effects
                   (tenant_id,project,execution_key,change_set_id,owner_instance,owner_epoch,
                    effect_kind,state)
                   VALUES($1,'AADS',$2,$3,'other-owner',2,'internal','applied')""",
                tenant, execution_key, change_set,
            )

        grant = await conn.fetchrow(
            "SELECT id,grant_version,max_executions FROM goal_auto_approval_grants WHERE tenant_id=$1 LIMIT 1", tenant
        )
        target = await conn.fetchrow(
            "SELECT id,type,version FROM work_items WHERE tenant_id=$1 LIMIT 1", tenant
        )
        await conn.execute(
            "UPDATE goal_auto_approval_grants SET used_executions=max_executions-1 WHERE id=$1", grant["id"]
        )

        async def reserve_once(key: str) -> bool:
            worker = await asyncpg.connect(_url())
            try:
                async with worker.transaction():
                    used = await worker.fetchval(
                        """UPDATE goal_auto_approval_grants SET used_executions=used_executions+1
                             WHERE id=$1 AND used_executions<max_executions RETURNING used_executions""",
                        grant["id"],
                    )
                    if used is None:
                        return False
                    await worker.execute(
                        """INSERT INTO goal_auto_approval_uses
                           (tenant_id,decision_id,execution_key,grant_id,grant_version,input_hash,
                            target_type,target_id,target_version,action,status,correlation_id)
                           VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,'update','reserved',$10)""",
                        tenant, uuid4(), key, grant["id"], grant["grant_version"],
                        "sha256:" + "8" * 64, target["type"], target["id"], target["version"], uuid4(),
                    )
                    return True
            finally:
                await worker.close()

        wins = await asyncio.gather(reserve_once("w14b-race-a"), reserve_once("w14b-race-b"))
        assert wins.count(True) == 1
        assert await conn.fetchval(
            "SELECT used_executions FROM goal_auto_approval_grants WHERE id=$1", grant["id"]
        ) == grant["max_executions"]
    finally:
        await conn.close()


def test_w14a_migration_twice_multi_approval_rollback_and_exactly_once() -> None:
    asyncio.run(_exercise())
