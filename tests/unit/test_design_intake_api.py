"""HTTP contracts using an in-memory connection; no database or network."""
import json
import os
from contextlib import nullcontext
from datetime import datetime, timezone
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI

os.environ.setdefault('JWT_SECRET_KEY', 'test-secret')
from app.api import design_intake as api
from app.services.design_agent import intake

SESSION = UUID('00000000-0000-0000-0000-000000000001')
QUESTION = UUID('00000000-0000-0000-0000-000000000002')
TENANT = UUID('00000000-0000-0000-0000-000000000003')
CHAT = UUID('00000000-0000-0000-0000-000000000005')
OTHER = UUID('00000000-0000-0000-0000-000000000004')


class Connection:
    def __init__(self):
        self.session_queries = []
        self.session = {'id': SESSION, 'round': 1, 'brief': '{}', 'brief_confirmed_at': None}
        self.question = dict(id=QUESTION, area='layout', question='배치는?',
                             options='["A", "B", "C"]', recommended='B',
                             answer=None, answer_source=None, confirmed_at=None)

    def transaction(self):
        return nullcontext()

    async def fetchrow(self, sql, *args):
        if 'FROM design_intake_sessions' in sql:
            self.session_queries.append(sql)
            if 'chat_session_id=$1' in sql:
                assert 'tenant_id=$2' in sql
                assert 'ORDER BY created_at DESC' in sql and 'LIMIT 1' in sql
                return self.session if args == (CHAT, TENANT) else None
            return self.session if args == (SESSION, TENANT) else None
        if 'SELECT recommended' in sql:
            return self.question if args == (QUESTION, SESSION, TENANT) else None
        if 'UPDATE design_intake_questions' in sql:
            if args[:3] != (QUESTION, SESSION, TENANT):
                return None
            self.question.update(answer=args[3], answer_source=args[4], confirmed_at=args[5])
            return {'id': QUESTION}
        if 'AS total' in sql:
            return {'total': 1, 'confirmed': int(self.question['confirmed_at'] is not None)}
        raise AssertionError(sql)

    async def fetch(self, sql, *args):
        assert args == (SESSION, TENANT)
        assert 'tenant_id=$2' in sql
        return [self.question.copy()]

    async def execute(self, sql, *args):
        assert args == (SESSION, TENANT)
        self.session['brief_confirmed_at'] = None

    async def fetchval(self, sql, *args):
        if 'UPDATE design_intake_sessions' in sql:
            self.session.update(brief=args[2], brief_confirmed_at=datetime.now(timezone.utc))
        return self.session['brief_confirmed_at'] if args[:2] == (SESSION, TENANT) else None


@pytest.fixture
async def client(monkeypatch):
    conn = Connection()

    class Pool:
        def acquire(self):
            return nullcontext(conn)

    monkeypatch.setattr(api, 'get_pool', lambda: Pool())
    app = FastAPI()
    app.include_router(api.router, prefix='/api/v1')
    context = {'tenant': {'id': str(TENANT)}}
    app.dependency_overrides[api.VIEW.dependency] = lambda: context
    app.dependency_overrides[api.WRITE.dependency] = lambda: context
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as http:
        yield http, conn, context


@pytest.mark.asyncio
async def test_get(client):
    http, conn, _ = client
    conn.session['round'] = 2
    response = await http.get(f'/api/v1/design/intake/{SESSION}')
    assert response.status_code == 200
    data = response.json()
    assert data['round'] == 2
    assert all('FOR UPDATE' not in sql for sql in conn.session_queries)
    assert data['brief_confirmed'] is False
    assert data['questions'][0]['options'] == ['A', 'B', 'C']
    assert data['questions'][0]['confirmed_at'] is None


@pytest.mark.asyncio
@pytest.mark.parametrize('source,internal,answer', [
    ('user', 'ceo_choice', 'A'), ('user_other', 'ceo_input', '직접 입력'),
    ('unknown', 'recommended_accepted', 'B'),
])
async def test_answer_and_confirm(client, source, internal, answer):
    http, conn, _ = client
    with pytest.raises(ValueError, match='brief must be confirmed'):
        await intake.assert_brief_confirmed(conn, TENANT, SESSION)
    response = await http.post(f'/api/v1/design/intake/{SESSION}/confirm', json={'brief': {}})
    assert response.status_code == 409
    response = await http.post(f'/api/v1/design/intake/{SESSION}/answers', json={
        'question_id': str(QUESTION), 'answer': '잘 모름' if source == 'unknown' else answer,
        'answer_source': source})
    assert response.status_code == 200
    assert response.json()['answer'] == answer
    assert conn.question['answer_source'] == internal
    assert conn.question['confirmed_at'] is not None
    response = await http.get(f'/api/v1/design/intake/{SESSION}')
    assert response.json()['questions'][0]['answer_source'] == source
    response = await http.post(f'/api/v1/design/intake/{SESSION}/confirm', json={'brief': {'goal': 'shop'}})
    assert response.status_code == 200
    await intake.assert_brief_confirmed(conn, TENANT, SESSION)
    assert json.loads(conn.session['brief']) == {'goal': 'shop'}
    await http.post(f'/api/v1/design/intake/{SESSION}/answers', json={
        'question_id': str(QUESTION), 'answer': 'C', 'answer_source': 'user'})
    with pytest.raises(ValueError, match='brief must be confirmed'):
        await intake.assert_brief_confirmed(conn, TENANT, SESSION)


@pytest.mark.asyncio
@pytest.mark.parametrize('method,path,body', [
    ('GET', '', None), ('POST', '/answers', {'question_id': str(QUESTION), 'answer': 'A', 'answer_source': 'user'}),
    ('POST', '/confirm', {'brief': {}}),
])
async def test_other_tenant_404(client, method, path, body):
    http, conn, context = client
    context['tenant']['id'] = str(OTHER)
    response = await http.request(method, f'/api/v1/design/intake/{SESSION}{path}', json=body)
    assert response.status_code == 404
    assert conn.question['answer'] is None


@pytest.mark.asyncio
@pytest.mark.parametrize('source,answer', [('invalid', 'A'), ('ceo_choice', 'A'), ('user', '  ')])
async def test_invalid_answer_400(client, source, answer):
    http, conn, _ = client
    response = await http.post(f'/api/v1/design/intake/{SESSION}/answers', json={
        'question_id': str(QUESTION), 'answer': answer, 'answer_source': source})
    assert response.status_code == 400
    assert conn.question['answer'] is None


@pytest.mark.asyncio
async def test_unknown_requires_recommendation(client):
    http, conn, _ = client
    conn.question['recommended'] = None
    response = await http.post(f'/api/v1/design/intake/{SESSION}/answers', json={
        'question_id': str(QUESTION), 'answer': '잘 모름', 'answer_source': 'unknown'})
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_missing_question_404(client):
    http, _, _ = client
    response = await http.post(f'/api/v1/design/intake/{SESSION}/answers', json={
        'question_id': str(OTHER), 'answer': 'A', 'answer_source': 'user'})
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_by_chat_matches_get_without_lock(client):
    http, conn, _ = client
    conn.session['round'] = 3
    response = await http.get(f'/api/v1/design/intake/by-chat/{CHAT}')
    direct = await http.get(f'/api/v1/design/intake/{SESSION}')
    assert response.status_code == direct.status_code == 200
    assert response.json() == direct.json()
    assert response.json()['round'] == 3
    assert all('FOR UPDATE' not in sql for sql in conn.session_queries)


@pytest.mark.asyncio
@pytest.mark.parametrize('other_tenant', [False, True])
async def test_by_chat_empty_and_tenant_isolation(client, other_tenant):
    http, _, context = client
    if other_tenant:
        context['tenant']['id'] = str(OTHER)
    chat = CHAT if other_tenant else OTHER
    response = await http.get(f'/api/v1/design/intake/by-chat/{chat}')
    assert response.status_code == 200
    assert response.json() == {
        'session_id': None, 'questions': [], 'round': None, 'brief': {},
        'brief_confirmed': False, 'brief_confirmed_at': None,
    }


def test_by_chat_route_precedes_session_route():
    paths = [route.path for route in api.router.routes]
    assert paths.index('/design/intake/by-chat/{chat_session_id}') < paths.index(
        '/design/intake/{session_id}')
