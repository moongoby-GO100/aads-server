"""M7 corpus excerpts through real services and a disposable PostgreSQL pool.

Run with AADS_DRAFT_TEST_DATABASE_URL pointing only to an approved *_test DB.
Original MD5 values identify the full source; they are NOT hashes of text_head.
Only generation is replaced, to avoid external LLM calls and nondeterminism.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

import asyncpg
import pytest

from app.services import directive_draft_service as service

ROOT = Path(__file__).parents[2]
CORPUS = json.loads((ROOT / 'tests/fixtures/planning_replay_corpus_20260923.json').read_text())


def turn_by_seq(seq):
    return next(turn for turn in CORPUS['turns'] if turn['seq'] == seq)


@asynccontextmanager
async def database(monkeypatch):
    url = os.getenv('AADS_DRAFT_TEST_DATABASE_URL')
    if not url or not urlparse(url).path.removeprefix('/').endswith('_test'):
        pytest.skip('DB 실행 미완료(skip): approved AADS_DRAFT_TEST_DATABASE_URL *_test required')
    # Use the already migrated disposable DB; never synthesize a parallel schema.
    try:
        conn = await asyncio.wait_for(asyncpg.connect(url), 5)
    except (PermissionError, OSError, asyncio.TimeoutError) as exc:
        pytest.skip(f'DB 실행 미완료(skip): {type(exc).__name__}')
    pool = None
    tenant, session, workspace, goal = (uuid.uuid4() for _ in range(4))
    owned_tenants = []
    try:
        await conn.execute("INSERT INTO tenants(id,slug,name) VALUES($1,$2,'재현')",
                           tenant, 'm7-replay-' + tenant.hex)
        owned_tenants.append(tenant)
        await conn.execute("INSERT INTO chat_workspaces(id,tenant_id,name,project_key) VALUES($1,$2,'재현','AADS')",
                           workspace, tenant)
        await conn.execute("INSERT INTO chat_sessions(id,tenant_id,workspace_id,title) VALUES($1,$2,$3,'설계 재현')",
                           session, tenant, workspace)
        await conn.execute("INSERT INTO goals(id,tenant_id,title,owner_role_key,owner_session_id) VALUES($1,$2,'설계 목표','CTO',$3)", goal, tenant, session)
        await conn.execute("INSERT INTO milestones(id,tenant_id,goal_id,title) VALUES($1,$2,$3,'설계 단계')",
                           uuid.uuid4(), tenant, goal)
        for turn in CORPUS['turns']:
            await conn.execute("""INSERT INTO chat_messages(id,tenant_id,session_id,role,content,created_at)
                VALUES($1,$2,$3,'user',$4,$5::text::timestamptz)""",
                uuid.UUID(turn['message_id']), tenant, session, turn['text_head'], turn['created_at_utc'])
        pool = await asyncpg.create_pool(url, min_size=2, max_size=5)
        monkeypatch.setattr(service, 'get_pool', lambda: pool)

        async def generate(source, risk_level, **kwargs):
            return service.build_fallback_directive(source, risk_level), 'fallback', None

        monkeypatch.setattr(service, 'generate_directive_content', generate)

        async def create(turn):
            return await service.create_draft(tenant_id=str(tenant), user_id=None,
                session_id=str(session), message_ids=[turn['message_id']], goal_id=str(goal))

        yield conn, create, tenant, session, goal, owned_tenants
    finally:
        if pool:
            await pool.close()
        try:
            # Delete only this test's randomly allocated tenants and their rows.
            for table in ('directive_draft_events', 'directive_draft_revisions',
                          'directive_drafts', 'chat_artifacts', 'chat_messages',
                          'chat_sessions', 'chat_workspaces', 'milestones', 'goals'):
                await conn.execute(f'DELETE FROM {table} WHERE tenant_id = ANY($1::uuid[])', owned_tenants)
            await conn.execute('DELETE FROM tenants WHERE id = ANY($1::uuid[])', owned_tenants)
        finally:
            await conn.close()


async def plan_snapshot(conn, tenant):
    payload = []
    for table in ('goals', 'milestones'):
        rows = await conn.fetch(f'SELECT row_to_json(t)::text AS body FROM {table} t WHERE tenant_id=$1 ORDER BY id', tenant)
        payload.append([r['body'] for r in rows])
    return [len(rows) for rows in payload], hashlib.sha256(json.dumps(payload).encode()).hexdigest()


@pytest.mark.parametrize('scenario', ['corpus', 'concurrency', 'update_trace', 'binding'])
def test_m7_postgres_replay(monkeypatch, scenario):
    asyncio.run(exercise(monkeypatch, scenario))


async def exercise(monkeypatch, scenario):
    async with database(monkeypatch) as (conn, create, tenant, session, goal, owned_tenants):
        if scenario == 'corpus':
            baseline = await plan_snapshot(conn, tenant)
            results = {}
            for turn in CORPUS['turns']:
                before = await conn.fetchval('SELECT count(*) FROM directive_draft_revisions WHERE tenant_id=$1', tenant)
                draft = await create(turn)
                results[turn['message_id']] = draft
                after = await conn.fetchval('SELECT count(*) FROM directive_draft_revisions WHERE tenant_id=$1', tenant)
                assert after - before == int(turn['expected']['draft_revision_created']), turn['seq']
                if turn.get('duplicate_of'):
                    assert draft['id'] == results[turn['duplicate_of']]['id']
                else:
                    assert turn['message_id'] in draft['source_message_ids']
                assert draft['classification']['goal_id'] == str(goal)
                assert await plan_snapshot(conn, tenant) == baseline, turn['seq']
            assert await conn.fetchval('SELECT count(*) FROM chat_messages WHERE tenant_id=$1', tenant) == 13
        elif scenario == 'concurrency':
            results = await asyncio.gather(*(create(turn_by_seq(seq)) for seq in (8, 10, 8, 10, 8)))
            assert len({d['id'] for d in results}) == 1
            assert await conn.fetchval('SELECT count(*) FROM directive_drafts WHERE tenant_id=$1', tenant) == 1
            assert await conn.fetchval('SELECT count(*) FROM directive_draft_revisions WHERE tenant_id=$1', tenant) == 1
            repeated = await create(turn_by_seq(10))
            assert repeated['id'] == results[0]['id']  # d3254e03 / c5973979
            assert await conn.fetchval('SELECT count(*) FROM directive_draft_revisions WHERE tenant_id=$1', tenant) == 1
        elif scenario == 'update_trace':
            draft = await create(turn_by_seq(7))
            original = draft['classification']
            for revision in (2, 3):
                draft = await service.update_draft(tenant_id=str(tenant), user_id=None,
                    draft_id=draft['id'], title=None,
                    content=draft['content'].replace('TITLE: ', f'TITLE: 검토{revision} ', 1),
                    expected_revision=revision - 1)
                assert draft['classification'] == original
            listed = await service.list_drafts(tenant_id=str(tenant), session_id=str(session))
            assert len(listed) == 1 and listed[0]['current_revision'] == 3
            await service.record_event(tenant_id=str(tenant), user_id=None, draft_id=draft['id'],
                action='approved', metadata={'decision': '검토 승인', 'evidence': turn_by_seq(7)['content_md5']})
            rows = await conn.fetch('SELECT revision,metadata FROM directive_draft_revisions WHERE tenant_id=$1 ORDER BY revision', tenant)
            assert [r['revision'] for r in rows] == [1, 2, 3]
            # Every update must preserve independently readable source/decision/evidence.
            for row in rows[1:]:
                meta = json.loads(row['metadata'])
                assert meta.get('source_message_ids') == draft['source_message_ids']
                assert meta.get('decisions') == original['decisions']
                assert meta.get('turn_kind_evidence') == original['turn_kind_evidence']
            assert await conn.fetchval("SELECT count(*) FROM directive_draft_events WHERE action='approved' AND tenant_id=$1", tenant) == 1
        else:
            turn = turn_by_seq(1)
            first = await create(turn)
            args = dict(tenant_id=str(tenant), user_id=None, session_id=str(session),
                        message_ids=[turn['message_id']], goal_id=str(goal))
            with pytest.raises(service.DraftNotFoundError):
                await service.create_draft(**{**args, 'tenant_id': str(uuid.uuid4())})
            with pytest.raises(ValueError):
                await service.create_draft(**{**args, 'message_ids': [str(uuid.uuid4())]})
            foreign_tenant, foreign_goal = uuid.uuid4(), uuid.uuid4()
            await conn.execute("INSERT INTO tenants(id,slug,name) VALUES($1,$2,'외부')", foreign_tenant, 'm7-replay-' + foreign_tenant.hex)
            owned_tenants.append(foreign_tenant)
            await conn.execute("INSERT INTO goals(id,tenant_id,title,owner_role_key,owner_session_id) VALUES($1,$2,'외부 목표','CTO',$3)", foreign_goal, foreign_tenant, session)
            with pytest.raises(service.GoalBindingError):
                await service.create_draft(**{**args, 'goal_id': str(foreign_goal)})
            other_session, other_message = uuid.uuid4(), uuid.uuid4()
            await conn.execute("INSERT INTO chat_sessions(id,tenant_id,workspace_id) SELECT $1,$2,workspace_id FROM chat_sessions WHERE id=$3", other_session, tenant, session)
            await conn.execute("INSERT INTO chat_messages(id,tenant_id,session_id,role,content) VALUES($1,$2,$3,'user','다른 세션')",
                               other_message, tenant, other_session)
            with pytest.raises(ValueError):
                await service.create_draft(**{**args, 'message_ids': [str(other_message)]})
            other = uuid.uuid4()
            await conn.execute("INSERT INTO goals(id,tenant_id,title,owner_role_key,owner_session_id) VALUES($1,$2,'다른 목표','CTO',$3)", other, tenant, session)
            with pytest.raises(service.GoalBindingError):
                await service.create_draft(**{**args, 'goal_id': str(other)})
            with pytest.raises(service.GoalBindingError):
                await service.create_draft(**{**args, 'goal_id': str(uuid.uuid4())})
            assert (await create(turn))['id'] == first['id']
            assert await conn.fetchval('SELECT count(*) FROM directive_drafts WHERE tenant_id=$1', tenant) == 1
