"""입력 허용과 제출 권한을 분리하는 M3 fail-closed 계약."""
from types import SimpleNamespace

import pytest

from app.api import ceo_chat_tools as tools
from app.browser_bridge import aads_adapter as adapter
from app.services import browser_login_autosave as autosave
from app.services import browser_observation_safety as safety
from app.services.tool_executor import current_chat_session_id, current_tenant_id


@pytest.fixture(autouse=True)
def scope():
    chat = current_chat_session_id.set('chat-a')
    tenant = current_tenant_id.set('tenant-a')
    yield
    current_chat_session_id.reset(chat)
    current_tenant_id.reset(tenant)


class Page:
    url = 'https://example.test/login'

    def __init__(self, attrs=None):
        self.attrs = {} if attrs is None else attrs
        self.calls = []

    async def eval_on_selector(self, selector, script):
        return self.attrs

    async def evaluate(self, *args):
        return {'login': True, 'account': True}

    async def fill(self, selector, value, **kwargs):
        self.calls.append(('fill', selector))

    async def press_key(self, key, **kwargs):
        self.calls.append(('press', key))

    async def click_detailed(self, selector, **kwargs):
        self.calls.append(('click', selector))
        return {'dispatched': True, 'verified': True}


def _make_pc(monkeypatch, *, attrs=None, owner=None):
    # Inject attrs into the page that _probe_attrs actually queries.
    page = Page(attrs)
    page._session = SimpleNamespace(endpoint=SimpleNamespace(metadata={}))
    if owner is not None:
        page._session.endpoint.metadata['aads_input_owner'] = owner
    monkeypatch.setattr(tools, '_is_pc_agent_page', lambda p: p is page)

    async def acquire(*args, **kwargs):
        return SimpleNamespace(pages=[page]), None

    async def on_fill(*args, **kwargs):
        pass

    monkeypatch.setattr(tools, '_acquire_pw_context', acquire)
    monkeypatch.setattr(autosave, 'on_fill', on_fill)
    return page


async def test_submission_without_binding_is_fail_closed():
    assert await tools._submit_guard_error(Page(), browser_work_key='wk') == 'vault_account_state_unverified'


async def test_implicit_pc_submission_checks_acquired_owner(monkeypatch):
    page = _make_pc(monkeypatch, owner=['tenant-a', 'other-chat'])
    assert await tools._submit_guard_error(page) == 'bridge_session_owner_mismatch'
    assert 'bridge_session_owner_mismatch' in await tools.tool_browser_press_key('Enter')
    assert page.calls == []


@pytest.mark.parametrize('owner', [None, ['tenant-a', 'chat-a'], ['tenant-a', 'other-chat'], ['other-tenant', 'chat-a']])
async def test_implicit_pc_fill_only_blocks_other_owner(monkeypatch, owner):
    page = _make_pc(monkeypatch, owner=owner)
    out = await tools.tool_browser_fill('#name', 'example')
    mismatch = owner is not None and owner != ['tenant-a', 'chat-a']
    assert ('bridge_session_owner_mismatch' in out) is mismatch
    assert bool(page.calls) is not mismatch
    if owner is None:
        assert await tools._submit_guard_error(page) == 'bridge_session_owner_unverified'


@pytest.mark.parametrize('attrs', [{}, {'type': 'submit'}, {'type': 'button', 'tag': 'button'}])
async def test_pc_attrs_present_empty_matrix(monkeypatch, attrs):
    page = _make_pc(monkeypatch, attrs=attrs, owner=['tenant-a', 'chat-a'])
    assert await autosave._probe_attrs(page, '#go') == attrs

    async def no_note(*args, **kwargs):
        return ''

    monkeypatch.setattr(autosave, 'on_submit', no_note)
    out = await tools.tool_browser_click('#go')
    guarded = not attrs or attrs['type'] == 'submit'
    assert ('vault_account_state_unverified' in out) is guarded
    assert bool(page.calls) is not guarded


async def test_unknown_continue_click_does_not_dispatch(monkeypatch):
    page = _make_pc(monkeypatch, owner=['tenant-a', 'chat-a'])
    assert 'vault_account_state_unverified' in await tools.tool_browser_click('#continue')
    assert page.calls == []


def test_r5_browser_connect_select_does_not_issue():
    session = SimpleNamespace(endpoint=SimpleNamespace(metadata={}))
    assert adapter.claim_bridge_session(session, issue=False) == 'bridge_session_owner_unverified'
    assert session.endpoint.metadata == {}
    assert adapter.claim_bridge_session(session, issue=True) == ''
    assert adapter.claim_bridge_session(session, issue=False) == ''


async def test_connect_select_unregistered_never_activates(monkeypatch):
    from app.browser_bridge import service
    session = SimpleNamespace(endpoint=SimpleNamespace(metadata={}))
    fake = SimpleNamespace(sessions=SimpleNamespace(get=lambda sid: session))
    monkeypatch.setattr(service, 'get_browser_bridge_service', lambda: fake)
    assert 'bridge_session_owner_unverified' in await tools.tool_browser_connect('select', session_id='bridge')
    assert session.endpoint.metadata == {}


async def test_bound_submission_still_validates_account_and_scope():
    page = Page()
    safety.remember_credential(page, {'username': 'synthetic', 'work_key': 'wk'}, 'wk',
                               tenant_id='tenant-a', session_id='chat-a', lane='server')
    assert await tools._submit_guard_error(page, browser_work_key='wk', tenant_id='tenant-a') == ''
    assert await tools._submit_guard_error(page, browser_work_key='other', tenant_id='tenant-a') == 'vault_credential_work_key_mismatch'


@pytest.mark.parametrize('work_key', ['wk', None])
async def test_executor_and_browser_tools_share_server_page(monkeypatch, work_key):
    from app.services.work_recipe.executor import BrowserRecipeExecutor

    contexts = {}
    keys = []

    async def evict(ttl):
        pass

    async def headless(key):
        keys.append(key)
        return contexts.setdefault(key, SimpleNamespace(pages=[Page()]))

    fake = SimpleNamespace(_evict_idle_headless_work_contexts=evict,
                           _headless_work_context=headless,
                           _mark_headless_work_context_used=lambda key: None)
    monkeypatch.setattr(adapter, 'get_browser_bridge_service', lambda: fake)
    executor = BrowserRecipeExecutor(browser_work_key=work_key)
    page = await executor._get_page({'action': 'snapshot'})
    context, error = await tools._acquire_pw_context(browser_work_key=work_key or '')
    assert error is None
    assert page is context.pages[0]
    assert keys == [work_key or 'chat:chat-a'] * 2


async def test_matching_pc_owner_can_submit_only_with_verified_binding(monkeypatch):
    page = _make_pc(monkeypatch, owner=['tenant-a', 'chat-a'])
    assert await tools._submit_guard_error(page) == 'vault_account_state_unverified'
    safety.remember_credential(page, {'username': 'synthetic', 'work_key': 'wk'}, 'wk',
                               tenant_id='tenant-a', session_id='chat-a', lane='pc')
    assert await tools._submit_guard_error(page, browser_work_key='wk', tenant_id='tenant-a') == ''


@pytest.mark.parametrize('owner', [None, ['tenant-a', 'other-chat']])
async def test_explicit_bridge_fill_and_submit_use_registry_owner(monkeypatch, owner):
    from app.browser_bridge import service
    page = Page()
    session = SimpleNamespace(endpoint=SimpleNamespace(metadata={}))
    if owner is not None:
        session.endpoint.metadata['aads_input_owner'] = owner
    monkeypatch.setattr(service, 'get_browser_bridge_service', lambda: SimpleNamespace(
        sessions=SimpleNamespace(get=lambda sid: session)))
    error = tools._bridge_input_error(page, 'tenant-a', 'chat-a', submission=False,
                                      browser_session_id='bridge')
    assert error == ('bridge_session_owner_mismatch' if owner else '')
    assert await tools._submit_guard_error(page, browser_session_id='bridge') == (
        'bridge_session_owner_mismatch' if owner else 'bridge_session_owner_unverified')


def test_bridge_owner_uses_bound_tenant_over_tool_argument():
    session = SimpleNamespace(endpoint=SimpleNamespace(metadata={
        'aads_input_owner': ['other-tenant', 'chat-a']}))
    assert adapter.claim_bridge_session(session, tenant_id='other-tenant') == 'bridge_session_owner_mismatch'


async def test_unbound_acquisition_keeps_fill_available():
    tenant = current_tenant_id.set('')
    chat = current_chat_session_id.set('')
    session = SimpleNamespace(session_id='bridge', endpoint=SimpleNamespace(metadata={}))
    context = SimpleNamespace(pages=[Page()])

    async def ensure(**kwargs):
        return session

    async def acquire(**kwargs):
        return context, None

    try:
        actual, error = await adapter._acquire_pc_context(
            SimpleNamespace(ensure_work_session=ensure, acquire_playwright_context=acquire),
            None, 'wk', 'about:blank')
        assert actual is context and error is None
        assert session.endpoint.metadata == {}
        assert adapter.claim_bridge_session(session) == 'bridge_session_owner_unverified'
    finally:
        current_tenant_id.reset(tenant)
        current_chat_session_id.reset(chat)
