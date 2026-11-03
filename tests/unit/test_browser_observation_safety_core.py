"""Synthetic contracts for the helper, without CEO tools or browser processes."""
import asyncio
import copy
from urllib.parse import quote, quote_plus

import pytest

from app.services import browser_observation_safety as safety


class Page:
    url = 'https://example.test/login'
    _target_id = 'tab-1'

    async def evaluate(self, script, username):
        return {'login': True, 'account': True}


def run(awaitable):
    return asyncio.run(awaitable)


def bind(page):
    safety.remember_credential(page, {'username': 'synthetic', 'password': 'pw-secret',
                                     'work_key': 'wk'}, 'wk', tenant_id='tenant',
                               session_id='session', lane='server')


def test_structured_types_short_secret_controls_and_aria_siblings():
    raw = {'children': [
        {'type': 'password', 'value': '1', 'children': [
            {'role': 'button', 'name': 'Continue', 'value': '12'}]},
        {'role': 'textbox', 'value': '123', 'aria-label': 'Address'}],
        'count': 1, 'ready': True, 'reason_code': 'ready', 'ratio': 1.5,
        'tuple': ('1', '12'), 'text': 'token 1 and 123'}
    before = copy.deepcopy(raw)
    clean = safety.sanitize_observation(raw)
    assert raw == before
    assert clean['children'][0]['value'] == '[filled]'
    assert clean['children'][0]['children'][0]['value'] == '12'
    assert clean['children'][1] == raw['children'][1]
    assert clean['tuple'] == ('[redacted]', '12')
    assert clean['count'] == 1 and clean['ready'] is True and clean['ratio'] == 1.5
    assert clean['reason_code'] == 'ready'
    assert clean['text'] == 'token [redacted] and 123'


def test_known_secrets_overlap_and_normal_input_does_not_register():
    page = Page()
    value = {'type': 'text', 'value': '12'}
    assert safety.sanitize_observation(value) == value
    assert safety.page_secrets(page) == ()
    assert safety.sanitize_observation('abcd abc', ('abc', 'abcd')) == '[redacted] [redacted]'


@pytest.mark.parametrize('field,value', [('tenant_id', 'other'), ('session_id', 'other'),
                                        ('lane', 'pc'), ('work_key', 'other')])
def test_explicit_scope_mismatch(field, value):
    page = Page()
    bind(page)
    scope = dict(tenant_id='tenant', session_id='session', lane='server', work_key='wk')
    scope[field] = value
    assert 'mismatch' in run(safety.submission_guard(page, **scope))


def test_omission_requires_trusted_context_and_does_not_override_explicit():
    page = Page()
    bind(page)
    assert run(safety.submission_guard(page, '')) == 'vault_credential_scope_mismatch'
    with safety.trusted_caller_context(tenant_id='tenant', session_id='session',
                                       lane='server', work_key='wk'):
        assert run(safety.submission_guard(page, '')) == ''
        assert 'mismatch' in run(safety.submission_guard(page, 'wrong'))
    assert run(safety.submission_guard(page, '')) == 'vault_credential_scope_mismatch'


def test_tab_origin_expiry_and_uncredentialed_browsing(monkeypatch):
    page = Page()
    assert run(safety.submission_guard(page, '')) == ''
    bind(page)
    scope = dict(tenant_id='tenant', session_id='session', lane='server')
    page._target_id = 'tab-2'
    assert 'scope_mismatch' in run(safety.submission_guard(page, 'wk', **scope))
    page._target_id = 'tab-1'
    page.url = 'https://other.test/'
    assert 'unverified' in run(safety.submission_guard(page, 'wk', **scope))
    page.url = Page.url
    monkeypatch.setattr(safety.time, 'monotonic', lambda: 10**20)
    assert safety.page_secrets(page) == ()
    assert 'unverified' in run(safety.submission_guard(page, 'wk', **scope))


def test_sibling_redaction_never_shares_submission_authority():
    context = Page()
    first, sibling = Page(), Page()
    safety.register_page_context(first, context)
    safety.register_page_context(sibling, context)
    bind(first)
    assert 'pw-secret' in safety.page_secrets(sibling)
    assert run(safety.submission_guard(sibling, '')) == ''


def test_error_boundary_preserves_exception_identity_and_diagnostics():
    exc = RuntimeError('timeout connecting https://user:pw@example.test/a?token=pw-secret#pw-secret')
    exc.reason_code = 'navigation_unstable'
    exc.error_code = 'COMMAND_TIMEOUT'
    exc.detail = {'retry_safe': True}
    before = (exc.args, copy.deepcopy(exc.__dict__))
    rendered = safety.safe_error(exc, secrets=('pw-secret',))
    assert 'pw-secret' not in rendered and 'user:pw' not in rendered
    assert 'timeout connecting https://example.test/a' in rendered
    assert 'reason_code=navigation_unstable' in rendered
    assert 'error_code=COMMAND_TIMEOUT' in rendered and 'retry_safe=true' in rendered
    assert (exc.args, exc.__dict__) == before
    try:
        raise exc
    except RuntimeError as caught:
        assert caught is exc


def test_safe_url_removes_authority_query_fragment_and_encoded_secret():
    page = Page()
    safety.remember_credential(page, {'password': 'a b'}, '', guard=False)
    assert safety.safe_page_url(page, 'https://user:pass@example.test/a%20b?q=hidden#hidden') == (
        'https://example.test/[redacted]')


def test_unknown_password_is_masked_without_global_registration(monkeypatch):
    class ScreenshotPage(Page):
        def locator(self, selector):
            assert selector == 'input,textarea,[contenteditable=true]'
            return 'all-inputs'

        async def screenshot(self, **kwargs):
            assert kwargs['mask'] == ['all-inputs']
            return b'synthetic'

    page = ScreenshotPage()
    monkeypatch.setattr(safety, 'is_pc_page', lambda page: False)
    assert run(safety.masked_screenshot(page)) == b'synthetic'
    assert safety.page_secrets(page) == ()
    clean = safety.sanitize_observation({'type': 'password', 'value': 'unknown'})
    assert clean['value'] == '[filled]' and clean['secret'] is True


def test_pc_evidence_fails_before_snapshot_or_storage(monkeypatch):
    monkeypatch.setattr(safety, 'is_pc_page', lambda page: True)
    with pytest.raises(ValueError, match='pc_atomic_mask_capture_unsupported'):
        run(safety.masked_screenshot(Page()))
    with pytest.raises(ValueError, match='pc_atomic_mask_capture_unsupported'):
        run(safety.collect_safe_evidence(Page(), tenant_id='tenant'))
    with pytest.raises(ValueError, match='artifact_tenant_required'):
        run(safety.collect_safe_evidence(Page(), tenant_id=''))


def test_private_artifact_contract_with_structured_evidence(monkeypatch):
    from app.services import browser_artifact_state as artifacts

    saved = {}

    def save(**kwargs):
        saved.update(kwargs)
        return {'ref': '/api/v1/browser-artifacts/synthetic'}

    async def screenshot(page):
        return b'synthetic'

    async def snapshot(page):
        return {'type': 'password', 'value': 'unknown'}

    monkeypatch.setattr(safety, 'is_pc_page', lambda page: False)
    monkeypatch.setattr(safety, 'masked_screenshot', screenshot)
    monkeypatch.setattr(artifacts, 'save_screenshot_artifact', save)
    result = run(safety.collect_safe_evidence(Page(), tenant_id='tenant',
                                             session_id='session', work_key='wk',
                                             read_snapshot=snapshot))
    assert result['dom']['value'] == '[filled]'
    assert result['screenshot']['url'] == '/api/v1/browser-artifacts/synthetic'
    assert saved['tenant_id'] == 'tenant' and saved['session_id'] == 'session'
    assert saved['work_key'] == 'wk'


def test_public_account_check_resolves_only_trusted_work_key():
    page = Page()
    cred = {'username': 'synthetic', 'work_key': 'wk'}
    assert run(safety.check_credential_page(page, cred, '')) == 'vault_credential_work_key_mismatch'
    with safety.trusted_caller_context(tenant_id='tenant', session_id='session',
                                       lane='server', work_key='wk'):
        assert run(safety.check_credential_page(page, cred, '')) == ''
        assert 'mismatch' in run(safety.check_credential_page(page, cred, 'different'))


def test_registration_rejects_explicit_work_key_mismatch_before_remembering():
    page = Page()
    with pytest.raises(ValueError, match='vault_credential_work_key_mismatch'):
        safety.remember_credential(page, {'password': 'synthetic', 'work_key': 'other'}, 'wk')
    assert safety.page_secrets(page) == ()


def test_custom_error_contract_and_deadline_type_remain_actionable():
    from app.browser_bridge.pc_agent_budget import PcAgentDeadlineExceeded

    exc = PcAgentDeadlineExceeded('budget expired')
    exc.retry_safe = False
    assert 'pc_agent_deadline_exceeded' in safety.safe_error(exc)
    assert 'retry_safe=false' in safety.safe_error(exc)
    custom = RuntimeError('synthetic diagnostic')
    custom.error_code = 'CUSTOM_BRIDGE_42'
    custom.reason_code = 'bridge_retry'
    assert 'error_code=CUSTOM_BRIDGE_42' in safety.safe_error(custom)
    assert 'reason_code=bridge_retry' in safety.safe_error(custom)


@pytest.mark.parametrize('key', ['status', 'reason_code', 'error_code', 'target_id',
                                  'operation', 'retry_safe', 'count', 'ready'])
def test_control_fields_cannot_attest_secret_values(key):
    secret = '-'.join(('synthetic', 'passphrase', '42'))
    raw = {'payload': {key: {'status': secret, 'children': [secret, ('prefix ' + secret, 42, True)]}},
           'count': 42, 'ready': True, 'ratio': 1.5, 'optional': None}
    before = copy.deepcopy(raw)
    clean = safety.sanitize_observation(raw, (secret,))
    assert secret not in str(clean)
    assert clean['payload'][key]['status'] == '[redacted]'
    assert clean['payload'][key]['children'] == ['[redacted]', ('prefix [redacted]', 42, True)]
    assert type(clean['count']) is int and clean['count'] == 42
    assert clean['ready'] is True and clean['ratio'] == 1.5 and clean['optional'] is None
    assert raw == before
    assert safety.sanitize_observation({key: secret}, (secret,))[key] == '[redacted]'


def test_control_tokens_preserved_without_registering_normal_values():
    page = Page()
    raw = {'status': 'ready', 'reason_code': 'navigation_unstable',
           'error_code': 'COMMAND_TIMEOUT', 'target_id': 'tab-1',
           'count': 1, 'ready': True, 'text': 'ready 1 123 tab-1'}
    assert safety.sanitize_observation(raw) == raw
    assert safety.page_secrets(page) == ()
    clean = safety.sanitize_observation(raw, ('1',))
    assert clean['target_id'] == 'tab-[redacted]'
    assert clean['count'] == 1 and clean['ready'] is True
    assert clean['status'] == 'ready'
    assert clean['text'] == 'ready [redacted] 123 tab-[redacted]'
    assert safety.page_secrets(page) == ()
    assert safety.sanitize_observation({'status': 'ready'}, ('ready',)) == {'status': '[redacted]'}


@pytest.mark.parametrize('encoding', ['raw', 'quote', 'quote_plus', 'lower', 'mixed'])
def test_error_and_page_urls_redact_encoded_secrets_without_mutating_exception(encoding):
    secret = 'synthetic password/42?é'
    encoded = {'raw': secret, 'quote': quote(secret, safe=''),
               'quote_plus': quote_plus(secret),
               'lower': quote(secret, safe='').lower(),
               'mixed': quote_plus(secret).replace('%2F', '%2f').replace('%C3', '%c3')}[encoding]
    url = 'https://user:password@example.test/' + encoded + '?token=hidden#hidden'
    exc = RuntimeError('timeout at ' + url)
    exc.reason_code = 'navigation_unstable'
    exc.error_code = 'COMMAND_TIMEOUT'
    exc.detail = {'retry_safe': True, 'diagnostic': 'retained'}
    before = (exc.args, copy.deepcopy(exc.__dict__))
    rendered = safety.safe_error(exc, secrets=(secret,))
    assert secret not in rendered and encoded not in rendered
    assert 'user:password' not in rendered and 'hidden' not in rendered
    assert 'https://example.test/[redacted]' in rendered
    assert 'reason_code=navigation_unstable' in rendered
    assert 'error_code=COMMAND_TIMEOUT' in rendered and 'retry_safe=true' in rendered
    assert 'timeout at' in rendered and 'last_error=RuntimeError' in rendered
    assert (exc.args, exc.__dict__) == before
    try:
        raise exc
    except RuntimeError as caught:
        assert caught is exc
    page = Page()
    safety.remember_credential(page, {'password': secret}, '', guard=False)
    # Page URLs are serialized; a raw '?' is a URL delimiter, not path content.
    if encoding != 'raw':
        assert safety.safe_page_url(page, url) == 'https://example.test/[redacted]'
    assert safety.sanitize_observation('SYNTHETIC PASSWORD/42?É', (secret,)) == 'SYNTHETIC PASSWORD/42?É'
