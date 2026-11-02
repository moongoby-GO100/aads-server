"""Read Gemini web limits through an explicitly selected, existing CDP session.

This is not Antigravity quota and must never feed model admission controls.
Bindings are restored only from the dedicated authenticated DB registry.
Overview reads the cache; explicit refresh and the active-slot scheduler collect.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from urllib.parse import urlsplit

SOURCE = "https://gemini.google.com/usage?hl=ko"
TTL = 300
COLLECTION_TIMEOUT = 20
CLEANUP_TIMEOUT = 20
MAX_KEYS = 128
CLEANUP_MIN_AGE = 180
# Lookup reasons that prove the PC no longer holds an owned tab for this request.
GONE_REASONS = frozenset({'target_missing', 'confirmed_target_absent'})
logger = logging.getLogger(__name__)
DOM = """() => ({url: location.href, text: document.body.innerText,
 identity: Array.from(document.querySelectorAll('[aria-label]'))
 .map(e => e.getAttribute('aria-label')).filter(s => /Google 계정|Google Account/.test(s)).join(' ')})"""


def unavailable(status):
    return {"status": status, "source_url": SOURCE, "scope": "gemini_web",
            "source_label": "Gemini 웹 사용량", "windows": [], "observed_at": None,
            "account": None, "reason": status, "recovery_url": SOURCE,
            "connection_url": "/api/v1/llm-keys/gemini-web-usage/connect",
            "refresh_url": "/api/v1/llm-keys/gemini-web-usage/refresh"}


def tab_gone(exc, depth=0):
    """True only when a PC lookup error names a reason proving the owned tab is gone."""
    detail = getattr(exc, 'detail', exc)
    if depth > 4 or not isinstance(detail, dict):
        return False
    if detail.get('reason') in GONE_REASONS:
        return True
    return any(tab_gone(v, depth + 1) for v in detail.values() if isinstance(v, dict))


def parse_dom(dom, expected_account, now=None):
    """Keep ambiguous reset labels raw. Never infer a year, timezone or duration."""
    now = now or datetime.now(timezone.utc)
    url = urlsplit(dom.get("url", ""))
    if url.scheme != "https" or url.hostname != "gemini.google.com" or url.path.rstrip("/") != "/usage":
        return unavailable("auth_required")
    emails = set(re.findall(r"[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}", dom.get("identity", "").casefold()))
    if not expected_account or not emails:
        return unavailable("identity_unknown")
    if emails != {expected_account.strip().casefold()}:
        return unavailable("account_mismatch")
    text = dom.get("text", "")
    windows = []
    labels = list(re.finditer(r"현재 사용량|주간 한도", text))
    if len(labels) != 2 or [m[0] for m in labels] != ["현재 사용량", "주간 한도"]:
        return unavailable("collection_failed")
    for i, name in enumerate(("current", "weekly")):
        section = text[labels[i].end():labels[i + 1].start() if i == 0 else len(text)]
        match = re.match(r"\s*([0-9]+(?:\.[0-9]+)?)\s*%\s*([^%]*?)초기화", section)
        if not match or not 0 <= float(match[1]) <= 100 or not match[2].strip():
            return unavailable("collection_failed")
        windows.append({"name": name, "used_percent": float(match[1]),
                        "reset_raw": match[2].strip(), "resets_at": None, "window_minutes": None})
    # Body menus/promotions are not evidence of a subscription tier.
    return {**unavailable("available"), "account": expected_account.strip().casefold(),
            "plan": None, "windows": windows,
            "observed_at": now.isoformat(), "reason": None}


def fresh(sample, now=None):
    try:
        observed = datetime.fromisoformat(sample["observed_at"])
        age = ((now or datetime.now(timezone.utc)) - observed).total_seconds()
        return observed.tzinfo is not None and 0 <= age < TTL
    except (KeyError, TypeError, ValueError, AttributeError):
        return False


def select_session(service, owner, tenant):
    # Select only a dedicated registered work lane owned by this authenticated
    # administrator. Missing scope metadata is not permission to use a profile.
    candidates = [s for s in service.sessions.list_sessions()
                  if s.created_by == owner and owner and tenant and not s.is_expired
                  and s.work_key == "extai-gemini-web-usage"
                  and s.endpoint.metadata.get("tenant_id") == tenant
                  and s.endpoint.metadata.get("profile_lane") == "extai_fixed"
                  and s.endpoint.kind.value in {"cdp", "websocket", "local_agent"}
                  and getattr(service, '_gemini_usage_bindings', {}).get(s.session_id) ==
                      (owner, tenant, s.endpoint.metadata.get('agent_id'),
                       s.endpoint.metadata.get('owner_user_id'))]
    return candidates[0] if len(candidates) == 1 else None


class Collector:
    def __init__(self):
        self.samples = {}
        self.locks = {}
        self.tasks = {}
        self.cleanup = set()
        self.pending_cleanup = {}
        self.recovering = set()
        self.requests = set()

    def _key(self, account, owner, tenant):
        return (tenant, owner, (account or '').strip().casefold())

    def cached(self, account, owner='', tenant=''):
        key = self._key(account, owner, tenant)
        sample, expires = self.samples.get(key, (None, 0))
        if sample and expires > time.monotonic() and (sample['status'] != 'available' or fresh(sample)):
            return sample
        return unavailable('identity_unknown' if not key[2] else 'not_collected')

    async def authorized(self, account, owner, tenant):
        try:
            row = await load_binding(owner, tenant, account)
            return bool(row and await registration_active(row['owner_user_id'], tenant))
        except Exception:
            return False

    async def snapshot(self, account, owner='', tenant=''):
        if not account:
            return unavailable('identity_unknown')
        try:
            async with asyncio.timeout(2):
                row = await load_binding(owner, tenant, account)
                if not row:
                    return unavailable('not_connected')
                if not await registration_active(row['owner_user_id'], tenant):
                    return unavailable('pc_offline')
                return stored_snapshot(row) or self.cached(account, owner, tenant)
        except Exception:
            return unavailable('pc_offline')

    def _evict(self):
        # Keep entries with holders/waiters/tasks; removing those would split a key's lock.
        for key in list(self.locks):
            if len(self.locks) < MAX_KEYS:
                break
            lock = self.locks[key]
            if not lock.locked() and not getattr(lock, '_waiters', None) and key not in self.tasks and key not in self.pending_cleanup.values():
                self.locks.pop(key)
                self.samples.pop(key, None)

    def _track_cleanup(self, page_task, key):
        self.pending_cleanup[page_task] = key
        if page_task in self.recovering:
            return
        self.recovering.add(page_task)
        async def recover():
            try:
                async with asyncio.timeout(CLEANUP_TIMEOUT):
                    page = await asyncio.shield(page_task)
                    await page.close()
                    self.pending_cleanup.pop(page_task, None)
            except (Exception, asyncio.CancelledError):
                if page_task.done() and not page_task.cancelled() and page_task.exception() is not None:
                    # No page object exists; the durable cleanup row is the recovery record.
                    self.pending_cleanup.pop(page_task, None)
                logger.warning('gemini_web_usage.cleanup_pending')
            finally:
                self.recovering.discard(page_task)
        task = asyncio.create_task(recover())
        self.cleanup.add(task)
        task.add_done_callback(self.cleanup.discard)


    async def bounded(self, coroutine):
        if len(self.requests) >= MAX_KEYS:
            coroutine.close()
            return unavailable('busy')
        task = asyncio.create_task(coroutine)
        self.requests.add(task)
        def finished(t):
            self.requests.discard(t)
            if not t.cancelled():
                t.exception()
        task.add_done_callback(finished)
        try:
            done, _ = await asyncio.wait({task}, timeout=COLLECTION_TIMEOUT)
            if not done:
                task.cancel()
                return unavailable('timeout')
            return task.result()
        except asyncio.CancelledError:
            task.cancel()
            raise

    async def read(self, account, owner='', tenant=''):
        return await self.bounded(self._read(account, owner, tenant))

    async def _read(self, account, owner="", tenant=""):
        key = (tenant, owner, (account or "").strip().casefold())
        if not key[2]:
            return unavailable("identity_unknown")
        if not await self.authorized(account, owner, tenant):
            return unavailable('pc_offline')
        self._evict()
        if key not in self.locks and len(self.locks) >= MAX_KEYS:
            return unavailable('busy')
        lock = self.locks.setdefault(key, asyncio.Lock())
        async with lock:
            if key in self.tasks and not self.tasks[key].done():
                return unavailable('cleanup_pending')
            if any(k == key for k in self.pending_cleanup.values()):
                return unavailable('cleanup_pending')
            sample, deadline = self.samples.get(key, (None, 0))
            if sample and time.monotonic() < deadline:
                if sample["status"] != "available" or fresh(sample):
                    return sample
            try:
                task = asyncio.create_task(self._collect(key[2], owner, tenant))
                self.tasks[key] = task
                # wait_for waits for cancellation cleanup and can exceed its timeout.
                done, _ = await asyncio.wait({task}, timeout=COLLECTION_TIMEOUT)
                if not done:
                    task.cancel()
                    sample = unavailable('timeout')
                else:
                    sample = task.result()
            except asyncio.CancelledError:
                if key in self.tasks:
                    self.tasks[key].cancel()
                raise
            except TimeoutError:
                sample = unavailable("pc_offline")
            except Exception as exc:
                code = str(getattr(exc, 'error_code', '') or '')
                sample = unavailable('pc_offline' if code in {
                    'PC_AGENT_OFFLINE', 'NO_CAPABLE_AGENT', 'PC_AGENT_ROUTE_UNAVAILABLE',
                    'COMMAND_TIMEOUT', 'LEASE_EXPIRED', 'AGENT_BUSY'} else 'collection_failed')
            finally:
                task = self.tasks.get(key)
                if task is not None:
                    def finished(t, key=key):
                        if self.tasks.get(key) is t:
                            self.tasks.pop(key, None)
                        if not t.cancelled():
                            t.exception()
                    task.add_done_callback(finished)
            lifetime = 30
            if sample["status"] == "available" and fresh(sample):
                observed = datetime.fromisoformat(sample["observed_at"])
                lifetime = max(0, TTL - (datetime.now(timezone.utc) - observed).total_seconds())
            if sample["status"] == "available" and not fresh(sample):
                sample = unavailable("stale")
                lifetime = 30
            self.samples[key] = (sample, time.monotonic() + lifetime)
            return sample

    async def _collect(self, account, owner, tenant):
        async with operation_lease('collect') as lease:
            if lease is None:
                return unavailable('busy')
            if not await account_enabled(account):
                return unavailable('account_disabled')
            row = await load_binding(owner, tenant, account)
            if not row or not await registration_active(row['owner_user_id'], tenant):
                return unavailable('pc_offline')
            if await cleanup_pending():
                return unavailable('cleanup_pending')
            sample = stored_snapshot(row)
            if sample is not None:
                return sample
            sample = await self._collect_page(account, owner, tenant)
            if await cleanup_pending():
                sample = {**sample, 'cleanup_status': 'cleanup_pending'}
            if not await publish_sample(lease, tenant, owner, account, sample):
                return unavailable('lease_expired')
            return sample

    async def _collect_page(self, account, owner, tenant):
        if not owner or not tenant:
            return unavailable("pc_offline")
        from app.browser_bridge.service import get_browser_bridge_service
        service = get_browser_bridge_service()
        session = await restore_session(service, owner, tenant, account)
        if not session:
            return unavailable("pc_offline")
        if session.endpoint.kind.value == 'local_agent':
            from app.browser_bridge.service import _GeminiUsageContext
            if not await verify_agent(session.endpoint.metadata.get('agent_id'),
                                      session.endpoint.metadata.get('owner_user_id'), tenant):
                return unavailable('pc_offline')
            context, error = _GeminiUsageContext(session, service), None
        else:
            context, error = await service.acquire_playwright_context(session.session_id)
        if error or context is None:
            return unavailable("pc_offline")
        page_task = asyncio.create_task(context.new_page())
        page = None
        try:
            page = await asyncio.shield(page_task)
            if session.endpoint.kind.value == 'local_agent':
                await assert_lease()
            await page.goto(SOURCE, wait_until="domcontentloaded", timeout=15000)
            try:
                await page.wait_for_function(
                    """() => location.hostname !== 'gemini.google.com' ||
                    (/현재 사용량/.test(document.body?.innerText || '') &&
                     /주간 한도/.test(document.body?.innerText || '') &&
                     Array.from(document.querySelectorAll('[aria-label]')).some(
                     e => /Google 계정|Google Account/.test(e.getAttribute('aria-label'))))""",
                    timeout=7000)
            except Exception:
                # Parse the final DOM once, including login/identity failures.
                pass
            if session.endpoint.kind.value == 'local_agent':
                await assert_lease()
            return parse_dom(await page.evaluate(DOM), account)
        finally:
            if page is None or asyncio.current_task().cancelling():
                self._track_cleanup(page_task, (tenant, owner, account))
            else:
                close_task = asyncio.create_task(page.close())
                try:
                    await asyncio.shield(close_task)
                except asyncio.CancelledError:
                    # Reuse the in-flight close; never create a second concurrent close.
                    self._track_cleanup(asyncio.create_task(_closing_page(close_task)), (tenant, owner, account))
                    raise
                except Exception:
                    self._track_cleanup(page_task, (tenant, owner, account))


async def _closing_page(task):
    await task
    class Closed:
        async def close(self):
            return
    return Closed()


collector = Collector()


async def refresh_registered():
    """Existing active-slot scheduler polls only explicitly scoped work sessions.

    No session creation, login, active-tab navigation or profile selection.
    Limit to four sessions and the same per-account request coalescing.
    """
    await recover_cleanup()
    for pending in list(collector.pending_cleanup):
        collector._track_cleanup(pending, collector.pending_cleanup[pending])
    from app.core.db_pool import get_pool
    async with get_pool().acquire() as conn:
        rows = await conn.fetch('SELECT * FROM gemini_web_usage_bindings ORDER BY updated_at LIMIT 4')
    # Scheduling must honor the same server account state as HTTP collection.
    from app.api.llm_keys import _gemini_usage_account
    async with asyncio.timeout(5):
        account = await _gemini_usage_account()
    if (not account or not account.get('is_active') or account.get('manual_disabled')
            or not account.get('actual_account')):
        return
    for row in rows:
        if row['account'] == account['actual_account'].strip().casefold():
            await collector.read(row['account'], row['owner_email'], row['tenant_id'])


async def verify_agent(agent_id, user_id, tenant):
    """AgentInfo ownership originates in DB-token-authenticated WS registration.

    Require an active DB registration for this owner/tenant as well.
    Shared-secret/unscoped registrations never satisfy this check.
    """
    from app.services.pc_agent_manager import pc_agent_manager
    if not agent_id or not user_id or not tenant:
        return False
    agent = pc_agent_manager.get_agent(agent_id)
    status = pc_agent_manager.get_agent_status(agent_id)
    if agent is None:
        # Same-backend peer contract; scope comes from WS-token-authenticated
        # AgentInfo, never from browser/session metadata or client registration.
        from app.api.pc_agent import _peer_online_agents_snapshot
        try:
            async with asyncio.timeout(3):
                payload = await _peer_online_agents_snapshot()
            candidates = [a for a in (payload or {}).get('agents', [])
                          if a.get('agent_id') == agent_id]
            if len(candidates) != 1:
                return False
            remote = candidates[0]
            if (remote.get('user_id') != user_id or remote.get('tenant_id') != tenant
                    or not remote.get('is_online')
                    or 'browser_owned_tab' not in remote.get('command_types', [])):
                return False
        except Exception:
            return False
    elif (agent.user_id != user_id or agent.tenant_id != tenant
            or not status or not status.get('is_online')
            or 'browser_owned_tab' not in agent.command_types):
        return False
    return await registration_active(user_id, tenant)


async def registration_active(user_id, tenant):
    from app.core.db_pool import get_pool
    try:
        async with asyncio.timeout(5):
            async with get_pool().acquire() as conn:
                return bool(await conn.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM kakao_pc_agent_tokens "
                    "WHERE user_id::text=$1 AND tenant_id::text=$2 AND is_active=TRUE)",
                    user_id, tenant))
    except Exception:
        return False


async def _connect(agent_id, user, account):
    from app.browser_bridge.service import get_browser_bridge_service, EXTAI_PROFILE_LANE
    owner = str(user.get('email') or '')
    tenant = str(user.get('tenant_id') or '')
    user_id = str(user.get('user_id') or user.get('id') or '')
    if not owner or not tenant or not user_id or not account:
        return unavailable('identity_unknown')
    if not await account_enabled(account):
        return unavailable('account_disabled')
    if not await verify_agent(agent_id, user_id, tenant):
        return unavailable('pc_offline')
    service = get_browser_bridge_service()
    from app.core.db_pool import get_pool
    async with get_pool().acquire() as conn:
        registered = await conn.fetchrow('SELECT * FROM gemini_web_usage_bindings LIMIT 1')
    if registered and (registered['owner_email'], registered['tenant_id'],
                       registered['owner_user_id'], registered['agent_id']) != (owner, tenant, user_id, agent_id):
        return unavailable('ownership_conflict')
    if registered and registered['account'] != account.strip().casefold():
        return unavailable('account_mismatch')
    if await cleanup_pending():
        return unavailable('cleanup_pending')
    existing = service.sessions.find_by_work_key('extai-gemini-web-usage')
    if registered and existing is None:
        if registered['account'] != account.strip().casefold():
            return unavailable('account_mismatch')
        return stored_snapshot(registered) or unavailable('not_collected')
    if existing:
        proof = await load_binding(owner, tenant, account)
        if (not proof or proof['session_id'] != existing.session_id
                or existing.created_by != owner
                or existing.endpoint.metadata.get('tenant_id') != tenant
                or existing.endpoint.metadata.get('owner_user_id') != user_id
                or existing.endpoint.metadata.get('agent_id') != agent_id):
            return unavailable('ownership_conflict')
    # about:blank prevents the shared-lane attachment helper from adopting or
    # navigating an existing Gemini/user tab. Collection creates its own target.
    lease_uncertain.set(True)
    async with asyncio.timeout(COLLECTION_TIMEOUT):
        session = await service.ensure_work_session(
            work_key='extai-gemini-web-usage', agent_id=agent_id, url='about:blank',
            queue_wait_timeout_seconds=10, command_timeout_seconds=30)
    if not await verify_agent(agent_id, user_id, tenant):
        return unavailable('pc_offline')
    metadata = dict(session.endpoint.metadata)
    # A session adopted by work_key is ours only if it is the one proven above or
    # brand new (unscoped, created by the PC-agent path); never overwrite another owner's.
    if not ((existing is not None and session.session_id == existing.session_id)
            or (session.created_by == 'pc_agent' and not metadata.get('tenant_id')
                and not metadata.get('owner_user_id'))):
        return unavailable('ownership_conflict')
    if (metadata.get('agent_id') != agent_id
            or metadata.get('profile_lane') != EXTAI_PROFILE_LANE
            or not metadata.get('profile_dir') or 'isolated-' in metadata['profile_dir']):
        return unavailable('pc_offline')
    await assert_lease()
    session.created_by = owner
    metadata.update(tenant_id=tenant, owner_user_id=user_id,
                    gemini_account=account.strip().casefold())
    session.endpoint.metadata = metadata
    await save_binding(session, owner, tenant, user_id, agent_id, account)
    lease_uncertain.set(False)
    service.sessions.touch(session)
    service._gemini_usage_bindings[session.session_id] = (owner, tenant, agent_id, user_id)
    collector.samples.pop((tenant, owner, account.strip().casefold()), None)
    return unavailable('not_collected')


async def _connect_transaction(agent_id, user, account):
    try:
        async with operation_lease('connect', {
                'tenant_id': str(user.get('tenant_id') or ''),
                'owner_email': str(user.get('email') or ''),
                'owner_user_id': str(user.get('user_id') or user.get('id') or ''),
                'agent_id': agent_id, 'account': (account or '').strip().casefold(),
                'work_key': 'extai-gemini-web-usage'}) as lease:
            if lease is None:
                return unavailable('busy')
            return await _connect(agent_id, user, account)
    except TimeoutError:
        return unavailable('timeout')
    except Exception:
        logger.warning('gemini_web_usage.binding_unavailable')
        return unavailable('pc_offline')


async def save_binding(session, owner, tenant, user_id, agent_id, account):
    import json
    from app.core.db_pool import get_pool
    lease = await assert_lease()
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            await fence(conn, lease)
            result = await conn.execute(
                "INSERT INTO gemini_web_usage_bindings "
                "(tenant_id, owner_email, owner_user_id, agent_id, account, session_id, endpoint) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb) "
                "ON CONFLICT (tenant_id, owner_email) DO UPDATE SET "
                "owner_user_id=EXCLUDED.owner_user_id, agent_id=EXCLUDED.agent_id, "
                "account=EXCLUDED.account, session_id=EXCLUDED.session_id, "
                "endpoint=EXCLUDED.endpoint, updated_at=now(), "
                "sample=CASE WHEN gemini_web_usage_bindings.account=EXCLUDED.account "
                "THEN gemini_web_usage_bindings.sample ELSE NULL END, "
                "sample_until=CASE WHEN gemini_web_usage_bindings.account=EXCLUDED.account "
                "THEN gemini_web_usage_bindings.sample_until ELSE NULL END "
                "WHERE gemini_web_usage_bindings.account=EXCLUDED.account "
                "AND gemini_web_usage_bindings.owner_user_id=EXCLUDED.owner_user_id "
                "AND gemini_web_usage_bindings.agent_id=EXCLUDED.agent_id",
                tenant, owner, user_id, agent_id, account.strip().casefold(),
                session.session_id, json.dumps(session.endpoint.public_dict()))

            if result != 'INSERT 0 1':
                raise RuntimeError('binding scope conflict')


async def load_binding(owner, tenant, account):
    from app.core.db_pool import get_pool
    async with get_pool().acquire() as conn:
        return await conn.fetchrow(
            "SELECT * FROM gemini_web_usage_bindings "
            "WHERE tenant_id=$1 AND owner_email=$2 AND account=$3",
            tenant, owner, account.strip().casefold())


async def restore_session(service, owner, tenant, account):
    """The dedicated DB record, not general session metadata, authorizes restoration."""
    import json
    from app.browser_bridge.models import BrowserBridgeSession, BrowserEndpoint, BrowserEndpointKind
    row = await load_binding(owner, tenant, account)
    if not row or not await verify_agent(row['agent_id'], row['owner_user_id'], tenant):
        return None
    raw = row['endpoint']
    endpoint = json.loads(raw) if isinstance(raw, str) else raw
    metadata = endpoint['metadata']
    if (endpoint['kind'] != 'local_agent' or metadata.get('tenant_id') != tenant
            or metadata.get('owner_user_id') != row['owner_user_id']
            or metadata.get('agent_id') != row['agent_id']
            or metadata.get('profile_lane') != 'extai_fixed'):
        return None
    return BrowserBridgeSession(
        session_id=row['session_id'], label='Gemini 웹 사용량',
        registered_at=row['updated_at'], created_by=owner,
        work_key='extai-gemini-web-usage',
        endpoint=BrowserEndpoint(kind=BrowserEndpointKind.LOCAL_AGENT, metadata=metadata))


def stored_snapshot(row):
    import json
    try:
        sample = row.get('sample')
        sample = json.loads(sample) if isinstance(sample, str) else sample
        if (not isinstance(sample, dict) or row['sample_until'] <= datetime.now(timezone.utc)
                or (sample['status'] == 'available' and not fresh(sample))):
            return None
        return sample
    except (KeyError, TypeError, ValueError):
        return None


async def begin_cleanup(session):
    """Persist intent before dispatch; recovery can resolve a late PC create by scope."""
    import uuid
    from app.core.db_pool import get_pool
    request_id = 'gemini-usage-' + uuid.uuid4().hex
    m = session.endpoint.metadata
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            await fence(conn, current_lease.get())
            return await _insert_cleanup(conn, request_id, m, session)


async def _insert_cleanup(conn, request_id, m, session):
    count = await conn.fetchval('SELECT count(*) FROM gemini_web_usage_cleanup')
    if count >= MAX_KEYS:
        raise RuntimeError('cleanup capacity reached')
    await conn.execute(
        'INSERT INTO gemini_web_usage_cleanup (request_id,tenant_id,owner_email,account) '
        'VALUES ($1,$2,$3,$4)', request_id, m['tenant_id'], session.created_by, m['gemini_account'])
    return request_id


async def finish_cleanup(request_id):
    from app.core.db_pool import get_pool
    async with get_pool().acquire() as conn:
        await conn.execute('DELETE FROM gemini_web_usage_cleanup WHERE request_id=$1', request_id)


async def _recover_cleanup():
    """Bounded passes, explicit ownership proof, no lease-based closure assumption."""
    from app.core.db_pool import get_pool
    from app.browser_bridge.service import (
        BrowserBridgeError, get_browser_bridge_service, _GeminiUsagePage)
    service = get_browser_bridge_service()
    async with get_pool().acquire() as conn:
        rows = await conn.fetch('SELECT * FROM gemini_web_usage_cleanup WHERE retry_after <= now() ORDER BY retry_after, created_at LIMIT 4')
    for row in rows:
        try:
            async with asyncio.timeout(CLEANUP_TIMEOUT):
                session = await restore_session(service, row['owner_email'], row['tenant_id'], row['account'])
                if session is None:
                    await defer_cleanup(row['request_id'], 'ownership_unverified')
                    continue
                if row['work_key'] != 'extai-fixed-profile':
                    await defer_cleanup(row['request_id'], 'scope_mismatch')
                    continue
                m = session.endpoint.metadata
                await assert_lease()
                try:
                    proof = await service._run_extai_pc_command('browser_owned_tab', dict(
                        op='lookup', by_scope=True, work_key=row['work_key'],
                        tenant_id=row['tenant_id'], chat_id=row['request_id'], port=int(m['port'])),
                        agent_id=m['agent_id'], queue_wait_timeout_seconds=5,
                        command_timeout_seconds=10, single_route_attempt=True)
                except BrowserBridgeError as exc:
                    proof = exc
                if proof is not None and not isinstance(proof, Exception) and (
                        proof.get('target_id') and proof.get('owner_token')):
                    await assert_lease()
                    page = _GeminiUsagePage(session, service, proof['target_id'],
                                            proof['owner_token'], row['request_id'])
                    await page.close()
                elif tab_gone(proof) and _aged(row):
                    # Aged past any in-flight create: nothing we can prove ours remains.
                    await finish_cleanup(row['request_id'])
                    logger.warning('gemini_web_usage.cleanup_released_unowned')
                else:
                    await defer_cleanup(row['request_id'], 'ownership_unverified')
        except Exception:
            await defer_cleanup(row['request_id'], 'retry_required')
            logger.warning('gemini_web_usage.cleanup_pending')


def _aged(row):
    created = row.get('created_at') if hasattr(row, 'get') else None
    return bool(created and (datetime.now(timezone.utc) - created).total_seconds() >= CLEANUP_MIN_AGE)


async def connect(agent_id, user, account):
    return await collector.bounded(_connect_transaction(agent_id, user, account))


# One global work lane, hence one bounded DB row. Tokens are never reused.
# Expiry fences writes; it does NOT prove a browser operation has stopped.
current_lease = ContextVar('gemini_usage_lease', default=None)
lease_uncertain = ContextVar('gemini_usage_lease_uncertain', default=False)
LEASE_SECONDS = 20


class LeaseExpired(RuntimeError):
    pass


async def fence(conn, token):
    row = await conn.fetchrow(
        "SELECT token FROM gemini_web_usage_lease WHERE lane='gemini-web-usage' "
        "AND token=$1 AND state='running' AND expires_at>clock_timestamp() FOR UPDATE", token)
    if row is None:
        raise LeaseExpired('Gemini usage lease expired')


async def assert_lease():
    from app.core.db_pool import get_pool
    token = current_lease.get()
    async with get_pool().acquire() as conn:
        await fence(conn, token)
    return token


@asynccontextmanager
async def operation_lease(kind, scope=None):
    """No pool connection survives a yield. Interrupted work requires reconciliation.

    B must reconcile a cancelled bootstrap by its durable token before explicitly
    releasing recovery_required. Expiry alone must never authorize a new create.
    """
    from app.core.db_pool import get_pool
    import json
    token = uuid.uuid4().hex
    async with get_pool().acquire() as conn:
        claimed = await conn.fetchval(
            "UPDATE gemini_web_usage_lease SET token=$1, kind=$2, state='running', "
            "expires_at=clock_timestamp()+($3 * interval '1 second'), updated_at=now(), scope=$4::jsonb "
            "WHERE lane='gemini-web-usage' AND (state='idle' OR "
            "($2='cleanup' AND kind IN ('collect','cleanup') AND state IN ('running','recovery_required') "
            "AND expires_at<clock_timestamp()-interval '180 seconds')) RETURNING token",
            token, kind, LEASE_SECONDS, json.dumps(scope or {}))
    if not claimed:
        logger.warning('gemini_web_usage.lease_busy_or_recovery_required')
        yield None
        return
    context = current_lease.set(token)
    uncertainty = lease_uncertain.set(False)
    completed = False
    try:
        yield token
        completed = True
    finally:
        completed = completed and not lease_uncertain.get()
        lease_uncertain.reset(uncertainty)
        current_lease.reset(context)
        # Independent short cleanup task: caller cancellation must not lose intent.
        async def release():
            async with asyncio.timeout(2), get_pool().acquire() as conn:
                state = await conn.fetchval(
                    "UPDATE gemini_web_usage_lease SET state=CASE WHEN $2 "
                    "AND expires_at>clock_timestamp() THEN 'idle' ELSE 'recovery_required' END, "
                    "updated_at=now() WHERE lane='gemini-web-usage' AND token=$1 RETURNING state", token, completed)
                if state == 'recovery_required':
                    logger.error('gemini_web_usage.lease_recovery_required kind=%s', kind)
        task = asyncio.create_task(release())
        collector.cleanup.add(task)
        def finished(t):
            collector.cleanup.discard(t)
            if not t.cancelled() and t.exception():
                logger.error('gemini_web_usage.lease_recovery_required')
        task.add_done_callback(finished)
        await asyncio.shield(task)


async def cleanup_pending():
    from app.core.db_pool import get_pool
    async with get_pool().acquire() as conn:
        return await conn.fetchval('SELECT EXISTS (SELECT 1 FROM gemini_web_usage_cleanup)')


async def publish_sample(token, tenant, owner, account, sample):
    import json
    from app.core.db_pool import get_pool
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            await fence(conn, token)
            result = await conn.execute(
                "UPDATE gemini_web_usage_bindings SET sample=$4::jsonb, "
                "sample_until=now()+($5 * interval '1 second') "
                "WHERE tenant_id=$1 AND owner_email=$2 AND account=$3",
                tenant, owner, account, json.dumps(sample),
                TTL if sample['status'] == 'available' else 30)
            return result == 'UPDATE 1'


async def defer_cleanup(request_id, reason):
    from app.core.db_pool import get_pool
    async with get_pool().acquire() as conn:
        await conn.execute(
            "UPDATE gemini_web_usage_cleanup SET state='retry_required', "
            "last_reason=$2, attempts=attempts+1, retry_after=now()+interval '60 seconds' "
            "WHERE request_id=$1", request_id, reason)
    logger.warning('gemini_web_usage.cleanup_retry_required reason=%s', reason)


async def recover_cleanup():
    async def run():
        async with operation_lease('cleanup') as token:
            if token is not None:
                await _recover_cleanup()
    await collector.bounded(run())


async def account_enabled(account):
    """B supplies the server account selector; missing wiring fails closed."""
    try:
        from app.api.llm_keys import _gemini_usage_account
        async with asyncio.timeout(5):
            row = await _gemini_usage_account()
        return bool(row and row.get('is_active') and not row.get('manual_disabled')
                    and row.get('actual_account', '').strip().casefold() == account.strip().casefold())
    except Exception:
        return False
