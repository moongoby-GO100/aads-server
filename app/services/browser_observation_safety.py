"""Request/page scoped browser redaction and credential submission checks.

Redaction values are shared within a browser context for at most 15 minutes;
submission authority remains page bound. Nothing is written to disk. A disappearing
password field alone is never evidence of authentication.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar

import hashlib
import re
import time
from typing import Any
from urllib.parse import quote, quote_plus, urlsplit
from weakref import WeakKeyDictionary, ref

_TRUSTED_SCOPE: ContextVar[dict | None] = ContextVar("browser_trusted_scope", default=None)


@contextmanager
def trusted_caller_context(*, tenant_id: str, session_id: str, lane: str, work_key: str):
    """Bind server-resolved identity; never populate from untrusted tool arguments."""
    token = _TRUSTED_SCOPE.set(dict(tenant_id=tenant_id, session_id=session_id,
                                    lane=lane, work_key=work_key))
    try:
        yield
    finally:
        _TRUSTED_SCOPE.reset(token)


_PAGE_SECRETS: WeakKeyDictionary = WeakKeyDictionary()
_PAGE_CREDENTIALS: WeakKeyDictionary = WeakKeyDictionary()
_PAGE_CONTEXTS: WeakKeyDictionary = WeakKeyDictionary()
_CONTEXT_SECRETS: WeakKeyDictionary = WeakKeyDictionary()
_VALUE_KEYS = {'value', 'text', 'selected_option'}


def is_pc_page(page: Any) -> bool:
    from app.browser_bridge.service import _LocalAgentPage
    return isinstance(page, _LocalAgentPage)


def sensitive_field(node: dict) -> bool:
    return node.get('secret') is True or str(node.get('type', '')).lower() == 'password' or str(
        node.get('autocomplete', '')
    ).lower() in {'current-password', 'new-password'}


def sanitize_observation(value: Any, secrets: tuple[str, ...] = ()) -> Any:
    """Copy only browser observations; names/labels are not secret evidence.

    Short passwords are masked in their bound field and as complete tokens, never
    as substrings of unrelated numbers (password '1' must not corrupt '123').
    """
    found = {s for s in secrets if s and s not in {'[filled]', '[empty]', '[redacted]'}}

    def collect(item: Any, secret: bool = False) -> None:
        if isinstance(item, dict):
            secret = sensitive_field(item)
            for key, child in item.items():
                if secret and key in _VALUE_KEYS and isinstance(child, str) and child:
                    found.add(child)
                collect(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                collect(child)

    collect(value)
    # One pass also handles overlapping/repeated secrets without re-redacting markers.
    variants = {variant for secret in found
                for variant in (secret, quote(secret, safe=''), quote_plus(secret))}
    patterns = []
    for variant in sorted(variants, key=len, reverse=True):
        escaped = re.escape(variant)
        # Only percent hex is case insensitive; ordinary secret text stays exact.
        escaped = re.sub(r'%[0-9A-Fa-f]{2}', lambda match: '%' + ''.join(
            f'[{char.lower()}{char.upper()}]' if char.isalpha() else char
            for char in match[0][1:]), escaped)
        patterns.append(escaped if len(variant) >= 4 else r'(?<!\w)' + escaped + r'(?!\w)')
    pattern = re.compile('|'.join(patterns)) if patterns else None

    def clean(item: Any, secret: bool = False) -> Any:
        if isinstance(item, dict):
            secret = sensitive_field(item)
            # browser.observation_redaction_boundary_bypass: a control key does
            # not attest its value. Recursion preserves numbers/bools and types.
            result = {key: clean(child) for key, child in item.items()}
            if secret:
                result['secret'] = True
                result['filled'] = bool(item.get('filled') or any(item.get(k) for k in _VALUE_KEYS))
                for key in _VALUE_KEYS & item.keys():
                    result[key] = '[filled]' if result['filled'] else '[empty]'
            return result
        if isinstance(item, (list, tuple)):
            return tuple(clean(child) for child in item) if isinstance(item, tuple) else [clean(child) for child in item]
        return pattern.sub('[redacted]', item) if pattern and isinstance(item, str) else item

    return clean(value)


def remember_credential(page: Any, cred: dict, work_key: str, *, guard: bool = True,
                        tenant_id: str = '', session_id: str = '', lane: str = '') -> None:
    from app.browser_bridge.aads_adapter import normalize_browser_lane
    trusted = _TRUSTED_SCOPE.get() or {}
    work_key = work_key or trusted.get('work_key', '')
    tenant_id = tenant_id or trusted.get('tenant_id', '')
    session_id = session_id or trusted.get('session_id', '')
    lane = lane or trusted.get('lane', '')
    if cred.get('work_key') and cred['work_key'] != work_key:
        raise ValueError('vault_credential_work_key_mismatch')
    values = tuple(set(page_secrets(page) + (str(cred.get('password') or ''),)))
    _PAGE_SECRETS[page] = (time.monotonic() + 900, values)
    context = _page_context(page)
    if context is not None:
        _CONTEXT_SECRETS[context] = (time.monotonic() + 900, values)
    if guard:
        _PAGE_CREDENTIALS[page] = {
            'username': str(cred.get('username') or ''),
            'work_key': str(cred.get('work_key') or work_key),
            'tenant_id': tenant_id, 'session_id': session_id, 'lane': normalize_browser_lane(lane),
            'origin': _origin(page), 'target_id': _target_id(page),
            'expires': time.monotonic() + 900,
        }


def register_page_context(page: Any, context: Any) -> None:
    """Share redaction knowledge with sibling tabs, never submission authority."""
    try:
        _PAGE_CONTEXTS[page] = ref(context)
    except TypeError:
        pass  # Test/legacy adapters may expose non-weak-referenceable contexts.


def _page_context(page: Any) -> Any:
    context_ref = _PAGE_CONTEXTS.get(page)
    context = context_ref() if context_ref is not None else getattr(page, 'context', None)
    try:
        hash(context)
        ref(context)
        return context
    except TypeError:
        return None


def page_secrets(page: Any) -> tuple[str, ...]:
    if page is None:
        return ()
    expires, values = _PAGE_SECRETS.get(page, (0, ()))
    if expires <= time.monotonic():
        _PAGE_SECRETS.pop(page, None)
        values = ()
    context = _page_context(page)
    if context is not None:
        expires, shared = _CONTEXT_SECRETS.get(context, (0, ()))
        if expires > time.monotonic():
            values = tuple(set(values + shared))
        else:
            _CONTEXT_SECRETS.pop(context, None)
    return values


def _target_id(page: Any) -> str:
    target = getattr(page, '_target_id', '')
    return target if isinstance(target, str) else ''


def _origin(page: Any) -> str:
    p = urlsplit(str(getattr(page, 'url', '') or ''))
    return f'{p.scheme}://{p.netloc}'


_ACCOUNT_STATE_JS = """expected => {
    const visible = el => !!(el.getClientRects().length);
    const inputs = Array.from(document.querySelectorAll('input')).filter(visible);
    const ids = inputs.filter(el => el.type !== 'password' &&
        /username|user.?id|email|login.?id|아이디|이메일/i.test(
            [el.name,el.id,el.autocomplete,el.placeholder,el.getAttribute('aria-label')].join(' ')));
    const login = inputs.some(el => el.type === 'password' ||
        ['current-password','new-password'].includes(el.autocomplete));
    const mismatch = ids.some(el => el.value && el.value !== expected);
    const account = ids.some(el => el.value === expected) ||
        Array.from(document.querySelectorAll('[data-username],[data-account], [aria-label]'))
        .filter(visible).some(el => [el.getAttribute('data-username'),el.getAttribute('data-account'),
            el.getAttribute('aria-label')].includes(expected)) ||
        Array.from(document.querySelectorAll('header span,nav span,[role=button], [role=menuitem]'))
        .filter(visible).some(el => (el.textContent || '').trim() === expected);
    const logout = Array.from(document.querySelectorAll('button,a,input[type=submit]'))
        .filter(visible).some(el => /^(로그아웃|log out|logout|sign out)$/i.test((el.textContent || el.value || '').trim()));
    return {login, mismatch, account, authenticated: !login && account && logout,
        document: performance.timeOrigin};
}"""


async def check_credential_page(page: Any, cred: dict, work_key: str, *, submission: bool = False) -> str:
    work_key = work_key or (_TRUSTED_SCOPE.get() or {}).get('work_key', '')
    if not str(cred.get('username') or '').strip():
        return 'vault_account_state_unverified'
    if not work_key or not cred.get('work_key') or cred['work_key'] != work_key:
        return 'vault_credential_work_key_mismatch'
    try:
        state = await page.evaluate(_ACCOUNT_STATE_JS, str(cred.get('username') or ''))
        if not isinstance(state, dict):
            return 'vault_account_state_unverified'
        if state.get('mismatch'):
            return 'vault_account_mismatch'
        if state.get('login'):
            # Empty username is permitted for filling, but never for submission.
            return '' if not submission or state.get('account') is True else 'vault_account_state_unverified'
        if state.get('authenticated') is True and state.get('account') is True:
            return ''
    except Exception:
        pass
    return 'vault_account_state_unverified'


async def submission_guard(page: Any, work_key: str, *, tenant_id: str = '',
                           session_id: str = '', lane: str = '') -> str:
    from app.browser_bridge.aads_adapter import normalize_browser_lane
    trusted = _TRUSTED_SCOPE.get() or {}
    tenant_id = tenant_id or trusted.get('tenant_id', '')
    session_id = session_id or trusted.get('session_id', '')
    resolved_lane = lane or trusted.get('lane', '')
    lane = normalize_browser_lane(resolved_lane)
    work_key = work_key or trusted.get('work_key', '')
    cred = _PAGE_CREDENTIALS.get(page)
    if not cred:
        return ''
    if not resolved_lane or not tenant_id or not session_id:
        return 'vault_credential_scope_mismatch'
    if not cred.get('tenant_id') or not cred.get('session_id'):
        return 'vault_credential_scope_mismatch'
    if cred.get('target_id') and cred['target_id'] != _target_id(page):
        return 'vault_credential_scope_mismatch'
    if cred['expires'] <= time.monotonic() or cred['origin'] != _origin(page):
        return 'vault_account_state_unverified'
    for key, actual in (('tenant_id', tenant_id), ('session_id', session_id), ('lane', lane)):
        if cred.get(key) != actual:
            return 'vault_credential_scope_mismatch'
    error = await check_credential_page(page, cred, work_key, submission=True)
    if not error and not cred.get('target_id'):
        cred['target_id'] = _target_id(page)
    return error


def safe_error_code(code: Any) -> str:
    allowed = {'PC_AGENT_DEADLINE_EXCEEDED', 'COMMAND_TIMEOUT', 'TARGET_MISMATCH',
               'CLICK_DISPATCH_UNCERTAIN', 'SNAPSHOT_UNSTABLE', 'INPUT_FILL_NOT_CONFIRMED',
               'PC_AGENT_OFFLINE', 'NO_CAPABLE_AGENT', 'CDP_NOT_READY', 'CLICK_NOT_VERIFIED',
               'ELEMENT_NOT_ACTIONABLE', 'ELEMENT_NOT_FOUND', 'SELECTOR_AMBIGUOUS',
               'ELEMENT_DISABLED', 'ELEMENT_HIDDEN', 'ELEMENT_OBSCURED'}
    return code if isinstance(code, str) and code in allowed else 'BROWSER_OPERATION_FAILED'


def safe_error(exc: Exception, *, operation: str = "browser_operation",
               secrets: tuple[str, ...] = ()) -> str:
    """Render diagnostics at an external boundary with caller-known secrets removed.

    Pass the request/page secrets here; never replace or mutate the exception used
    for retries, classification, or propagation.
    """
    from app.browser_bridge.pc_agent_budget import PcAgentDeadlineExceeded

    try:
        raw = str(exc).lower()
    except Exception:
        raw = ''
    code = next((code for pattern, code in (
        ('pc_atomic_mask_capture_unsupported', 'pc_atomic_mask_capture_unsupported'),
        ('renderer crashed', 'renderer_crashed'), ('timeout', 'browser_timeout'),
        ('strict mode violation', 'selector_ambiguous'), ('detached', 'target_detached'),
        ('navigation', 'navigation_unstable'), ('closed', 'target_closed'),
    ) if pattern in raw), 'browser_operation_failed')
    if isinstance(exc, PcAgentDeadlineExceeded):
        code = 'pc_agent_deadline_exceeded'
    operation = operation if re.fullmatch(r'(?:tool_)?browser_[a-z_]+|tool_capture_screenshot', operation) else 'browser_operation'
    # These categories come from the bridge contract, never arbitrary page text.
    error_code = getattr(exc, 'error_code', '')
    error_code = (sanitize_observation(error_code, secrets)
                  if isinstance(error_code, str) and re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]{0,79}', error_code)
                  else 'BROWSER_OPERATION_FAILED')
    reason = getattr(exc, 'reason_code', '')
    if isinstance(reason, str) and re.fullmatch(r'[A-Za-z_]{1,80}', reason):
        code = sanitize_observation(reason, secrets)
    # Render only at the external observation boundary; leave the exception intact.
    message = sanitize_observation(str(exc), secrets)
    from app.services.browser_artifact_state import redact_artifact_url
    message = re.sub(r'https?://[^\s]+', lambda m: redact_artifact_url(m[0]), message)
    detail = getattr(exc, 'detail', None)
    retry_safe = detail.get('retry_safe') if isinstance(detail, dict) else getattr(exc, 'retry_safe', None)
    retry = 'true' if retry_safe is True else 'false' if retry_safe is False else 'unknown'
    return (f'reason_code={code} operation={operation} error_code={error_code} '
            f'retry_safe={retry} last_error={type(exc).__name__} next_action=browser_snapshot '
            f'detail={message}')


async def masked_screenshot(page: Any, *, full_page: bool = False, timeout: int = 15000) -> bytes:
    if is_pc_page(page):
        # The current bridge drops **kwargs; no atomic mask/capture/target attestation.
        raise ValueError('pc_atomic_mask_capture_unsupported')
    masks = [page.locator('input,textarea,[contenteditable=true]')]
    for secret in page_secrets(page):
        if secret:
            masks.append(page.get_by_text(secret, exact=len(secret) < 4))
    return await page.screenshot(full_page=full_page, timeout=timeout, mask=masks)


def safe_page_url(page: Any, url: str = '') -> str:
    from app.services.browser_artifact_state import redact_artifact_url
    secrets = page_secrets(page)
    return sanitize_observation(redact_artifact_url(url or page.url), secrets)


async def collect_safe_evidence(page: Any, *, tenant_id: str, session_id: str = '', work_key: str = '',
                                read_snapshot: Any = None) -> dict:
    from app.services.browser_artifact_state import save_screenshot_artifact
    if not str(tenant_id or '').strip():
        raise ValueError('artifact_tenant_required')
    # Check capability before collecting or persisting any PC evidence.
    if is_pc_page(page):
        raise ValueError('pc_atomic_mask_capture_unsupported')
    if read_snapshot is None:
        raise ValueError('safe_snapshot_reader_required')
    observation = await read_snapshot(page)
    observation = sanitize_observation(observation, page_secrets(page))
    png = await masked_screenshot(page)
    ref = save_screenshot_artifact(png=png, tenant_id=str(tenant_id), session_id=session_id,
                                   work_key=work_key, url=safe_page_url(page), source='dom_evidence')
    return {'url': safe_page_url(page), 'dom': {'text': observation[:4000]} if isinstance(observation, str) else observation,
            'screenshot': {'status': 'captured', 'url': ref['ref'],
                           'sha256': hashlib.sha256(png).hexdigest(), 'bytes': len(png)}}
