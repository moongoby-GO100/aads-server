"""Actual approval migration/transactions on disposable PostgreSQL only; no DSN input."""
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from scripts.test_support.go100_approval_test_env import IsolatedPostgres
from app.services.go100_deploy_approval import ApprovalDenied, approve, validate_contract

ROOT = Path(__file__).resolve().parents[2]
PRINCIPAL = {"user_id": "admin", "is_internal_admin": True, "tenant_id": str(uuid4())}


@pytest.fixture
def postgres():
    server = IsolatedPostgres().start()
    try:
        yield server
    finally:
        server.close()


async def setup(server, *, fail_audit=False):
    import asyncpg
    conn = await asyncpg.connect(**server.connection_args(), command_timeout=10)
    # Parent fixture exposes actual production column names; approval DDL is unmodified.
    await conn.execute("""
        CREATE TABLE pipeline_jobs(job_id text PRIMARY KEY, project text, tenant_id uuid, chat_session_id text, commit_hash text);
        CREATE TABLE chat_sessions(id uuid PRIMARY KEY, tenant_id uuid, user_id text);
        CREATE TABLE deploy_runs(id bigserial PRIMARY KEY, project text, component text,
          release_sha text, target_env text, runner_job_id text, chat_session_id uuid,
          requested_by text, request_payload jsonb DEFAULT '{}', status text,
          phase text, approval_policy text, updated_at timestamptz DEFAULT now());
        CREATE TABLE deploy_locks(project text, component text, target_env text,
          deploy_run_id bigint, owner_instance text, owner_epoch text, lease_expires_at timestamptz,
          PRIMARY KEY(project,component,target_env));
        CREATE TABLE deploy_phase_events(id bigserial PRIMARY KEY, deploy_run_id bigint,
          phase text,status text,phase_started_at timestamptz,phase_completed_at timestamptz,
          duration_ms bigint,error_summary text,metadata jsonb);
    """)
    if fail_audit:
        await conn.execute("ALTER TABLE deploy_phase_events ADD CONSTRAINT fail_audit CHECK(false)")
    await conn.execute((ROOT / "migrations/20261118_go100_deploy_approval.sql").read_text())
    tenant, session = uuid4(), uuid4()
    await conn.execute("INSERT INTO chat_sessions VALUES($1,$2,'admin')", session, tenant)
    await conn.execute("INSERT INTO pipeline_jobs VALUES('job','GO100',$1,$2,$3)", tenant, str(session), "a"*40)
    await conn.execute("""INSERT INTO deploy_runs(project,component,release_sha,target_env,runner_job_id,
        chat_session_id,requested_by,status) VALUES('GO100','backend',$1,'test','job',$2,'admin','awaiting_approval')""", "a"*40, session)
    return conn


async def lease(conn):
    await conn.execute("""INSERT INTO deploy_locks VALUES('GO100','backend','test',1,'worker','7',now()+interval '5 minutes')""")


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 90))


def test_approve_reconnect_validate_cli_and_immutability(postgres):
    async def exercise():
        import asyncpg
        conn = await setup(postgres)
        try:
            result = await approve(conn, 1, PRINCIPAL)
            assert result["lane"] == "api_only" and "batch" in result["scope"]
            assert result["tenant_id"] != PRINCIPAL["tenant_id"]  # global admin tenant is not authority
            assert (await approve(conn, 1, PRINCIPAL))["approval_id"] == result["approval_id"]
            await lease(conn)
        finally:
            await conn.close()
        conn = await asyncpg.connect(**postgres.connection_args())
        try:
            contract = await validate_contract(conn, 1, "worker", "7")
            assert contract["approval_id"] == result["approval_id"]
            assert contract["release_sha"] == "a"*40 and contract["schema_version"] == 1
            schema = json.loads((ROOT / "docs/specs/go100-release/go100-central-release-contract-v1.json").read_text())
            assert set(contract) == set(schema["required"]) == set(schema["properties"])
            assert type(contract["run_id"]) is int and contract["run_id"] >= 1
            assert isinstance(contract["owner_epoch"], str) and contract["owner_epoch"]
            from app.services.go100_deploy_approval import snapshot_hash
            snapshot = dict(await conn.fetchrow("SELECT * FROM go100_deploy_approval_snapshots WHERE run_id=1"))
            assert contract["snapshot_hash"] == snapshot_hash(snapshot)
            assert contract["snapshot_hash"] == snapshot["snapshot_hash"]
            args = postgres.connection_args()
            env = {**os.environ, "DATABASE_URL": f"postgresql://go100_test@127.0.0.1:{args['port']}/go100_test"}
            command = [sys.executable, str(ROOT / "scripts/go100_deploy_approval.py"),
                       "--run-id", "1", "--owner-instance", "worker", "--owner-epoch", "7"]
            completed = await asyncio.to_thread(subprocess.run, command, env=env, capture_output=True, text=True, timeout=20)
            assert completed.returncode == 0, completed.stdout
            assert json.loads(completed.stdout) == contract
            definition = await conn.fetchval("""SELECT pg_get_triggerdef(oid) FROM pg_trigger
                WHERE tgrelid='go100_deploy_approval_snapshots'::regclass AND NOT tgisinternal""")
            assert "TRUNCATE" in definition and "go100_deploy_approval_immutable" in definition
            for sql in ("UPDATE go100_deploy_approval_snapshots SET approved_by='forged' WHERE run_id=1", "DELETE FROM go100_deploy_approval_snapshots WHERE run_id=1"):
                with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError):
                    await conn.execute(sql)
            assert await conn.fetchval("SELECT count(*) FROM go100_deploy_approval_snapshots") == 1
        finally:
            await conn.close()
    run(exercise())


@pytest.mark.parametrize("sql,reason", [
    ("UPDATE deploy_runs SET runner_job_id=NULL", "legacy_unbound"),
    ("UPDATE deploy_runs SET requested_by='forged'", "requester_provenance"),
    ("UPDATE chat_sessions SET tenant_id=gen_random_uuid()", "source_tenant"),
    ("UPDATE chat_sessions SET user_id='forged'", "requester_provenance"),
    ("UPDATE pipeline_jobs SET chat_session_id='not-a-uuid'", "source_session_invalid_uuid"),
    ("UPDATE pipeline_jobs SET project='AADS'", "source_project"),
    ("UPDATE deploy_runs SET chat_session_id=gen_random_uuid()", "source_session"),
    ("UPDATE pipeline_jobs SET commit_hash=repeat('b',40)", "source_sha"),
    ("UPDATE deploy_runs SET release_sha='abc1234'", "invalid_release_sha"),
    ("UPDATE deploy_runs SET request_payload='{\"lane\":\"batch\",\"approved\":true}'", "lane_mismatch"),
    ("UPDATE deploy_runs SET component='cards'", "component_not_allowed"),
])
def test_forged_source_rollback(postgres, sql, reason):
    async def exercise():
        conn = await setup(postgres)
        try:
            await conn.execute(sql)
            with pytest.raises(ApprovalDenied, match=reason):
                await approve(conn, 1, PRINCIPAL)
            assert await conn.fetchval("SELECT count(*) FROM go100_deploy_approval_snapshots") == 0
            assert await conn.fetchval("SELECT status FROM deploy_runs WHERE id=1") == "awaiting_approval"
        finally:
            await conn.close()
    run(exercise())


@pytest.mark.parametrize("sql,reason", [
    ("UPDATE deploy_runs SET status='cancelled'", "run_not_executable"),
    ("UPDATE deploy_runs SET status='revoked'", "run_not_executable"),
    ("UPDATE deploy_runs SET approval_policy='revoked'", "approval_policy_revoked"),
    ("INSERT INTO deploy_phase_events(deploy_run_id,phase) VALUES(1,'approval_revoked')", "approval_revoked"),
    ("UPDATE deploy_runs SET release_sha=repeat('b',40)", "run_release_sha_mismatch"),
    ("UPDATE deploy_runs SET component='frontend'", "component_mismatch"),
    ("UPDATE deploy_runs SET request_payload='{\"lane\":\"batch\"}'", "lane_mismatch"),
    ("UPDATE deploy_runs SET target_env='production'", "target_env_mismatch"),
    ("UPDATE pipeline_jobs SET commit_hash=repeat('b',40)", "source_sha"),
    ("UPDATE deploy_locks SET lease_expires_at=now()-interval '1 second'", "live_lease"),
    ("UPDATE deploy_locks SET owner_epoch='8'", "live_lease"),
    ("UPDATE deploy_locks SET owner_instance='other'", "live_lease"),
    ("UPDATE deploy_locks SET deploy_run_id=2", "live_lease"),
])
def test_validation_rejects_current_state_drift(postgres, sql, reason):
    async def exercise():
        conn = await setup(postgres)
        try:
            await approve(conn, 1, PRINCIPAL)
            await lease(conn)
            await conn.execute(sql)
            with pytest.raises(ApprovalDenied, match=reason):
                await validate_contract(conn, 1, "worker", "7")
        finally:
            await conn.close()
    run(exercise())


def test_competing_approvals_and_transaction_rollback(postgres):
    async def exercise():
        import asyncpg
        conn = await setup(postgres)
        other = await asyncpg.connect(**postgres.connection_args())
        try:
            first, second = await asyncio.gather(approve(conn, 1, PRINCIPAL), approve(other, 1, PRINCIPAL))
            assert first["approval_id"] == second["approval_id"]
            assert await conn.fetchval("SELECT count(*) FROM deploy_phase_events") == 1
            with pytest.raises(ApprovalDenied, match="authenticated_requester"):
                await approve(conn, 1, {**PRINCIPAL, "user_id": "forged"})
        finally:
            await other.close()
            await conn.close()
    run(exercise())


def test_audit_failure_rolls_back_snapshot_and_queue(postgres):
    async def exercise():
        import asyncpg
        conn = await setup(postgres, fail_audit=True)
        try:
            with pytest.raises(asyncpg.CheckViolationError):
                await approve(conn, 1, PRINCIPAL)
            assert await conn.fetchval("SELECT count(*) FROM go100_deploy_approval_snapshots") == 0
            assert await conn.fetchval("SELECT status FROM deploy_runs WHERE id=1") == "awaiting_approval"
        finally:
            await conn.close()
    run(exercise())


def test_expired_snapshot_and_frontend(postgres):
    async def exercise():
        conn = await setup(postgres)
        try:
            await conn.execute("UPDATE deploy_runs SET component='frontend'")
            assert (await approve(conn, 1, PRINCIPAL))["lane"] == "frontend"
            # Time travel uses a separate signed INSERT, never changes an immutable row.
            from app.services.go100_deploy_approval import snapshot_hash
            from datetime import timedelta
            row = dict(await conn.fetchrow("SELECT * FROM go100_deploy_approval_snapshots"))
            row.update(id=uuid4(), run_id=2, issued_at=row["issued_at"]-timedelta(hours=3),
                       expires_at=row["expires_at"]-timedelta(hours=3))
            row["snapshot_hash"] = snapshot_hash(row)
            await conn.execute("""INSERT INTO deploy_runs(id,project,component,release_sha,target_env,runner_job_id,
                chat_session_id,requested_by,status,approval_policy) SELECT 2,project,component,release_sha,target_env,runner_job_id,
                chat_session_id,requested_by,'queued','manual_approved' FROM deploy_runs WHERE id=1""")
            columns = list(row)
            await conn.execute("INSERT INTO go100_deploy_approval_snapshots ("+','.join(columns)+") VALUES ("+
                               ','.join('$'+str(i+1) for i in range(len(columns)))+")", *row.values())
            await conn.execute("INSERT INTO deploy_locks VALUES('GO100','frontend','test',2,'worker','7',now()+interval '1 minute')")
            with pytest.raises(ApprovalDenied, match="expired"):
                await validate_contract(conn, 2, "worker", "7")
        finally:
            await conn.close()
    run(exercise())


def test_snapshot_cannot_replay_on_another_run(postgres):
    async def exercise():
        conn = await setup(postgres)
        try:
            await approve(conn, 1, PRINCIPAL)
            await conn.execute("""INSERT INTO deploy_runs(id,project,component,release_sha,target_env,
                runner_job_id,chat_session_id,requested_by,status,approval_policy)
                SELECT 2,project,component,release_sha,target_env,runner_job_id,chat_session_id,
                requested_by,'queued','manual_approved' FROM deploy_runs WHERE id=1""")
            with pytest.raises(ApprovalDenied, match="approval_missing"):
                await validate_contract(conn, 2, "worker", "7")
        finally:
            await conn.close()
    run(exercise())


def test_non_destructive_disable(postgres, monkeypatch):
    async def exercise():
        conn = await setup(postgres)
        try:
            await approve(conn, 1, PRINCIPAL)
            await lease(conn)
            monkeypatch.setenv("AADS_GO100_DEPLOY_APPROVAL_ENABLED", "false")
            with pytest.raises(ApprovalDenied, match="disabled"):
                await validate_contract(conn, 1, "worker", "7")
            assert await conn.fetchval("SELECT count(*) FROM go100_deploy_approval_snapshots") == 1
            assert await conn.fetchval("SELECT count(*) FROM deploy_phase_events") == 1
            monkeypatch.setenv("AADS_GO100_DEPLOY_APPROVAL_ENABLED", "true")
            assert (await validate_contract(conn, 1, "worker", "7"))["schema_version"] == 1
        finally:
            await conn.close()
    run(exercise())


def test_migration_ledger_and_postcheck_are_atomic(postgres):
    async def exercise():
        import asyncpg
        conn = await asyncpg.connect(**postgres.connection_args())
        migration = (ROOT / "migrations/20261118_go100_deploy_approval.sql").read_text()
        verify = (ROOT / "migrations/20261118_go100_deploy_approval.verify.sql").read_text()
        try:
            await conn.execute("CREATE TABLE deploy_runs(id bigint PRIMARY KEY); CREATE TABLE schema_migrations(filename text PRIMARY KEY, sha256 text)")
            with pytest.raises(asyncpg.RaiseError):
                async with conn.transaction():
                    await conn.execute(migration)
                    await conn.execute("INSERT INTO schema_migrations VALUES('go100', 'checksum')")
                    await conn.execute(verify)
                    await conn.execute("DO $$ BEGIN RAISE EXCEPTION 'postcheck failed'; END $$")
            assert await conn.fetchval("SELECT to_regclass('go100_deploy_approval_snapshots')") is None
            assert await conn.fetchval("SELECT count(*) FROM schema_migrations") == 0
            async with conn.transaction():
                await conn.execute(migration)
                assert await conn.fetchval("SHOW lock_timeout") == '5s'
                assert await conn.fetchval("SHOW statement_timeout") == '1min'
                await conn.execute("INSERT INTO schema_migrations VALUES('go100', 'checksum')")
                await conn.execute(verify)
            assert await conn.fetchval("SELECT count(*) FROM schema_migrations") == 1
            assert await conn.fetchval("SELECT to_regclass('go100_deploy_approval_snapshots')") is not None
        finally:
            await conn.close()
    run(exercise())
