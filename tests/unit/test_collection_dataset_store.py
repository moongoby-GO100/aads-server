"""수집 저장소 중복·격리·실패·잠금·인증 계약."""
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api import collection_datasets as api
from app.services import collection_dataset_store as store
from app.services import collection_pause as pause

TENANT = uuid4()
OTHER = uuid4()


class Connection:
    def __init__(self):
        self.records = {}
        self.calls = []
        self.dataset = {'id': uuid4(), 'field_schema': {'id': {}}, 'key_fields': ['id'], 'enabled': False}

    @asynccontextmanager
    async def transaction(self):
        yield

    async def fetchrow(self, sql, *args):
        self.calls.append((sql, args))
        return self.dataset if args[0] == TENANT else None

    async def fetchval(self, sql, *args):
        self.calls.append((sql, args))
        if 'advisory' in sql:
            return True
        if 'INSERT INTO collection_records' in sql:
            key = (args[0], args[1], args[2])
            if key not in self.records:
                self.records[key] = args[3]
                return args[2]
        return None

    async def execute(self, sql, *args):
        self.calls.append((sql, args))


@pytest.mark.asyncio
async def test_upsert_deduplicates_and_scopes_every_data_query():
    conn = Connection()
    output = {'status': 'success', 'rows': [{'id': '1'}, {'id': '1'}], 'required_fill_rate': 1}
    first = await store.upsert(conn, TENANT, 'items', output)
    second = await store.upsert(conn, TENANT, 'items', output)
    assert (first['rows_new'], second['rows_new']) == (1, 0)
    assert len(conn.records) == 1
    assert all('tenant_id' in sql for sql, _ in conn.calls)
    with pytest.raises(ValueError, match='dataset_not_found'):
        await store.upsert(conn, OTHER, 'items', output)


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['extraction_failed', 'failed', 'action_required', 'no_records'])
async def test_failed_extraction_never_writes_records(status):
    conn = Connection()
    result = await store.upsert(conn, TENANT, 'items', {'status': status, 'rows': [{'id': '1'}]})
    assert result['rows_new'] == 0
    assert not conn.records


@pytest.mark.asyncio
async def test_action_required_uses_delivery_rules():
    conn = Connection()
    result = await store.upsert(conn, TENANT, 'items', {'status': 'failed'}, error_code='PORTAL_BLOCKED')
    assert result['status'] == 'action_required'


@pytest.mark.parametrize('row', [{'id': '<html>raw</html>'}, {'id': 'Bearer credential'}, {'cookie': 'value'}, {'id': {'raw': 'value'}}])
def test_unsafe_fields_rejected(row):
    with pytest.raises(ValueError):
        store.prepare_rows([row], {'id': {}}, ['id'])


@pytest.mark.asyncio
async def test_pause_is_dataset_and_tenant_scoped(monkeypatch):
    monkeypatch.setattr(pause, '_load', AsyncMock(return_value={
        'smart_browser_datasets': {f'{TENANT}:items': True}, 'delivery': False}))
    assert await pause.is_collection_paused('smart_browser', dataset_key='items', tenant_id=str(TENANT))
    assert not await pause.is_collection_paused('smart_browser', dataset_key='items', tenant_id=str(OTHER))
    assert not await pause.is_collection_paused('delivery')


@pytest.mark.asyncio
async def test_locks_release_when_body_fails(monkeypatch):
    conn = Connection()
    class Pool:
        @asynccontextmanager
        async def acquire(self):
            yield conn
    monkeypatch.setattr(store, 'get_pool', lambda: Pool())
    monkeypatch.setattr(pause, 'is_collection_paused', AsyncMock(return_value=False))
    with pytest.raises(RuntimeError, match='body failed'):
        async with store.execution_scope(TENANT, ['items'], 'example.com'):
            raise RuntimeError('body failed')
    assert sum('pg_advisory_unlock' in sql for sql, _ in conn.calls) == 2


@pytest.mark.asyncio
async def test_automatic_action_required_does_not_execute(monkeypatch):
    conn = Connection()
    conn.dataset['enabled'] = True
    original = conn.fetchval
    async def fetchval(sql, *args):
        if 'r.status' in sql:
            return 'action_required'
        return await original(sql, *args)
    conn.fetchval = fetchval
    class Pool:
        @asynccontextmanager
        async def acquire(self):
            yield conn
    monkeypatch.setattr(store, 'get_pool', lambda: Pool())
    monkeypatch.setattr(pause, 'is_collection_paused', AsyncMock(return_value=False))
    with pytest.raises(RuntimeError, match='ACTION_REQUIRED'):
        async with store.execution_scope(TENANT, ['items'], 'example.com', automatic=True):
            pytest.fail('automatic execution must remain blocked')


@pytest.mark.asyncio
async def test_api_auth_and_tenant_membership(monkeypatch):
    app = FastAPI()
    app.include_router(api.router, prefix='/api/v1')
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get('/api/v1/collection-datasets')).status_code in {401, 403}
        async def no_membership():
            return {'tenant_id': str(TENANT)}
        app.dependency_overrides[api.get_current_user] = no_membership
        assert (await client.get('/api/v1/collection-datasets')).status_code == 403
        async def member():
            return {
            'tenant_id': str(TENANT), 'current_membership': {
                'tenant_id': str(TENANT), 'role': 'viewer', 'status': 'active'}}
        app.dependency_overrides[api.get_current_user] = member
        pool = type('Pool', (), {'fetch': AsyncMock(return_value=[]), 'fetchval': AsyncMock(return_value=None)})()
        monkeypatch.setattr(api, 'get_pool', lambda: pool)
        assert (await client.get('/api/v1/collection-datasets')).json() == {'datasets': []}
        assert pool.fetch.call_args.args[1] == TENANT
        assert (await client.get('/api/v1/collection-datasets/private/records')).status_code == 404
        assert (await client.get('/api/v1/collection-datasets/private/records?limit=0')).status_code == 422

@pytest.mark.asyncio
async def test_player_store_failure_and_login_no_retry(monkeypatch):
    from app.services.work_recipe.player import RecipePlayer
    from app.services.work_recipe.schema import WorkRecipe
    recipe = WorkRecipe.from_dict({'name': '수집', 'domain': 'example.com', 'steps': [{
        'action': 'extract', 'dataset': 'items', 'row_selector': '.row',
        'fields': {'id': {'selector': '.id', 'type': 'text'}}, 'key_fields': ['id']}]})
    @asynccontextmanager
    async def scope(*args, **kwargs):
        yield object()
    monkeypatch.setattr(store, 'execution_scope', scope)
    monkeypatch.setattr(store, 'upsert', AsyncMock(side_effect=RuntimeError('db down')))
    execute = AsyncMock(return_value={'output': {'status': 'success', 'rows': [{'id': '1'}]}})
    result = await RecipePlayer(execute).play(recipe, context={'tenant_id': str(TENANT)})
    assert result.status == 'failed'
    assert result.error == 'dataset_store_failed'
    assert execute.await_count == 1
    monkeypatch.setattr(store, 'upsert', AsyncMock(return_value={'status': 'action_required'}))
    execute = AsyncMock(return_value={'ok': False, 'error': 'PORTAL_BLOCKED'})
    result = await RecipePlayer(execute, retries=3).play(recipe, context={'tenant_id': str(TENANT)})
    assert result.status == 'blocked'
    assert execute.await_count == 1


@pytest.mark.asyncio
async def test_cron_registration_stays_disabled_and_tenant_scoped(monkeypatch):
    pytest.importorskip('apscheduler', reason='로컬 예약 실행 의존성 미설치')
    pool = type('Pool', (), {'execute': AsyncMock(return_value='UPDATE 1')})()
    monkeypatch.setattr(store, 'get_pool', lambda: pool)
    await store.register_schedule(TENANT, 'items', '0 9 * * *')
    sql, tenant, key, cron = pool.execute.call_args.args
    assert 'enabled=false' in sql and 'tenant_id=$1' in sql
    assert (tenant, key, cron) == (TENANT, 'items', '0 9 * * *')
    with pytest.raises(ValueError):
        await store.register_schedule(TENANT, 'items', 'not cron')


@pytest.mark.asyncio
async def test_busy_domain_releases_dataset_lock(monkeypatch):
    conn = Connection()
    attempts = 0
    async def fetchval(sql, *args):
        nonlocal attempts
        conn.calls.append((sql, args))
        if 'pg_try_advisory_lock' in sql:
            attempts += 1
            return attempts == 1
        return True
    conn.fetchval = fetchval
    class Pool:
        @asynccontextmanager
        async def acquire(self):
            yield conn
    monkeypatch.setattr(store, 'get_pool', lambda: Pool())
    with pytest.raises(RuntimeError, match='ALREADY_RUNNING'):
        async with store.execution_scope(TENANT, ['items'], 'EXAMPLE.com'):
            pytest.fail('must not execute')
    assert sum('pg_advisory_unlock' in sql for sql, _ in conn.calls) == 1


@pytest.mark.asyncio
async def test_login_before_extract_persists_action_required(monkeypatch):
    from app.services.work_recipe.player import RecipePlayer
    from app.services.work_recipe.schema import WorkRecipe
    recipe = WorkRecipe.from_dict({'name': '수집', 'domain': 'example.com', 'steps': [
        {'action': 'navigate', 'url': 'https://example.com'},
        {'action': 'extract', 'dataset': 'items', 'row_selector': '.row',
         'fields': {'id': {'selector': '.id', 'type': 'text'}}, 'key_fields': ['id']}]})
    @asynccontextmanager
    async def scope(*args, **kwargs):
        yield object()
    monkeypatch.setattr(store, 'execution_scope', scope)
    save = AsyncMock(return_value={'status': 'action_required'})
    monkeypatch.setattr(store, 'upsert', save)
    execute = AsyncMock(return_value={'ok': False, 'error': 'PC_AGENT_LOGIN_REQUIRED'})
    result = await RecipePlayer(execute).play(recipe, context={'tenant_id': str(TENANT)})
    assert result.status == 'failed' and execute.await_count == 3
    assert save.call_args.args[2] == 'items'
    assert save.call_args.kwargs['error_code'] == 'PC_AGENT_LOGIN_REQUIRED'


@pytest.mark.asyncio
async def test_dataset_pause_prevents_execution_and_unlocks(monkeypatch):
    conn = Connection()
    class Pool:
        @asynccontextmanager
        async def acquire(self):
            yield conn
    monkeypatch.setattr(store, 'get_pool', lambda: Pool())
    monkeypatch.setattr(pause, 'is_collection_paused', AsyncMock(return_value=True))
    with pytest.raises(RuntimeError, match='COLLECTION_PAUSED'):
        async with store.execution_scope(TENANT, ['items'], 'example.com'):
            pytest.fail('paused dataset must not execute')
    assert sum('pg_advisory_unlock' in sql for sql, _ in conn.calls) == 2


@pytest.mark.asyncio
async def test_record_query_never_loses_tenant_filter(monkeypatch):
    pool = type('Pool', (), {'fetch': AsyncMock(return_value=[]), 'fetchval': AsyncMock(return_value=uuid4())})()
    monkeypatch.setattr(api, 'get_pool', lambda: pool)
    assert await api.history(TENANT, 'items', 'collection_records', 10) == []
    sql, tenant, key, limit = pool.fetch.call_args.args
    assert 'd.tenant_id=$1' in sql
    assert (tenant, key, limit) == (TENANT, 'items', 10)


@pytest.mark.asyncio
@pytest.mark.parametrize("error", ["PC_AGENT_LOGIN_REQUIRED", "PORTAL_BLOCKED"])
@pytest.mark.parametrize("action,dataset", [
    ("navigate", False), ("navigate", True), ("extract", False),
    ("extract", True), ("paginate", False), ("paginate", True),
])
@pytest.mark.parametrize("raises", [False, True])
async def test_player_user_action_blocking_is_dataset_scoped(error, action, dataset, raises):
    from app.services.work_recipe.player import RecipePlayer, StepFailure
    from app.services.work_recipe.schema import RecipeStep

    step_data = {"action": action}
    if action == "navigate":
        step_data["url"] = "https://example.com"
    else:
        step_data.update(row_selector=".row", fields={"id": {"selector": ".id", "type": "text"}},
                         key_fields=["id"])
    if action == "paginate":
        step_data.update(steps=["items"], next_selector=".next")
    if dataset:
        step_data["dataset"] = "items"
    step = RecipeStep.from_dict(step_data, seq=1)
    recovery = {"route": "browser_agent", "reason": "login"}
    evidence = {"screenshot": "login.png"}
    response = {"ok": False, "error": error, "recovery": recovery,
                "narration": "사용자 확인 대기", "evidence": evidence, "output": {"status": "failed"}}
    execute = AsyncMock(side_effect=StepFailure(error)) if raises else AsyncMock(return_value=response)
    result = await RecipePlayer(execute, retries=2, retry_delay=0)._run_step(
        "step", step, {}, set(), {}, ())
    blocked = dataset and action in {"extract", "paginate"}
    attempts = 1 if blocked or action == "paginate" else 3
    assert result.status == ("blocked" if blocked else "failed")
    assert result.attempts == execute.await_count == attempts
    assert result.error == error
    assert result.recovery == ({} if raises else recovery)
    assert result.narration == ("" if raises else "사용자 확인 대기")
    assert result.evidence == (evidence if blocked and not raises else {})
    assert result.duration_ms >= 0
