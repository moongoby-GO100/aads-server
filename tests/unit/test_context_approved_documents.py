"""승인 정본의 목표 우선순위·발췌·CEO 권한 범위를 검증한다."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from app.api import canonical_documents as docs
from app.services.context_builder import _build_approved_document_layer

TENANT = '00000000-0000-0000-0000-000000000001'
SESSION = '00000000-0000-0000-0000-000000000002'
GOAL = '00000000-0000-0000-0000-000000000003'
EMPTY = '승인된 정본 문서 없음/조회 불가'


def document():
    return dict(document_key='plan', project_key='AADS', title='계획', revision=2,
                version='1.0.0', excerpt='도입부', goal_rank=1)


class Connection:
    def __init__(self, project='AADS', allowed=True):
        self.project = project
        self.allowed = allowed
        self.calls = []

    async def fetchrow(self, sql, *args):
        return dict(tenant_id=TENANT, project_key=self.project, user_id='user')

    async def fetchval(self, sql, *args):
        assert 'project_document_grants' in sql
        return self.allowed

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        if 'SELECT id FROM goals' in sql:
            assert 'owner_session_id=$2::uuid' in sql and args == (TENANT, SESSION)
            for status in ('done', 'completed', 'cancelled', 'superseded', 'archived'):
                assert "'" + status + "'" in sql
            return [{'id': GOAL}]
        if 'SELECT DISTINCT project_key' in sql:
            assert args == (TENANT, 'user', ['read', 'write', 'approve'])
            return [{'project_key': 'AADS'}, {'project_key': 'GO100'}]
        assert 'r.id=h.approved_revision_id' in sql
        if 'goal_rank' in sql:
            assert 'project_document_goal_links' in sql
            assert 'linked.goal_id=ANY($4::uuid[])' in sql
            assert 'ORDER BY goal_rank,h.updated_at DESC' in sql
            assert args[:3] == (TENANT, 'AADS', 8)
            # Model the ordering and limit requested from PostgreSQL.
            rows = [document(), {**document(), 'document_key': 'linked', 'goal_rank': 0}]
            return sorted(rows, key=lambda r: r['goal_rank']) if args[3] else rows
        assert 'content' not in sql
        assert args == (TENANT, ['AADS', 'GO100'], 40)
        available = [document(), {**document(), 'project_key': 'GO100'},
                     {**document(), 'project_key': 'PRIVATE', 'title': '금지 문서'}]
        return [row for row in available if row['project_key'] in args[1]]


def test_session_goal_linked_documents_first():
    conn = Connection()
    result = asyncio.run(_build_approved_document_layer(SESSION, conn))
    assert result.index('[목표 연결] linked') < result.index('- plan')
    assert conn.calls[-1][1][3] == [GOAL]


def test_old_call_signature_and_positional_limit():
    for args in [(), (8,)]:
        rows = asyncio.run(docs.approved_brief(Connection(), TENANT, 'AADS', *args))
        assert rows[0]['document_key'] == 'plan'


def test_acceptance_after_long_introduction_and_fallback():
    body = '도입' * 3000 + '\n## 완료 기준\n검증 통과\n## 기타\n제외'
    result = docs.format_approved_brief([{**document(), 'excerpt': body}])
    assert '## 완료 기준\n검증 통과' in result
    assert '도입' not in result and '제외' not in result
    assert '도입부' in docs.format_approved_brief([document()])


def test_brief_budget_and_secret_filter():
    rows = [{**document(), 'excerpt': 'x' * 2000} for _ in range(8)]
    assert len(docs.format_approved_brief(rows, max_chars=10000)) <= 3000
    assert docs.format_approved_brief([{**document(), 'excerpt': 'password=hidden'}]) == EMPTY
    assert docs.format_approved_brief([{**document(), 'excerpt': '## 완료 기준\n안전\n## 기타\npassword=hidden'}]) == EMPTY


def test_ceo_index_all_granted_projects_without_body_or_ungranted_project():
    result = asyncio.run(_build_approved_document_layer(SESSION, Connection('CEO')))
    assert 'AADS' in result and 'GO100' in result and 'plan@r2' in result
    assert '도입부' not in result and '금지 문서' not in result and 'PRIVATE' not in result


def test_index_budget_and_secret_filter():
    rows = [{**document(), 'title': '계획' * 100} for _ in range(40)]
    assert len(docs.format_approved_index(rows, max_chars=10000)) <= 2000
    assert docs.format_approved_index([{**document(), 'title': 'password=hidden'}]) == EMPTY


@pytest.mark.parametrize('method', ['fetchrow', 'fetchval', 'fetch'])
def test_database_errors_fail_closed(method):
    conn = Connection()
    setattr(conn, method, AsyncMock(side_effect=RuntimeError('database unavailable')))
    assert asyncio.run(_build_approved_document_layer(SESSION, conn)) == EMPTY


def test_denied_project_and_empty_ceo_grants_fail_closed():
    assert asyncio.run(_build_approved_document_layer(SESSION, Connection(allowed=False))) == EMPTY
    conn = Connection('CEO')
    conn.fetch = AsyncMock(return_value=[])
    assert asyncio.run(_build_approved_document_layer(SESSION, conn)) == EMPTY
    assert conn.fetch.await_count == 1


@pytest.mark.parametrize('limit', [0, 41])
def test_index_invalid_limit(limit):
    with pytest.raises(ValueError):
        asyncio.run(docs.approved_index(Connection(), TENANT, ['AADS'], limit))


@pytest.mark.parametrize('project', ['AADS', 'CEO'])
def test_document_query_errors_fail_closed(project):
    conn = Connection(project)
    original = conn.fetch

    async def fail_documents(sql, *args):
        if 'project_document_heads' in sql:
            raise RuntimeError('document lookup unavailable')
        return await original(sql, *args)

    conn.fetch = fail_documents
    assert asyncio.run(_build_approved_document_layer(SESSION, conn)) == EMPTY
