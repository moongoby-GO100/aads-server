"""Disposable PostgreSQL proof for W-14a persistence invariants."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import pytest
from fastapi import HTTPException

asyncpg = pytest.importorskip("asyncpg")

from app.services.goal_work_hierarchy import ActorScope
from app.services.goal_workflow_approval import reserve_grant_use, revoke_grant
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


def _url() -> str:
    if not urlparse(DATABASE_URL).path.removeprefix("/").endswith("_test"):
        pytest.fail("M12_TEST_DATABASE_URL database name must end in _test")
    return DATABASE_URL


async def _exercise() -> None:
    conn = await asyncpg.connect(_url())
    old_workflow_flag = os.environ.get("GOAL_WORKFLOW_APPROVAL_ENABLED")
    old_auto_mode = os.environ.get("GOAL_AUTO_APPROVAL_MODE")
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
               (tenant_id,project,mode,effective_at,policy,policy_hash,created_by)
               VALUES($1,'AADS','canary',clock_timestamp(),'{}',$2,$3) RETURNING id""",
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
            "SELECT id,grant_version FROM goal_auto_approval_grants WHERE tenant_id=$1 LIMIT 1", tenant
        )
        target = await conn.fetchrow(
            "SELECT id,goal_id,type,version FROM work_items WHERE tenant_id=$1 LIMIT 1", tenant
        )
        os.environ["GOAL_WORKFLOW_APPROVAL_ENABLED"] = "true"
        os.environ["GOAL_AUTO_APPROVAL_MODE"] = "on"
        actor_scope = ActorScope(str(tenant), str(principal), "goal_test_owner", "AADS", "project")
        request = {
            "project": "AADS", "goal_id": str(target["goal_id"]),
            "target_id": str(target["id"]), "target_type": str(target["type"]),
            "target_version": int(target["version"]), "action": "update",
            "execution_key": "w14b-service-first", "environment": "dev",
            "risk_tier": "A1", "tool_groups": [],
        }
        denied = await reserve_grant_use(
            conn, tenant_id=str(tenant), actor=actor_scope,
            request={**request, "execution_key": "denied",
                     "risk_tier": "A2", "tool_groups": ["shell"]},
        )
        assert denied["decision"] == "PROJECT_APPROVAL"
        assert await conn.fetchval(
            "SELECT used_executions FROM goal_auto_approval_grants WHERE id=$1", grant["id"]
        ) == 0
        first = await reserve_grant_use(
            conn,
            tenant_id=str(tenant),
            actor=actor_scope,
            request=request,
        )
        assert first["decision"] == "AUTO"
        replay = await reserve_grant_use(
            conn, tenant_id=str(tenant), actor=actor_scope, request=request,
        )
        assert replay["reason_codes"] == ["idempotent_replay"]
        with pytest.raises(HTTPException) as conflict:
            await reserve_grant_use(
                conn, tenant_id=str(tenant), actor=actor_scope,
                request={**request, "estimated_files": 1},
            )
        assert conflict.value.status_code == 409

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
        ) == 2

        before_epoch = await conn.fetchval(
            "SELECT revocation_epoch FROM goal_auto_approval_grants WHERE id=$1", grant["id"]
        )
        with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError):
            async with conn.transaction():
                await conn.execute(
                    "UPDATE goal_auto_approval_grants SET max_executions=3 WHERE id=$1", grant["id"]
                )
        await conn.execute(
            "UPDATE goal_auto_approval_grants SET status='revoked' WHERE id=$1", grant["id"]
        )
        assert await conn.fetchval(
            "SELECT revocation_epoch FROM goal_auto_approval_grants WHERE id=$1", grant["id"]
        ) == before_epoch + 1

        delegated_assignment = uuid4()
        await conn.execute(
            """INSERT INTO project_role_assignments
               (id,tenant_id,project,role_key,session_id,active)
               VALUES($1,$2,'AADS','goal_test_delegate',$3,true)""",
            delegated_assignment, tenant, reviewer_b,
        )
        parent_grant = await conn.fetchval(
            """INSERT INTO goal_auto_approval_grants
               (tenant_id,project,principal_session_id,assignment_id,goal_id,milestone_id,
                actions,tool_groups,max_risk_tier,environments,conditions,max_executions,
                max_files,max_rows,max_cost_usd,max_parallel,max_duration_seconds,
                valid_from,expires_at,delegation_depth,policy_version,scope_hash,
                revocation_strategy,requested_by,issued_by,approved_by)
               SELECT $1,'AADS',$2,assignment_id,goal_id,milestone_id,
                      ARRAY['update'],ARRAY['shell'],'A1',ARRAY['dev'],'{"region":"test"}',2,
                      2,2,2,1,60,clock_timestamp(),clock_timestamp()+interval '1 hour',1,
                      $3,$4,'cancel_now',$5,$5,$6
                 FROM goal_auto_approval_grants WHERE id=$7
               RETURNING id""",
            tenant, principal, policy_id, "sha256:" + "a" * 64,
            reviewer_a, reviewer_b, grant["id"],
        )
        child_grant = await conn.fetchval(
            """INSERT INTO goal_auto_approval_grants
               (tenant_id,project,principal_session_id,assignment_id,goal_id,milestone_id,
                actions,tool_groups,max_risk_tier,environments,conditions,max_executions,
                max_files,max_rows,max_cost_usd,max_parallel,max_duration_seconds,
                valid_from,expires_at,delegation_depth,parent_grant_id,policy_version,scope_hash,
                revocation_strategy,requested_by,issued_by,approved_by)
               SELECT $1,'AADS',$2,$3,goal_id,milestone_id,
                      ARRAY['update'],ARRAY['shell'],'A1',ARRAY['dev'],
                      '{"region":"test","narrow":true}',1,1,1,1,1,30,
                      clock_timestamp(),clock_timestamp()+interval '30 minutes',0,$4,$5,$6,
                      'compensate',$7,$8,$9
                 FROM goal_auto_approval_grants WHERE id=$10
               RETURNING id""",
            tenant, reviewer_b, delegated_assignment, parent_grant, policy_id,
            "sha256:" + "b" * 64, reviewer_a, principal, reviewer_a, grant["id"],
        )
        delegated = await reserve_grant_use(
            conn, tenant_id=str(tenant),
            actor=ActorScope(str(tenant), str(reviewer_b), "goal_test_delegate", "AADS", "project"),
            request={**request, "execution_key": "w14b-delegated", "tool_groups": ["shell"]},
        )
        assert delegated["decision"] == "AUTO"
        counters = await conn.fetch(
            """SELECT id,used_executions FROM goal_auto_approval_grants
                 WHERE id=ANY($1::uuid[]) ORDER BY id""",
            [parent_grant, child_grant],
        )
        assert {row["id"]: row["used_executions"] for row in counters} == {
            parent_grant: 1, child_grant: 1,
        }
        await revoke_grant(
            conn, tenant_id=str(tenant), grant_id=str(parent_grant),
            actor=ActorScope(str(tenant), str(reviewer_a), "CEO", "AADS", "ceo_integrated"),
            reason="integration test",
        )
        states = await conn.fetch(
            "SELECT id,status,revocation_epoch FROM goal_auto_approval_grants WHERE id=ANY($1::uuid[])",
            [parent_grant, child_grant],
        )
        assert {(row["status"], row["revocation_epoch"]) for row in states} == {
            ("revoked", 1),
        }
        assert await conn.fetchval(
            "SELECT status FROM goal_auto_approval_uses WHERE execution_key='w14b-delegated'"
        ) == "manual_reconciliation"
    finally:
        if old_workflow_flag is None:
            os.environ.pop("GOAL_WORKFLOW_APPROVAL_ENABLED", None)
        else:
            os.environ["GOAL_WORKFLOW_APPROVAL_ENABLED"] = old_workflow_flag
        if old_auto_mode is None:
            os.environ.pop("GOAL_AUTO_APPROVAL_MODE", None)
        else:
            os.environ["GOAL_AUTO_APPROVAL_MODE"] = old_auto_mode
        await conn.close()


def test_w14a_migration_twice_multi_approval_rollback_and_exactly_once() -> None:
    asyncio.run(_exercise())
