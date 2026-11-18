import os
os.environ.setdefault("JWT_SECRET_KEY", "go100-test-only-not-production-key-20261118")

"""Exercise provenance against a disposable, owned PostgreSQL instance."""
import uuid

import asyncpg
import pytest
from fastapi import HTTPException

from app.api import canonical_documents as docs
from scripts.test_support.go100_approval_test_env import IsolatedPostgres


@pytest.fixture
def postgres():
    server = IsolatedPostgres().start()
    try:
        yield server
    finally:
        server.close()


async def create_schema(conn):
    await conn.execute('''
        CREATE TABLE chat_workspaces(id uuid PRIMARY KEY, tenant_id uuid, project_key text);
        CREATE TABLE chat_sessions(id uuid PRIMARY KEY, tenant_id uuid, workspace_id uuid, user_id text);
        CREATE TABLE pipeline_jobs(job_id text PRIMARY KEY, tenant_id uuid, project text, chat_session_id text, actual_model text);
        CREATE TABLE tenant_memberships(tenant_id uuid, user_id text, role text, status text, deleted_at timestamptz);
        CREATE TABLE project_document_grants(tenant_id uuid, project_key text, user_id text, access text);
        CREATE TABLE goals(id uuid, tenant_id uuid, project text);
        CREATE TABLE milestones(id uuid, goal_id uuid);
        CREATE TABLE goal_task_links(task_id text, tenant_id uuid, milestone_id uuid, goal_id uuid);
        CREATE TABLE project_document_heads(id uuid DEFAULT gen_random_uuid() PRIMARY KEY, tenant_id uuid, project_key text, document_key text, kind text, title text, generation int DEFAULT 0, latest_revision_id uuid, approved_revision_id uuid, updated_at timestamptz DEFAULT now(), UNIQUE(tenant_id,project_key,document_key));
        CREATE TABLE project_document_goal_links(head_id uuid, tenant_id uuid, project_key text, goal_id uuid);
        CREATE TABLE project_document_revisions(id uuid DEFAULT gen_random_uuid() PRIMARY KEY, head_id uuid, tenant_id uuid, project_key text, revision int, version text, title text, content text, content_hash text, source_path text, source_kind text, source_task_id text, source_session_id uuid, goal_id uuid, change_summary text, author_id text, idempotency_key text, doc_tier text, author_model text, author_provider text);
        CREATE TABLE project_document_events(tenant_id uuid, project_key text, head_id uuid, revision_id uuid, action text, actor_id text);
        ''')


@pytest.mark.asyncio
async def test_cross_project_provenance_and_rollback(postgres):
    conn = await asyncpg.connect(**postgres.connection_args())
    tenant, other, workspace, session = (uuid.uuid4() for _ in range(4))
    try:
        await create_schema(conn)
        await conn.execute('INSERT INTO chat_workspaces VALUES($1,$2,$3)', workspace, tenant, 'AADS')
        await conn.execute('INSERT INTO chat_sessions VALUES($1,$2,$3,$4)', session, tenant, workspace, 'user')
        await conn.execute('INSERT INTO pipeline_jobs VALUES($1,$2,$3,$4,$5)', 'runner-test', tenant, 'AADS', str(session), 'claude-test')
        await conn.execute("INSERT INTO tenant_memberships VALUES($1,$2,'member','active',NULL)", tenant, 'user')
        await conn.execute('INSERT INTO project_document_grants VALUES($1,$2,$3,$4)', tenant, 'GO100', 'user', 'write')
        body = docs.RevisionInput(document_key='test-doc', kind='report', title='보고', version='1.0.0', content='본문', source_task_id='runner-test', source_session_id=session, change_summary='문서 작성 이유입니다', expected_generation=0)
        async with conn.transaction():
            result = await docs.create_revision_in_tx(conn, str(tenant), 'chat', 'GO100', body, session_any_project=True, bound_session_id=session)
        row = await conn.fetchrow('SELECT source_task_id,author_model FROM project_document_revisions WHERE id=$1', result['revision_id'])
        assert tuple(row) == ('runner-test', 'claude-test')
        assert result['generation'] == 1
        await conn.execute('DELETE FROM project_document_grants')
        body = body.model_copy(update={'document_key': 'denied-doc'})
        with pytest.raises(HTTPException) as exc:
            async with conn.transaction():
                await docs.create_revision_in_tx(conn, str(tenant), 'chat', 'GO100', body, session_any_project=True, bound_session_id=session)
        assert exc.value.detail == 'task_not_found'
        assert not await conn.fetchval("SELECT 1 FROM project_document_heads WHERE document_key='denied-doc'")
        await conn.execute('INSERT INTO project_document_grants VALUES($1,$2,$3,$4)', tenant, 'GO100', 'user', 'write')
        await conn.execute('UPDATE chat_workspaces SET tenant_id=$1', other)
        with pytest.raises(HTTPException):
            async with conn.transaction():
                await docs.create_revision_in_tx(conn, str(tenant), 'chat', 'GO100', body, session_any_project=True, bound_session_id=session)
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_register_and_authority_matrix(postgres, monkeypatch):
    from app.services import canonical_document_tools as tools

    async def no_review(*args, **kwargs):
        return None

    monkeypatch.setattr(docs, '_schedule_cross_review_after_commit', no_review)
    monkeypatch.setattr(tools.canonical_gate, 'check_chat_register', no_review)
    pool = await asyncpg.create_pool(**postgres.connection_args())
    tenant, other, workspace, session, other_session = (uuid.uuid4() for _ in range(5))
    async with pool.acquire() as conn:
        await create_schema(conn)
        await conn.execute('INSERT INTO chat_workspaces VALUES($1,$2,$3)', workspace, tenant, 'AADS')
        await conn.execute('INSERT INTO chat_sessions VALUES($1,$2,$3,$4),($5,$2,$3,$4)', session, tenant, workspace, 'user', other_session)
        await conn.execute('INSERT INTO pipeline_jobs VALUES($1,$2,$3,$4,$5)', 'runner-test', tenant, 'AADS', str(session), 'claude-test')
        await conn.execute("INSERT INTO tenant_memberships VALUES($1,'user','owner','active',NULL)", tenant)
    inp = dict(project='GO100', document_key='actual-report', kind='report', title='실제 보고', content='첫 번째 문서 본문', source_task_id='runner-test', change_summary='문서 출처 검증을 위해 작성합니다')
    try:
        first = await tools.register_document(tenant_id=str(tenant), session_id=str(session), inp=inp, pool=pool)
        assert first.get('registered'), first
        second = await tools.register_document(tenant_id=str(tenant), session_id=str(session), inp={**inp, 'content': '두 번째 문서 본문'}, pool=pool)
        assert second.get('registered') and second['generation'] == 2, second
        readback = await asyncpg.connect(**postgres.connection_args())
        try:
            rows = await readback.fetch('SELECT revision,content,source_task_id,author_model FROM project_document_revisions ORDER BY revision')
            assert [tuple(r) for r in rows] == [(1, '첫 번째 문서 본문', 'runner-test', 'claude-test'), (2, '두 번째 문서 본문', 'runner-test', 'claude-test')]
        finally:
            await readback.close()
        async with pool.acquire() as conn:
            await conn.execute("UPDATE tenant_memberships SET role='member'")
            await conn.execute("INSERT INTO project_document_grants VALUES($1,'GO100','user','write')", tenant)
        allowed = await tools.register_document(tenant_id=str(tenant), session_id=str(session), inp={**inp, 'document_key': 'member-report'}, pool=pool)
        assert allowed.get('registered'), allowed
        async with pool.acquire() as conn:
            for mutation in ["DELETE FROM tenant_memberships", "INSERT INTO tenant_memberships VALUES('%s','user','member','suspended',NULL)" % tenant, "UPDATE tenant_memberships SET status='active',deleted_at=now()", "UPDATE tenant_memberships SET deleted_at=NULL", "DELETE FROM project_document_grants"]:
                await conn.execute(mutation)
                if mutation == "UPDATE tenant_memberships SET deleted_at=NULL":
                    continue
                result = await tools.register_document(tenant_id=str(tenant), session_id=str(session), inp={**inp, 'document_key': 'denied-report', 'role': 'owner', 'elevated': True}, pool=pool)
                assert result.get('error') == 'task_not_found', result
            await conn.execute("INSERT INTO project_document_grants VALUES($1,'GO100','user','approve')", tenant)
            await conn.execute('UPDATE pipeline_jobs SET chat_session_id=$1', 'not-a-uuid')
        result = await tools.register_document(tenant_id=str(tenant), session_id=str(session), inp={**inp, 'document_key': 'denied-report'}, pool=pool)
        assert result.get('error') == 'task_not_found', result
        async with pool.acquire() as conn:
            await conn.execute('UPDATE pipeline_jobs SET chat_session_id=$1', str(session))
        result = await tools.register_document(tenant_id=str(tenant), session_id=str(other_session), inp={**inp, 'document_key': 'denied-report', 'source_session_id': str(session)}, pool=pool)
        assert result.get('error') == 'task_not_found', result
        result = await tools.register_document(tenant_id=str(other), session_id=str(session), inp={**inp, 'document_key': 'denied-report'}, pool=pool)
        assert result.get('error'), result
        async with pool.acquire() as conn:
            assert not await conn.fetchval("SELECT 1 FROM project_document_heads WHERE document_key='denied-report'")
            assert tuple(await conn.fetchrow("SELECT project,chat_session_id,actual_model FROM pipeline_jobs WHERE job_id='runner-test'")) == ('AADS', str(session), 'claude-test')
    finally:
        await pool.close()
