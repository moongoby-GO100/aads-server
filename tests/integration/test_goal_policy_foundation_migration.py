"""Disposable PostgreSQL checks for W-12b/W-12c.

Set M12_TEST_DATABASE_URL to a newly-created, empty database whose name ends in
``_test``.  This test never destroys or resets an existing schema.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import pytest


asyncpg = pytest.importorskip("asyncpg")
DATABASE_URL = os.getenv("M12_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="M12_TEST_DATABASE_URL is not configured")
ROOT = Path(__file__).parents[2]
M12 = (ROOT / "migrations/20260919_goal_work_hierarchy_m12.sql").read_text()
M14 = (ROOT / "migrations/20260919_goal_work_hierarchy_m14.sql").read_text()
UP = (ROOT / "migrations/20260919_goal_policy_foundation_stores.sql").read_text()
W13 = (ROOT / "migrations/20260919_goal_policy_preconditions_w13.sql").read_text()
VERIFY = (ROOT / "migrations/20260919_goal_policy_foundation_stores.verify.sql").read_text()
DOWN = (ROOT / "migrations/rollback/20260919_goal_policy_foundation_stores.down.sql").read_text()
FOUNDATION_STORES = (
    "work_item_dependencies",
    "work_item_evidence",
    "goal_kill_switches",
    "goal_policy_decisions",
    "goal_workflow_outbox",
    "goal_execution_leases",
    "goal_auto_approval_use_reservations",
    "goal_auto_approval_use_events",
    "work_item_review_requirements",
    "work_item_review_decisions",
)


def _fresh_database_variant(sql: str) -> str:
    """Remove only idempotent trigger drops from legacy bootstrap migrations.

    The disposable database is asserted empty before this runs, so those legacy
    DROP TRIGGER statements are unnecessary.  The W-12b migration itself is
    always executed byte-for-byte and contains no destructive statement.
    """
    kept: list[str] = []
    for line in sql.splitlines():
        if line.lstrip().startswith("DROP TRIGGER IF EXISTS"):
            continue
        if line.lstrip().startswith(("DROP ", "TRUNCATE ")):
            pytest.fail(f"unexpected destructive bootstrap statement: {line.strip()}")
        kept.append(line)
    return "\n".join(kept)

BASELINE = """
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE TABLE tenants(id UUID PRIMARY KEY);
CREATE TABLE chat_sessions(id UUID PRIMARY KEY,tenant_id UUID NOT NULL REFERENCES tenants(id),UNIQUE(id,tenant_id));
CREATE TABLE goals(id UUID PRIMARY KEY DEFAULT gen_random_uuid(),title TEXT NOT NULL,project TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'active');
CREATE TABLE milestones(id UUID PRIMARY KEY DEFAULT gen_random_uuid(),goal_id UUID NOT NULL REFERENCES goals(id),title TEXT NOT NULL);
CREATE TABLE agent_permission_requests(id UUID PRIMARY KEY DEFAULT gen_random_uuid(),tenant_id UUID NOT NULL REFERENCES tenants(id));
CREATE OR REPLACE FUNCTION public.aads_internal_tenant_id() RETURNS UUID LANGUAGE SQL STABLE
AS $$ SELECT id FROM tenants ORDER BY id LIMIT 1 $$;
"""


def _url() -> str:
    if not urlparse(DATABASE_URL).path.removeprefix("/").endswith("_test"):
        pytest.fail("M12_TEST_DATABASE_URL database name must end in _test")
    return DATABASE_URL


async def _set_tenant(conn, tenant_id) -> None:
    await conn.execute("SELECT set_config('app.current_tenant_id',$1,true)", str(tenant_id))


async def _seed_all_foundation_stores(
    conn, *, tenant_id, principal_session_id, reviewer_session_id, policy_id, decision_id
) -> None:
    goal_id, milestone_id = uuid4(), uuid4()
    assignment_id, first_item_id, second_item_id = uuid4(), uuid4(), uuid4()
    change_set_id, grant_id, reservation_id, requirement_id = uuid4(), uuid4(), uuid4(), uuid4()
    await conn.execute(
        "INSERT INTO goals(id,title,project,status,tenant_id) VALUES($1,'test goal','AADS','active',$2)",
        goal_id, tenant_id,
    )
    await conn.execute(
        "INSERT INTO milestones(id,goal_id,title,tenant_id,project) VALUES($1,$2,'test milestone',$3,'AADS')",
        milestone_id, goal_id, tenant_id,
    )
    await conn.execute(
        """INSERT INTO project_role_assignments
           (id,tenant_id,project,role_key,session_id,active)
           VALUES($1,$2,'AADS','goal_test_owner',$3,true)""",
        assignment_id, tenant_id, principal_session_id,
    )
    await conn.execute(
        """INSERT INTO work_items
           (id,tenant_id,project,goal_id,milestone_id,type,title,idempotency_key,created_by)
           VALUES($1,$2,'AADS',$3,$4,'epic','first','first',$5),
                 ($6,$2,'AADS',$3,$4,'epic','second','second',$5)""",
        first_item_id, tenant_id, goal_id, milestone_id, principal_session_id, second_item_id,
    )
    await conn.execute(
        """INSERT INTO work_item_dependencies
           (tenant_id,project,goal_id,work_item_id,depends_on_id,created_by)
           VALUES($1,'AADS',$2,$3,$4,$5)""",
        tenant_id, goal_id, second_item_id, first_item_id, principal_session_id,
    )
    await conn.execute(
        """INSERT INTO work_item_evidence
           (tenant_id,project,goal_id,work_item_id,evidence_type,uri,content_hash,
            work_item_version,criterion_key,artifact_hash,created_by)
           VALUES($1,'AADS',$2,$3,'test','urn:test:evidence',$4,1,'criterion-1',$4,$5)""",
        tenant_id, goal_id, first_item_id, "sha256:" + "4" * 64, principal_session_id,
    )
    await conn.execute(
        """INSERT INTO work_item_change_sets
           (id,tenant_id,project,target_type,target_id,action,base_version,patch,patch_hash,
            rationale,expected_effect,rollback_plan,risk_tier,idempotency_key,requested_by)
           VALUES($1,$2,'AADS','epic',$3,'update',1,'[]',$4,'test','test','test','A1','change-1',$5)""",
        change_set_id, tenant_id, first_item_id, "sha256:" + "5" * 64, principal_session_id,
    )
    await conn.execute(
        """INSERT INTO goal_auto_approval_grants
           (id,tenant_id,project,principal_session_id,assignment_id,goal_id,milestone_id,
            actions,max_risk_tier,environments,max_executions,valid_from,expires_at,
            policy_version,scope_hash,revocation_strategy,requested_by,issued_by,approved_by)
           VALUES($1,$2,'AADS',$3,$4,$5,$6,ARRAY['update'],'A1',ARRAY['dev'],2,
                  clock_timestamp(),clock_timestamp()+interval '1 hour',$7,$8,'finish_current',$9,$9,$9)""",
        grant_id, tenant_id, principal_session_id, assignment_id, goal_id, milestone_id,
        policy_id, "sha256:" + "6" * 64, reviewer_session_id,
    )
    await conn.execute(
        """INSERT INTO goal_workflow_outbox
           (tenant_id,project,change_set_id,decision_id,execution_key,event_type,payload,
            owner_instance,owner_epoch)
           VALUES($1,'AADS',$2,$3,'execution-1','change_set.execute','{}','test-owner',1)""",
        tenant_id, change_set_id, decision_id,
    )
    await conn.execute(
        """INSERT INTO goal_execution_leases
           (tenant_id,project,execution_key,decision_id,owner_instance,owner_epoch,expires_at)
           VALUES($1,'AADS','execution-1',$2,'test-owner',1,clock_timestamp()+interval '5 minutes')""",
        tenant_id, decision_id,
    )
    await conn.execute(
        """INSERT INTO goal_auto_approval_use_reservations
           (id,tenant_id,project,execution_key,decision_id,grant_id,grant_version,reservation_state)
           VALUES($1,$2,'AADS','execution-1',$3,$4,1,'reserved')""",
        reservation_id, tenant_id, decision_id, grant_id,
    )
    await conn.execute(
        """INSERT INTO goal_auto_approval_use_events
           (tenant_id,project,reservation_id,execution_key,decision_id,grant_id,grant_version,
            event_type,sequence_no)
           VALUES($1,'AADS',$2,'execution-1',$3,$4,1,'reserved',1)""",
        tenant_id, reservation_id, decision_id, grant_id,
    )
    await conn.execute(
        """INSERT INTO work_item_review_requirements
           (id,tenant_id,project,goal_id,work_item_id,work_item_version,required_role_key,
            minimum_approvals,review_order,sla_seconds)
           VALUES($1,$2,'AADS',$3,$4,1,'independent_reviewer',1,1,300)""",
        requirement_id, tenant_id, goal_id, first_item_id,
    )
    await conn.execute(
        """INSERT INTO work_item_review_decisions
           (tenant_id,project,goal_id,work_item_id,work_item_version,requirement_id,
            reviewer_session_id,reviewer_role_key,verdict,evidence_snapshot_hash,
            contributor_session_ids,assigned_session_id,evidence_submitter_session_ids)
           VALUES($1,'AADS',$2,$3,1,$4,$5,'independent_reviewer','accepted',$6,
                  ARRAY[$7::uuid],$7,ARRAY[$7::uuid])""",
        tenant_id, goal_id, first_item_id, requirement_id, reviewer_session_id,
        "sha256:" + "7" * 64, principal_session_id,
    )


async def _exercise() -> None:
    admin = await asyncpg.connect(_url())
    tenant_a, tenant_b = uuid4(), uuid4()
    try:
        existing = await admin.fetchval(
            """SELECT count(*) FROM information_schema.tables
                 WHERE table_schema='public'
                   AND table_name IN ('tenants','chat_sessions','goals','milestones',
                                      'agent_permission_requests','work_items',
                                      'goal_policy_decisions')"""
        )
        if existing:
            pytest.fail("M12_TEST_DATABASE_URL must identify a fresh empty test database")
        await admin.execute(BASELINE)
        await admin.execute("INSERT INTO tenants VALUES($1),($2)", tenant_a, tenant_b)
        session_a, session_b, reviewer_a = uuid4(), uuid4(), uuid4()
        await admin.execute(
            "INSERT INTO chat_sessions VALUES($1,$2),($3,$4),($5,$2)",
            session_a, tenant_a, session_b, tenant_b, reviewer_a,
        )
        await admin.execute(_fresh_database_variant(M12))
        await admin.execute(_fresh_database_variant(M14))
        await admin.execute(UP)
        await admin.execute(UP)
        await admin.execute(W13)
        await admin.execute(W13)
        await admin.execute(VERIFY)
        for table_name in ("goal_precondition_snapshots", "goal_policy_inputs"):
            assert await admin.fetchval("SELECT to_regclass($1) IS NOT NULL", table_name)
            security = await admin.fetchrow(
                "SELECT relrowsecurity,relforcerowsecurity FROM pg_class WHERE oid=$1::regclass",
                table_name,
            )
            assert security["relrowsecurity"] and security["relforcerowsecurity"]
        policy_id = await admin.fetchval(
            """INSERT INTO goal_approval_policy_versions
               (tenant_id,policy,policy_hash,created_by)
               VALUES($1,'{}',$2,$3) RETURNING id""",
            tenant_a, "sha256:" + "1" * 64, session_a,
        )
        await admin.execute(
            """DO $$ BEGIN
                 IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='aads_w12_runtime_test') THEN
                   CREATE ROLE aads_w12_runtime_test NOSUPERUSER NOBYPASSRLS NOLOGIN;
                 END IF;
               END $$;
               GRANT USAGE ON SCHEMA public TO aads_w12_runtime_test;
               GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA public TO aads_w12_runtime_test;"""
        )
        role_flags = await admin.fetchrow(
            "SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname='aads_w12_runtime_test'"
        )
        assert role_flags is not None
        assert not role_flags["rolsuper"]
        assert not role_flags["rolbypassrls"]
        switch_a = uuid4()
        await admin.execute("SET ROLE aads_w12_runtime_test")

        # Missing context is default deny for reads and writes.
        assert await admin.fetchval("SELECT count(*) FROM goal_kill_switches") == 0
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await admin.execute(
                """INSERT INTO goal_kill_switches
                   (id,tenant_id,project,scope_kind,epoch,reason,changed_by)
                   VALUES($1,$2,'AADS','project',1,'test',$3)""",
                switch_a, tenant_a, session_a,
            )

        async with admin.transaction():
            await _set_tenant(admin, tenant_a)
            await admin.execute(
                """INSERT INTO goal_kill_switches
                   (id,tenant_id,project,scope_kind,epoch,reason,changed_by)
                   VALUES($1,$2,'AADS','project',1,'test',$3)""",
                switch_a, tenant_a, session_a,
            )
            assert await admin.fetchval("SELECT count(*) FROM goal_kill_switches") == 1
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                async with admin.transaction():
                    await admin.execute(
                        """INSERT INTO goal_kill_switches
                           (tenant_id,project,scope_kind,epoch,reason,changed_by)
                           VALUES($1,'AADS','project',1,'cross',$2)""",
                        tenant_b, session_b,
                    )

        async with admin.transaction():
            await _set_tenant(admin, tenant_b)
            assert await admin.fetchval("SELECT count(*) FROM goal_kill_switches") == 0
            assert await admin.execute(
                "UPDATE goal_kill_switches SET reason='leak' WHERE id=$1", switch_a
            ) == "UPDATE 0"

        await admin.execute("RESET ROLE")

        # Two concurrent active records for one scope: exactly one succeeds.
        async def insert_active(epoch: int) -> bool:
            conn = await asyncpg.connect(_url())
            try:
                await conn.execute("SET ROLE aads_w12_runtime_test")
                async with conn.transaction():
                    await _set_tenant(conn, tenant_b)
                    await conn.execute(
                        """INSERT INTO goal_kill_switches
                           (tenant_id,project,scope_kind,epoch,reason,changed_by)
                           VALUES($1,'AADS','tenant',$2,'race',$3)""",
                        tenant_b, epoch, session_b,
                    )
                return True
            except (asyncpg.UniqueViolationError, asyncpg.CheckViolationError):
                return False
            finally:
                await conn.close()

        assert sorted(await asyncio.gather(insert_active(1), insert_active(2))) == [False, True]

        # Append-only enforcement is independent of application repositories.
        await admin.execute("SET ROLE aads_w12_runtime_test")
        async with admin.transaction():
            await _set_tenant(admin, tenant_a)
            decision_id = uuid4()
            await admin.execute(
                """INSERT INTO goal_policy_decisions
                   (id,tenant_id,project,workspace_kind,principal_session_id,target_type,target_id,
                    action,base_version,environment,boundary_decision,approval_route,
                    automation_eligibility,risk_tier,result,policy_version,decision_input_hash,
                    precondition_snapshot_hash,canonicalization_version,hash_algorithm,
                    effective_application_result,masking_policy_version,kill_switch_epoch,
                    deny_policy_epoch,assignment_epoch,grant_revocation_epoch,target_version,
                    signature_algorithm,signature)
                   VALUES($1,$2,'AADS','project',$3,'task',$4,'update',1,'dev','DENY','NONE',
                          'NOT_EXECUTABLE','A1','DENY',$5,$6,$7,'RFC8785','SHA-256','DENY',1,
                          0,0,0,0,1,'test','signature')""",
                decision_id, tenant_a, session_a, uuid4(), policy_id,
                "sha256:" + "2" * 64, "sha256:" + "3" * 64,
            )
            await _seed_all_foundation_stores(
                admin,
                tenant_id=tenant_a,
                principal_session_id=session_a,
                reviewer_session_id=reviewer_a,
                policy_id=policy_id,
                decision_id=decision_id,
            )
            for table_name in FOUNDATION_STORES:
                assert await admin.fetchval(f"SELECT count(*) FROM {table_name}") == 1, table_name
            with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError):
                await admin.execute("DELETE FROM goal_policy_decisions WHERE id=$1", decision_id)
        await admin.execute("RESET ROLE")

        await admin.execute("SET ROLE aads_w12_runtime_test")
        for table_name in FOUNDATION_STORES:
            assert await admin.fetchval(f"SELECT count(*) FROM {table_name}") == 0
        async with admin.transaction():
            await _set_tenant(admin, tenant_b)
            for table_name in FOUNDATION_STORES:
                assert await admin.fetchval(
                    f"SELECT count(*) FROM {table_name} WHERE tenant_id=$1", tenant_a
                ) == 0, table_name
                assert await admin.execute(
                    f"DELETE FROM {table_name} WHERE tenant_id=$1", tenant_a
                ) == "DELETE 0", table_name
        await admin.execute("RESET ROLE")

        # Rollback is executable and deliberately retains schema, data, and RLS.
        await admin.execute(DOWN)
        assert await admin.fetchval("SELECT to_regclass('goal_policy_decisions') IS NOT NULL")
        assert await admin.fetchval(
            "SELECT relforcerowsecurity FROM pg_class WHERE oid='goal_policy_decisions'::regclass"
        )
    finally:
        await admin.execute("RESET ROLE")
        await admin.close()


def test_foundation_migration_twice_concurrency_tenant_fences_and_rollback():
    asyncio.run(_exercise())
