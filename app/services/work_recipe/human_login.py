"""Human login checkpoint; observations never return form values or page text."""

import asyncio
import logging
import os
import time
import uuid

from app.core.db_pool import get_pool
from app.services.error_book import record_candidate

logger = logging.getLogger(__name__)
SITE_KEY_PREFIX = "site:"
CREDENTIAL_KEY_PREFIX = "credential:"
BLOCK_REASONS = frozenset({"access_denied", "captcha_required", "bot_protection_suspected"})

# A passive reCAPTCHA/privacy notice is not an active challenge. Inspect visible
# headings/dialogs and actual challenge frames, never match the entire body.
PROBE = """() => {
 const visible = el => !!el && el.getClientRects().length > 0 &&
   getComputedStyle(el).visibility !== 'hidden';
 const labels = [...document.querySelectorAll('h1,h2,[role="alert"],[role="dialog"]')]
   .filter(visible).map(el => (el.innerText || '').trim().toLowerCase());
 const denied = labels.some(t => /^(access denied|접근이 거부되었습니다)[.!\\s]*$/.test(t)) ||
   /^access denied$/i.test(document.title.trim());
 const challenge = labels.some(t =>
   /^(verify you are human|complete the captcha|보안 확인이 필요합니다|캡차를 입력해 주세요)[.!\\s]*$/.test(t)) ||
   [...document.querySelectorAll('iframe')].some(el => visible(el) &&
     /recaptcha.*[?&]k=|hcaptcha/.test(el.src || '') &&
     /challenge|bframe/i.test((el.title || '') + ' ' + (el.src || '')));
 // Compare location locally, returning only an opaque number (no URL/token).
 const url = [...location.href].reduce((n, c) => Math.imul(n, 31) + c.charCodeAt(0) | 0, 0);
 return {url, requests: performance.getEntriesByType('resource').length,
   navigation: performance.timeOrigin,
   blocked: denied ? 'access_denied' : challenge ? 'captcha_required' : ''};
}"""
SUCCESS_PROBE = """args => args.every(c => c.kind === 'url_equals' ?
    location.href === c.value : (document.body?.innerText || '').includes(c.value))"""


class LoginPolicyStore:
    """Serialize reservations per tenant; site blocks expire after 24 hours.

    clear_site() explicitly releases a site early, without resetting credentials'
    daily attempt counters. A new observation refreshes the site's expiry.
    """

    def __init__(self, tenant, domain, credentials):
        self.tenant = uuid.UUID(str(tenant))
        self.domain = domain.lower().rstrip(".")
        self.credentials = sorted(set(map(str, credentials)))

    async def clear_site(self):
        async with get_pool().acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1,0))", str(self.tenant)
                )
                await conn.execute(
                    "DELETE FROM browser_login_policy WHERE tenant_id=$1 AND policy_key=$2",
                    self.tenant,
                    SITE_KEY_PREFIX + self.domain,
                )

    async def access(self, *, reserve=False, reason=""):
        if reason and reason not in BLOCK_REASONS:
            raise ValueError("login_policy_reason_invalid")
        async with get_pool().acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1,0))", str(self.tenant)
                )
                site_key = SITE_KEY_PREFIX + self.domain
                if reason:
                    await conn.execute(
                        """INSERT INTO browser_login_policy
                        (tenant_id,policy_key,day,attempts,reason,blocked_until)
                        VALUES($1,$2,CURRENT_DATE,0,$3,NOW()+INTERVAL '24 hours')
                        ON CONFLICT(tenant_id,policy_key,day) DO UPDATE
                        SET reason=$3, blocked_until=EXCLUDED.blocked_until""",
                        self.tenant,
                        site_key,
                        reason,
                    )
                    return True
                blocked = await conn.fetchval(
                    """SELECT EXISTS(SELECT 1 FROM browser_login_policy
                    WHERE tenant_id=$1 AND policy_key=$2 AND reason <> ''
                    AND blocked_until > NOW())""",
                    self.tenant,
                    site_key,
                )
                if blocked or not reserve:
                    return bool(blocked)
                if not self.credentials:
                    raise ValueError("login_credential_scope_required")
                limit = max(1, int(os.getenv("AADS_LOGIN_DAILY_SUBMIT_LIMIT", "2")))
                for credential in self.credentials:
                    count = await conn.fetchval(
                        """SELECT attempts FROM browser_login_policy
                        WHERE tenant_id=$1 AND policy_key=$2 AND day=CURRENT_DATE""",
                        self.tenant,
                        CREDENTIAL_KEY_PREFIX + credential,
                    )
                    if (count or 0) >= limit:
                        return True
                for credential in self.credentials:
                    await conn.execute(
                        """INSERT INTO browser_login_policy
                        (tenant_id,policy_key,day,attempts,reason)
                        VALUES($1,$2,CURRENT_DATE,1,'')
                        ON CONFLICT(tenant_id,policy_key,day) DO UPDATE
                        SET attempts=browser_login_policy.attempts+1""",
                        self.tenant,
                        CREDENTIAL_KEY_PREFIX + credential,
                    )
                return False


def validate_login_steps(recipe, contract):
    """Fail before any browser I/O if the approved prefix could submit."""
    submit_seq = contract["submit_step"]
    if not any(s.seq == submit_seq and s.action in {"click", "press"} for s in recipe.steps):
        raise ValueError("login_submit_step_invalid")
    allowed = {"navigate", "fill", "wait", "assert"}
    if any(s.action not in allowed for s in recipe.steps if s.seq < submit_seq):
        raise ValueError("login_prefix_must_not_submit")


class HumanLoginExecutor:
    def __init__(self, executor, browser, contract, store, wait_task, finish_task):
        self.executor, self.browser, self.contract = executor, browser, contract
        self.store, self.wait_task, self.finish_task = store, wait_task, finish_task
        self.done = False
        self.already_authenticated = False
        self.stage = "initialization"
        self.task = None

    @staticmethod
    def blocked(reason):
        return {
            "ok": False,
            "status": "blocked",
            "error": reason,
            "route": "human_gateway",
            "recovery": {"route": "human_gateway", "reason": reason, "auto_retry": False},
        }

    async def operation(self, stage, call, *args, **kwargs):
        self.stage = stage
        return await call(*args, **kwargs)

    async def __call__(self, payload):
        try:
            result = await self.run(payload)
            if self.task and result.get("status") == "blocked":
                result["recovery"]["auth_task_id"] = self.task["id"]
            return result
        except Exception as exc:
            # Only stage and exception class leave this boundary. Browser and DB
            # exception messages may contain credentials, SQL values or cookies.
            code = (
                "login_checkpoint_unavailable"
                if self.stage == "browser_observation"
                else "login_checkpoint_dependency_failed"
            )
            diagnostic = f"{code}: stage={self.stage} exception={type(exc).__name__}"
            logger.error("%s", diagnostic)
            await record_candidate(diagnostic, source="work_recipe.human_login")
            result = self.blocked(code)
            result["recovery"]["failure_stage"] = self.stage
            result["recovery"]["exception_type"] = type(exc).__name__
            return result

    async def observe(self, page):
        return await self.operation("browser_observation", page.evaluate, PROBE)

    async def success(self, page):
        return (
            await self.operation(
                "browser_observation", page.evaluate, SUCCESS_PROBE, self.contract["succeeds_when"]
            )
            is True
        )

    async def policy(self, **kwargs):
        return await self.operation("policy_store", self.store.access, **kwargs)

    async def wait(self, payload, reason):
        self.task = await self.operation("wait_task", self.wait_task, payload, reason)
        return self.task

    async def finish(self, task, ok, reason=""):
        return await self.operation("finish_task", self.finish_task, task, ok, reason)

    async def stop(self, payload, reason, task=None):
        await self.policy(reason=reason)
        if task:
            await self.finish(task, False, reason)
        else:
            await self.wait(payload, reason)
        return self.blocked(reason)

    async def run(self, payload):
        submit = self.contract["submit_step"]
        phase, seq = payload.get("phase"), payload.get("seq", submit + 1)
        page = self.browser.login_page
        if phase == "step" and seq <= submit and page is not None and not self.done:
            self.already_authenticated = await self.success(page)
        if phase == "step" and seq <= submit and (self.already_authenticated or self.done):
            return {"ok": True, "output": {"already_authenticated": True}}
        if phase != "step" or seq != submit or self.done:
            result = await self.operation("step_executor", self.executor, payload)
            if phase == "step" and seq < submit and self.browser.login_page is not None:
                observed = await self.observe(self.browser.login_page)
                if observed["blocked"]:
                    return await self.stop(payload, observed["blocked"])
            return result
        before = await self.observe(page)
        if before["blocked"]:
            return await self.stop(payload, before["blocked"])
        human = self.contract.get("human_submit_required", False) or await self.policy()
        if not human and await self.policy(reserve=True):
            await self.wait(payload, "daily_login_submit_limit")
            return self.blocked("daily_login_submit_limit")
        task = None
        if human:
            task = await self.wait(payload, "human_submit_required")
            timeout = self.contract.get("human_submit_timeout_seconds", 600)
        else:
            # Never re-submit, even if the browser reports an ambiguous failure.
            await self.operation("step_executor", self.executor, payload)
            timeout = self.contract.get("submit_observation_seconds", 5)
        deadline = time.monotonic() + timeout
        while True:
            observed = await self.observe(page)
            if observed["blocked"]:
                return await self.stop(payload, observed["blocked"], task)
            if await self.success(page):
                self.done = True
                if task:
                    await self.finish(task, True)
                return {"ok": True, "output": {"human_submit": human}}
            if time.monotonic() >= deadline:
                if human:
                    await self.finish(task, False, "human_login_timeout")
                    return self.blocked("human_login_timeout")
                if all(observed[k] == before[k] for k in ("url", "requests", "navigation")):
                    return await self.stop(payload, "bot_protection_suspected")
                await self.wait(payload, "login_verification_failed")
                return self.blocked("login_verification_failed")
            await asyncio.sleep(min(2, max(0, deadline - time.monotonic())))
