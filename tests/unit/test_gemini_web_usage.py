import asyncio
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import gemini_web_usage as usage

ACCOUNT = 'demo@example.com'
COLLECT_IMPL = usage.Collector._collect
RECOVER_IMPL = usage._recover_cleanup


@pytest.fixture(autouse=True)
def mock_binding_registry(monkeypatch):
    monkeypatch.setenv('JWT_SECRET_KEY', 'unit-test-placeholder-secret')
    monkeypatch.setattr(usage, 'account_enabled', AsyncMock(return_value=True))
    async def restore(service, owner, tenant, account):
        return usage.select_session(service, owner, tenant)
    monkeypatch.setattr(usage, 'restore_session', restore)
    monkeypatch.setattr(usage.Collector, 'authorized', AsyncMock(return_value=True))
    monkeypatch.setattr(usage.Collector, '_collect', usage.Collector._collect_page)
    monkeypatch.setattr(usage, 'recover_cleanup', AsyncMock())


def dom(**kwargs):
    return {'url': usage.SOURCE, 'identity': 'Google 계정 demo@example.com',
            'text': 'PRO\n현재 사용량\n93%\n2시간 후 초기화\n주간 한도\n9%\n11월 8일 초기화', **kwargs}


def test_parser_contract():
    sample = usage.parse_dom(dom(), ACCOUNT)
    assert sample['status'] == 'available'
    assert sample['scope'] == 'gemini_web'
    assert [w['used_percent'] for w in sample['windows']] == [93, 9]
    assert sample['windows'][0]['reset_raw'] == '2시간 후'
    assert all(w['resets_at'] is None and w['window_minutes'] is None for w in sample['windows'])


@pytest.mark.parametrize('data,status', [
    (dom(identity=''), 'identity_unknown'),
    (dom(identity='Google Account other@example.com'), 'account_mismatch'),
    (dom(url='https://accounts.google.com/'), 'auth_required'),
    (dom(text='현재 사용량 93% 주간 한도 9% 내일 초기화'), 'collection_failed'),
    (dom(text='현재 사용량 101% 내일 초기화 주간 한도 9% 내일 초기화'), 'collection_failed'),
    (dom(text='현재 사용량 9% 내일 초기화 현재 사용량 9% 내일 초기화 주간 한도 9% 내일 초기화'), 'collection_failed'),
])
def test_fail_closed(data, status):
    result = usage.parse_dom(data, ACCOUNT)
    assert result['status'] == status
    assert result['windows'] == []
    assert result['account'] is None


@pytest.mark.parametrize('observed', [None, 'invalid', '2026-01-01T12:00:00',
    (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat(),
    (datetime.now(timezone.utc) - timedelta(seconds=301)).isoformat()])
def test_ttl_invalid(observed):
    assert not usage.fresh({'observed_at': observed})


def test_ttl_boundary():
    now = datetime.now(timezone.utc)
    assert usage.fresh({'observed_at': now.isoformat()}, now)
    assert not usage.fresh({'observed_at': (now - timedelta(seconds=300)).isoformat()}, now)


def test_duplicate_refresh_and_expired_sample():
    async def run():
        c = usage.Collector()
        c._collect = AsyncMock(return_value=usage.parse_dom(dom(), ACCOUNT))
        a, b = await asyncio.gather(c.read(ACCOUNT, 'owner', 'tenant'), c.read(ACCOUNT, 'owner', 'tenant'))
        assert a == b
        assert c._collect.await_count == 1
        key = ('tenant', 'owner', ACCOUNT)
        c.samples[key][0]['observed_at'] = 'invalid'
        await c.read(ACCOUNT, 'owner', 'tenant')
        assert c._collect.await_count == 2
    asyncio.run(run())


def session(**kwargs):
    from app.browser_bridge.models import BrowserBridgeSession, BrowserEndpoint, BrowserEndpointKind
    return BrowserBridgeSession(created_by='owner', work_key='extai-gemini-web-usage',
        session_id='test-session', label='test', registered_at=datetime.now(timezone.utc),
        endpoint=BrowserEndpoint(kind=BrowserEndpointKind.CDP,
            metadata={'tenant_id': 'tenant', 'profile_lane': 'extai_fixed',
                      'agent_id': 'pc', 'owner_user_id': 'uid'}), **kwargs)


def scoped_service(**kwargs):
    # This proof is installed only by the authenticated server connect path in production.
    return SimpleNamespace(_gemini_usage_bindings={
        'test-session': ('owner', 'tenant', 'pc', 'uid')}, **kwargs)


def test_session_selection():
    s = session()
    service = scoped_service(sessions=SimpleNamespace(list_sessions=lambda: [s]))
    assert usage.select_session(service, 'owner', 'tenant') is s
    assert usage.select_session(service, 'other', 'tenant') is None
    assert usage.select_session(service, 'owner', 'other') is None
    s.endpoint.metadata['profile_lane'] = 'personal'
    assert usage.select_session(service, 'owner', 'tenant') is None


@pytest.mark.parametrize('navigation', ['ok', 'timeout', 'cancel'])
def test_browser_cleanup(monkeypatch, navigation):
    async def run():
        s = session()
        page = SimpleNamespace(goto=AsyncMock(), wait_for_function=AsyncMock(),
                               evaluate=AsyncMock(return_value=dom()), close=AsyncMock(side_effect=RuntimeError('close')))
        if navigation != 'ok':
            page.goto.side_effect = TimeoutError() if navigation == 'timeout' else asyncio.CancelledError()
        context = SimpleNamespace(new_page=AsyncMock(return_value=page))
        service = scoped_service(sessions=SimpleNamespace(list_sessions=lambda: [s]),
                                  acquire_playwright_context=AsyncMock(return_value=(context, None)))
        monkeypatch.setitem(sys.modules, 'app.browser_bridge.service',
                            SimpleNamespace(get_browser_bridge_service=lambda: service))
        c = usage.Collector()
        if navigation == 'cancel':
            with pytest.raises(asyncio.CancelledError):
                await c.read(ACCOUNT, 'owner', 'tenant')
        else:
            result = await c.read(ACCOUNT, 'owner', 'tenant')
            assert result['status'] == ('available' if navigation == 'ok' else 'pc_offline')
            if navigation == 'ok':
                page.wait_for_function.assert_awaited_once()
        await asyncio.gather(*c.requests, return_exceptions=True)
        await asyncio.gather(*c.cleanup)
        assert page.close.await_count == 2
        service.acquire_playwright_context.assert_awaited_once_with('test-session')
    asyncio.run(run())


def test_worker_cache_independence():
    async def run():
        first, second = usage.Collector(), usage.Collector()
        for c in (first, second):
            c._collect = AsyncMock(return_value=usage.parse_dom(dom(), ACCOUNT))
            assert (await c.read(ACCOUNT, 'owner', 'tenant'))['status'] == 'available'
            c._collect.assert_awaited_once()
    asyncio.run(run())


def test_duplicate_sessions_fail_closed():
    service = scoped_service(sessions=SimpleNamespace(list_sessions=lambda: [session(), session()]))
    assert usage.select_session(service, 'owner', 'tenant') is None


def test_periodic_registered_scope(monkeypatch):
    from contextlib import asynccontextmanager
    class DB:
        @asynccontextmanager
        async def acquire(self):
            yield self
        async def fetch(self, sql):
            return [dict(account=ACCOUNT, owner_email='owner', tenant_id='tenant')]
    from app.core import db_pool
    monkeypatch.setattr(db_pool, 'get_pool', lambda: DB())
    from app.api import llm_keys
    monkeypatch.setattr(llm_keys, '_gemini_usage_account', AsyncMock(return_value={
        'is_active': True, 'manual_disabled': False, 'actual_account': ACCOUNT}), raising=False)
    read = AsyncMock()
    monkeypatch.setattr(usage.collector, 'read', read)
    asyncio.run(usage.refresh_registered())
    read.assert_awaited_once_with(ACCOUNT, 'owner', 'tenant')


def test_refresh_route_requires_admin():
    from app.api import llm_keys
    from app.auth import require_internal_admin
    from fastapi import HTTPException
    routes = [r for r in llm_keys.router.routes if r.path.endswith('/gemini-web-usage/refresh')]
    if not routes:
        pytest.skip('B API wiring is outside A write scope')
    route = routes[0]
    assert any(d.call is require_internal_admin for d in route.dependant.dependencies)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(require_internal_admin({'is_internal_admin': False}))
    assert exc.value.status_code == 403


def test_cancel_during_tab_creation_closes_late_tab(monkeypatch):
    async def run():
        started = asyncio.Event()
        release = asyncio.Event()
        page = SimpleNamespace(close=AsyncMock())
        async def new_page():
            started.set()
            await release.wait()
            return page
        service = scoped_service(sessions=SimpleNamespace(list_sessions=lambda: [session()]),
            acquire_playwright_context=AsyncMock(return_value=(SimpleNamespace(new_page=new_page), None)))
        monkeypatch.setitem(sys.modules, 'app.browser_bridge.service',
                            SimpleNamespace(get_browser_bridge_service=lambda: service))
        c = usage.Collector()
        task = asyncio.create_task(c._collect(ACCOUNT, 'owner', 'tenant'))
        await started.wait()
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.gather(*c.requests, return_exceptions=True)
        await asyncio.gather(*c.cleanup)
        page.close.assert_awaited_once()
    asyncio.run(run())


def test_plan_is_null_without_decisive_dom_evidence():
    for body in ('PLUS\n', 'Google AI PRO 메뉴\nULTRA 업그레이드\n'):
        sample = usage.parse_dom(dom(text=body + '현재 사용량 1% 내일 초기화 주간 한도 2% 모레 초기화'), ACCOUNT)
        assert sample['status'] == 'available' and sample['plan'] is None


def test_wait_exception_parses_final_dom_once(monkeypatch):
    async def run():
        s = session()
        page = SimpleNamespace(goto=AsyncMock(), wait_for_function=AsyncMock(side_effect=TimeoutError()),
                               evaluate=AsyncMock(return_value=dom()), close=AsyncMock())
        service = scoped_service(sessions=SimpleNamespace(list_sessions=lambda: [s]),
            acquire_playwright_context=AsyncMock(return_value=(SimpleNamespace(new_page=AsyncMock(return_value=page)), None)))
        monkeypatch.setitem(sys.modules, 'app.browser_bridge.service',
                            SimpleNamespace(get_browser_bridge_service=lambda: service))
        result = await usage.Collector()._collect_page(ACCOUNT, 'owner', 'tenant')
        assert result['status'] == 'available'
        page.evaluate.assert_awaited_once()
    asyncio.run(run())


def test_lock_cache_eviction_never_drops_held_keys_or_double_collects(monkeypatch):
    async def run():
        monkeypatch.setattr(usage, 'MAX_KEYS', 4)
        c = usage.Collector()
        gate = asyncio.Event()
        calls = []
        async def collect(account, owner, tenant):
            calls.append(account)
            await gate.wait()
            return usage.parse_dom(dom(identity=f'Google 계정 {account}'), account)
        c._collect = collect
        held = [asyncio.create_task(c.read(f'a{i}@example.com', 'o', 't')) for i in range(4)]
        await asyncio.sleep(0.05)
        assert len(c.locks) == 4 and len(calls) == 4
        # Table full and every key in use: refuse instead of evicting a held lock.
        assert (await c.read('new@example.com', 'o', 't'))['status'] == 'busy'
        before = dict(c.locks)
        gate.set()
        assert {r['status'] for r in await asyncio.gather(*held)} == {'available'}
        assert c.locks == before
        # Idle keys are evicted for a new key; re-reading a held-then-idle key recollects at most once.
        assert (await c.read('new@example.com', 'o', 't'))['status'] == 'available'
        assert len(c.locks) <= 4
        again = await asyncio.gather(*[c.read('new@example.com', 'o', 't') for _ in range(3)])
        assert len(set(calls)) == len(calls) and calls.count('new@example.com') == 1
        assert [r['status'] for r in again] == ['available'] * 3
    asyncio.run(run())


def test_state_error_samples_expire_and_concurrent_requests_coalesce():
    async def run():
        c = usage.Collector()
        c._collect = AsyncMock(return_value=usage.unavailable('collection_failed'))
        results = await asyncio.gather(*[c.read(ACCOUNT, 'o', 't') for _ in range(5)])
        assert c._collect.await_count == 1 and {r['status'] for r in results} == {'collection_failed'}
        key = ('t', 'o', ACCOUNT)
        sample, _ = c.samples[key]
        c.samples[key] = (sample, 0)
        await c.read(ACCOUNT, 'o', 't')
        assert c._collect.await_count == 2
    asyncio.run(run())


def test_collection_deadline_bounds_slow_collector_and_cancellation(monkeypatch):
    async def run():
        monkeypatch.setattr(usage, 'COLLECTION_TIMEOUT', 0.1)
        c = usage.Collector()
        done = asyncio.Event()
        async def slow(*_):
            try:
                await asyncio.sleep(30)
            finally:
                await asyncio.sleep(0.5)  # a slow cleanup must not extend the caller's wait
                done.set()
        c._collect = slow
        started = asyncio.get_running_loop().time()
        result = await c.read(ACCOUNT, 'o', 't')
        assert result['status'] == 'timeout'
        assert asyncio.get_running_loop().time() - started < 0.4
        await asyncio.gather(*c.requests, return_exceptions=True)
        await done.wait()
    asyncio.run(run())


@pytest.mark.parametrize('method,path,body', [
    ('get', '/api/v1/llm-keys/overview', None),
    ('post', '/api/v1/llm-keys/gemini-web-usage/connect', {'agent_id': 'pc'}),
    ('post', '/api/v1/llm-keys/gemini-web-usage/refresh', None),
])
def test_http_routes_reject_non_admin_and_anonymous(monkeypatch, method, path, body):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from app.api import llm_keys
    from app.auth import get_current_user
    if not any('/api/v1' + r.path == path for r in llm_keys.router.routes):
        pytest.skip('B API wiring is outside A write scope')
    app = FastAPI()
    app.include_router(llm_keys.router, prefix='/api/v1')
    collect = AsyncMock()
    monkeypatch.setattr(usage.Collector, 'read', collect)
    monkeypatch.setattr(usage, 'connect', collect)
    async def run():
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            anonymous = await client.request(method.upper(), path, json=body)
            assert anonymous.status_code in {401, 403}
            async def non_admin():
                return {'email': 'u@example.com', 'is_internal_admin': False}
            app.dependency_overrides[get_current_user] = non_admin
            denied = await client.request(method.upper(), path, json=body)
            assert denied.status_code == 403
    asyncio.run(run())
    collect.assert_not_awaited()


@pytest.mark.parametrize('account', [None,
    {'is_active': False, 'actual_account': ACCOUNT},
    {'is_active': True, 'manual_disabled': True, 'actual_account': ACCOUNT},
    {'is_active': True, 'actual_account': 'other@example.com'}])
def test_scheduler_never_collects_disabled_or_mismatched_account(monkeypatch, account):
    from contextlib import asynccontextmanager
    from app.core import db_pool
    from app.api import llm_keys
    class DB:
        @asynccontextmanager
        async def acquire(self):
            yield self
        async def fetch(self, sql):
            return [dict(account=ACCOUNT, owner_email='owner', tenant_id='tenant')]
    monkeypatch.setattr(db_pool, 'get_pool', lambda: DB())
    monkeypatch.setattr(llm_keys, '_gemini_usage_account', AsyncMock(return_value=account), raising=False)
    read = AsyncMock()
    monkeypatch.setattr(usage.collector, 'read', read)
    asyncio.run(usage.refresh_registered())
    read.assert_not_awaited()


@pytest.mark.parametrize('reason', ['process_changed', 'browser_changed', 'owner_not_found', 'target_not_owned'])
def test_ownership_uncertainty_is_not_absence(reason):
    assert not usage.tab_gone({'reason': reason})


@pytest.mark.parametrize('field,value,status', [
    ('email', 'other', 'ownership_conflict'),
    ('tenant_id', 'other', 'ownership_conflict'),
    ('user_id', 'other', 'ownership_conflict'),
    ('agent_id', 'other', 'ownership_conflict'),
    ('email', 'owner', 'account_mismatch'),
])
def test_existing_account_mismatch_has_no_browser_side_effects(monkeypatch, field, value, status):
    from contextlib import asynccontextmanager
    @asynccontextmanager
    async def acquire():
        yield SimpleNamespace(fetchrow=AsyncMock(return_value={
            'owner_email': 'owner', 'tenant_id': 'tenant', 'owner_user_id': 'uid',
            'agent_id': 'pc', 'account': 'different@example.com'}))
    from app.core import db_pool
    monkeypatch.setattr(db_pool, 'get_pool', lambda: SimpleNamespace(acquire=acquire))
    monkeypatch.setattr(usage, 'verify_agent', AsyncMock(return_value=True))
    service = SimpleNamespace(sessions=SimpleNamespace(find_by_work_key=AsyncMock()),
                              ensure_work_session=AsyncMock())
    monkeypatch.setitem(sys.modules, 'app.browser_bridge.service', SimpleNamespace(
        get_browser_bridge_service=lambda: service, EXTAI_PROFILE_LANE='extai_fixed'))
    user = {'email': 'owner', 'tenant_id': 'tenant', 'user_id': 'uid'}
    if field != 'agent_id':
        user[field] = value
    result = asyncio.run(usage._connect(value if field == 'agent_id' else 'pc', user, ACCOUNT))
    assert result['status'] == status
    service.ensure_work_session.assert_not_awaited()
    service.sessions.find_by_work_key.assert_not_called()


def test_collect_releases_connection_before_nested_pc_database_work(monkeypatch):
    from contextlib import asynccontextmanager
    from app.core import db_pool
    async def run():
        semaphore = asyncio.Semaphore(1)
        @asynccontextmanager
        async def acquire():
            async with semaphore:
                yield conn
        conn = SimpleNamespace(fetchval=AsyncMock(return_value=False))
        pool = SimpleNamespace(acquire=acquire)
        monkeypatch.setattr(db_pool, 'get_pool', lambda: pool)
        @asynccontextmanager
        async def lease(kind):
            async with pool.acquire():
                pass
            yield 'token'
        monkeypatch.setattr(usage, 'operation_lease', lease)
        monkeypatch.setattr(usage, 'load_binding', AsyncMock(return_value={'owner_user_id': 'uid'}))
        monkeypatch.setattr(usage, 'registration_active', AsyncMock(return_value=True))
        monkeypatch.setattr(usage, 'publish_sample', AsyncMock(return_value=True))
        async def pc(*args):
            async with pool.acquire():
                return usage.parse_dom(dom(), ACCOUNT)
        c = usage.Collector()
        c._collect_page = pc
        result = await asyncio.wait_for(COLLECT_IMPL(c, ACCOUNT, 'owner', 'tenant'), 1)
        assert result['status'] == 'available'
    asyncio.run(run())


def test_cleanup_capacity_counts_and_inserts_under_same_fence(monkeypatch):
    from contextlib import asynccontextmanager
    from app.core import db_pool
    conn = SimpleNamespace(fetchval=AsyncMock(return_value=usage.MAX_KEYS), execute=AsyncMock())
    @asynccontextmanager
    async def transaction():
        yield
    conn.transaction = transaction
    @asynccontextmanager
    async def acquire():
        yield conn
    monkeypatch.setattr(db_pool, 'get_pool', lambda: SimpleNamespace(acquire=acquire))
    fence = AsyncMock()
    monkeypatch.setattr(usage, 'fence', fence)
    s = session()
    s.endpoint.metadata['gemini_account'] = ACCOUNT
    with pytest.raises(RuntimeError, match='capacity'):
        asyncio.run(usage.begin_cleanup(s))
    fence.assert_awaited_once()
    conn.execute.assert_not_awaited()


def test_cleanup_uses_create_scope_and_preserves_changed_browser(monkeypatch):
    from contextlib import asynccontextmanager
    from app.core import db_pool
    row = dict(request_id='request-1', tenant_id='tenant', owner_email='owner',
               account=ACCOUNT, work_key='extai-fixed-profile',
               created_at=datetime.now(timezone.utc)-timedelta(seconds=1000))
    @asynccontextmanager
    async def acquire():
        yield SimpleNamespace(fetch=AsyncMock(return_value=[row]))
    monkeypatch.setattr(db_pool, 'get_pool', lambda: SimpleNamespace(acquire=acquire))
    s = session()
    s.endpoint.metadata['port'] = 9222
    monkeypatch.setattr(usage, 'restore_session', AsyncMock(return_value=s))
    defer, finish = AsyncMock(), AsyncMock()
    monkeypatch.setattr(usage, 'defer_cleanup', defer)
    monkeypatch.setattr(usage, 'finish_cleanup', finish)
    service = SimpleNamespace(_run_extai_pc_command=AsyncMock(return_value={'reason': 'browser_changed'}))
    monkeypatch.setitem(sys.modules, 'app.browser_bridge.service', SimpleNamespace(
        BrowserBridgeError=RuntimeError, get_browser_bridge_service=lambda: service,
        _GeminiUsagePage=AsyncMock()))
    monkeypatch.setattr(usage, 'assert_lease', AsyncMock(return_value='token'))
    asyncio.run(RECOVER_IMPL())
    payload = service._run_extai_pc_command.call_args.args[1]
    assert payload['work_key'] == row['work_key']
    assert payload['chat_id'] == row['request_id']
    finish.assert_not_awaited()
    defer.assert_awaited_once_with('request-1', 'ownership_unverified')


def test_disabled_account_never_opens_page(monkeypatch):
    from contextlib import asynccontextmanager
    @asynccontextmanager
    async def lease(kind):
        yield 'token'
    monkeypatch.setattr(usage, 'operation_lease', lease)
    monkeypatch.setattr(usage, 'account_enabled', AsyncMock(return_value=False))
    c = usage.Collector()
    c._collect_page = AsyncMock()
    assert asyncio.run(COLLECT_IMPL(c, ACCOUNT, 'owner', 'tenant'))['status'] == 'account_disabled'
    c._collect_page.assert_not_awaited()
