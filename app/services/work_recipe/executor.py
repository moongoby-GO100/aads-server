"""Work Recipe 단계를 Browser Bridge에서 실행하는 어댑터."""
from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

from app.browser_bridge.aads_adapter import acquire_browser_context
from app.browser_bridge.service import BrowserBridgeError, get_browser_bridge_service
from app.services.screenshot_store import save_png
from app.services.work_recipe.audit import mask_secrets
from app.services.work_recipe.guard import (
    assert_not_page_derived,
    classify_step,
    requires_approval,
)
from app.services.work_recipe.schema import ALLOWED_ACTIONS

# Smart Browser is the normal execution lane.  A local PC lane is deliberately
# opt-in: it is reserved for native security software/certificates or an
# explicitly observed authentication challenge.  This keeps ordinary AADS
# login verification and read-only recipes off a user's desktop session.
_NATIVE_AUTH_STATES = frozenset({
    "certificate_password_required", "identity_check_required",
})
_HUMAN_GATEWAY_STATES = frozenset({
    "captcha_required", "otp_required", "login_required",
})
# 서버(데이터센터) IP 가 봇/WAF 에 막히는 사이트는 "로컬 보안 프로그램이 필요한
# 사이트" 와 전혀 다른 사유다. 두 가지를 native_auth_required 하나로 뭉뚱그리면
# 그 플래그는 더 이상 보안 판정에 쓸 수 없게 된다. 그래서 별도 레인 사유
# server_ip_blocked 를 둔다 (2026-09-21 CEO 지시: 서버 브라우저로 접근이 불가한
# 사이트는 사람에게 넘기지 말고 PC 레인으로 진행한다).
# 목록은 browser_task_gateway.ACCESS_MARKERS["bot_or_waf_blocked"] 와 같은 성격이되,
# 앱 권한 거부와 구분되지 않는 "forbidden"/"permission denied" 는 일부러 뺐다.
_SERVER_BLOCKED_MARKERS = (
    "access denied",
    "cloudflare",
    "cf-chl",
    "checking your browser",
    "verify you are human",
    "unusual traffic",
    "bot detection",
    "automated traffic",
    "자동화된 접근",
    "비정상적인 접근",
    "접근이 제한",
    "차단되었습니다",
)
# 차단 페이지는 짧다. 본문이 이보다 길면 같은 문구가 있어도 정상 페이지로 본다.
_BLOCK_PAGE_TEXT_LIMIT = 2_000


def smart_browser_route(context: Mapping[str, Any] | None = None) -> dict[str, str]:
    """Choose an explicit execution lane without silently escalating to PC.

    Callers may assert ``native_auth_required`` only for a known local-security
    requirement.  A site that a datacenter IP cannot reach at all declares
    ``server_access_blocked`` instead — a separate, honest reason that keeps the
    native-auth flag meaningful.  Authentication challenges themselves are
    handled by the Human Gateway after the Browser Agent observes them.
    """
    context = context or {}
    smart_context = context.get("smart_browser")
    if isinstance(smart_context, Mapping):
        context = {**context, **smart_context}
    if bool(context.get("native_auth_required") or context.get("requires_local_security_programs")):
        return {"runtime": "pc_agent", "reason": "native_auth_required"}
    if bool(context.get("server_access_blocked") or context.get("requires_residential_ip")):
        return {"runtime": "pc_agent", "reason": "server_ip_blocked"}
    return {"runtime": "browser_agent", "reason": "read_or_login_verification"}


def is_server_ip_blocked(*, url: str = "", text: str = "", error: str = "") -> bool:
    """서버 브라우저가 봇/WAF 차단으로 페이지 자체를 못 연 상황인가."""
    haystack = f"{url} {text} {error}".lower()
    return any(marker in haystack for marker in _SERVER_BLOCKED_MARKERS)


def smart_browser_recovery(*, url: str = "", text: str = "", error: str = "") -> dict[str, str]:
    """Return a safe, chat-displayable recovery handoff for a failed step."""
    from app.services.auth_challenge_orchestrator import classify_portal_state

    decision = classify_portal_state(url=url, text=f"{text}\n{error}")
    if decision.state in _NATIVE_AUTH_STATES:
        return {"route": "pc_agent", "reason": decision.reason_code, "resume": "same_work_session"}
    # 봇/WAF 차단은 권한 문제가 아니다. 사람을 부르기 전에 PC 레인으로 넘긴다.
    if is_server_ip_blocked(url=url, text=text, error=error):
        return {"route": "pc_agent", "reason": "server_ip_blocked", "resume": "same_work_session"}
    if decision.state in _HUMAN_GATEWAY_STATES:
        return {"route": "human_gateway", "reason": decision.reason_code, "resume": "same_work_session"}
    lowered = f"{text} {error}".lower()
    # "access denied" 는 위 봇/WAF 판정이 먼저 가져간다. 여기 남는 것은 앱 권한 거부다.
    if any(marker in lowered for marker in ("403", "forbidden", "permission denied", "권한")):
        return {"route": "human_gateway", "reason": "permission_insufficient", "resume": "after_access_grant"}
    if any(marker in lowered for marker in ("timeout", "net::err", "network", "dns", "connection refused")):
        return {"route": "browser_agent", "reason": "network_failure", "resume": "retry_same_step"}
    if decision.state == "session_expired":
        return {"route": "human_gateway", "reason": decision.reason_code, "resume": "login_then_retry_same_step"}
    return {"route": "browser_agent", "reason": "step_failed", "resume": "retry_same_step"}


class BrowserRecipeExecutor:
    """player의 step payload를 Playwright 호환 Browser Bridge로 옮긴다."""

    def __init__(
        self,
        *,
        browser_session_id: str | None = None,
        browser_work_key: str | None = None,
        page_texts: list[str] | None = None,
    ) -> None:
        self.browser_session_id = browser_session_id
        self.browser_work_key = browser_work_key
        self.page_texts = list(page_texts or [])
        self._context: Any = None
        self._page: Any = None
        # 봇/WAF 차단을 만나 PC 레인으로 한 번 넘어가면 같은 recipe 의 남은
        # 단계도 그 레인에서 이어간다. 레인이 단계마다 흔들리면 로그인 세션이 끊긴다.
        self._pc_lane_forced = False
        # 서비스 초기화/설정 오류를 첫 단계보다 앞에서 드러낸다.
        self._service = get_browser_bridge_service()

    async def __call__(self, payload: dict[str, Any]) -> dict[str, Any]:
        action = str(payload.get("action") or "")
        if action not in ALLOWED_ACTIONS:
            return {"ok": False, "error": f"지원하지 않는 브라우저 action: {action}"}

        result = await self._attempt(action, payload)
        # 봇 차단은 예외로만 오지 않는다. 쿠팡처럼 "Access Denied" 한 장을
        # 200 으로 돌려주면 단계는 성공하고 증거만 차단 화면이 된다(2026-09-21 실측).
        blocked = (result.get("recovery") or {}).get("reason") == "server_ip_blocked" or (
            bool(result.get("ok")) and self._evidence_shows_block(result)
        )
        if not blocked:
            return result
        recovery = result.get("recovery") or {}
        # 서버 IP 가 막힌 것은 사람이 풀어 줄 수 있는 문제가 아니다.
        # PC 레인이 준비돼 있으면 같은 단계를 그 레인에서 한 번 더 실행한다.
        resume = "pc_lane_also_failed" if self._pc_lane_forced else "needs_browser_work_key"
        if not self.browser_work_key or self._pc_lane_forced:
            if result.get("ok"):
                result["server_access"] = {"blocked": True, "lane": "browser_agent", "resume": resume}
            else:
                result["recovery"] = {**recovery, "reason": "server_ip_blocked", "resume": resume}
            return result
        self._pc_lane_forced = True
        self._context = None
        self._page = None
        retried = await self._attempt(action, payload)
        retried["lane_failover"] = {
            "from": "browser_agent", "to": "pc_agent", "reason": "server_ip_blocked",
        }
        if retried.get("ok") and self._evidence_shows_block(retried):
            retried["server_access"] = {
                "blocked": True, "lane": "pc_agent", "resume": "pc_lane_also_failed",
            }
        return retried

    @staticmethod
    def _evidence_shows_block(result: Mapping[str, Any]) -> bool:
        """증거 화면이 차단 페이지인가. 본문이 길면 차단 문구가 있어도 본문으로 본다."""
        evidence = result.get("evidence")
        if not isinstance(evidence, Mapping):
            return False
        dom = evidence.get("dom")
        text = str(dom.get("text") or "") if isinstance(dom, Mapping) else ""
        if len(text) > _BLOCK_PAGE_TEXT_LIMIT:
            return False
        return is_server_ip_blocked(url=str(evidence.get("url") or ""), text=text)

    async def _attempt(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            level = classify_step(payload, domain=self._domain(payload))
            # 승인 집행은 orchestrator의 GuardedRunRecorder가 담당한다. 여기서도
            # 반드시 같은 판정을 수행해 우회 실행 경로가 정책을 건너뛰지 않게 한다.
            requires_approval(level)
            self._assert_trusted_arguments(payload)
            page = await self._get_page(payload)
            output = await self._execute(page, action, payload)
            evidence = await self._collect_evidence(page)
            return {
                "ok": True,
                "output": output,
                "risk": level.value,
                "llm_calls": 0,
                "narration": self._narration(action, payload),
                "route": self._route(payload)["runtime"],
                "evidence": evidence,
            }
        except BrowserBridgeError as exc:
            return {
                "ok": False,
                "error": mask_secrets(f"Browser Bridge 사용 불가: {exc}"),
                "recovery": smart_browser_recovery(error=str(exc)),
            }
        except Exception as exc:  # noqa: BLE001 - player 계약상 단계 실패를 결과로 변환
            url = str(getattr(self._page, "url", "") or payload.get("url") or "")
            return {
                "ok": False,
                "error": mask_secrets(f"{type(exc).__name__}: {exc}"),
                "recovery": smart_browser_recovery(url=url, error=str(exc)),
            }

    @staticmethod
    def _domain(payload: Mapping[str, Any]) -> str:
        context = payload.get("context")
        return str(context.get("domain") or "") if isinstance(context, Mapping) else ""

    def _assert_trusted_arguments(self, payload: Mapping[str, Any]) -> None:
        arguments = {
            key: payload.get(key)
            for key in ("url", "selector", "value", "endpoint")
            if payload.get(key) is not None
        }
        context = payload.get("context")
        page_texts = list(self.page_texts)
        if isinstance(context, Mapping):
            supplied = context.get("page_texts")
            if isinstance(supplied, str):
                page_texts.append(supplied)
            elif isinstance(supplied, (list, tuple)):
                page_texts.extend(str(item) for item in supplied)
        assert_not_page_derived(arguments, page_texts, field="step")

    @staticmethod
    def _context_payload(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        context = payload.get("context")
        return context if isinstance(context, Mapping) else {}

    def _route(self, payload: Mapping[str, Any]) -> dict[str, str]:
        """차단으로 PC 레인에 넘어간 뒤에는 남은 단계도 같은 레인에서 이어간다."""
        if self._pc_lane_forced:
            return {"runtime": "pc_agent", "reason": "server_ip_blocked"}
        return smart_browser_route(self._context_payload(payload))

    async def _get_page(self, payload: Mapping[str, Any]) -> Any:
        if self._page is not None:
            return self._page
        route = self._route(payload)
        self._context, error = await acquire_browser_context(
            browser_session_id=self.browser_session_id,
            # A work key opens a local PC session.  Do not use it unless the
            # recipe explicitly declares a native-auth requirement.
            browser_work_key=self.browser_work_key if route["runtime"] == "pc_agent" else None,
            url=str(payload.get("url") or "about:blank"),
            prefer_headless=route["runtime"] == "browser_agent",
        )
        if self._context is None:
            raise BrowserBridgeError(error or "브라우저 컨텍스트를 확보하지 못했습니다")
        pages = list(getattr(self._context, "pages", []) or [])
        self._page = pages[0] if pages else await self._context.new_page()
        return self._page

    async def _collect_evidence(self, page: Any) -> dict[str, Any]:
        """Capture safe structural evidence without persisting page secrets."""
        evidence: dict[str, Any] = {"url": mask_secrets(getattr(page, "url", "") or "")}
        try:
            body = page.locator("body")
            dom = await body.inner_text(timeout=5_000)
            masked = mask_secrets(dom)[:4_000]
            evidence["dom"] = {"text": masked, "sha256": hashlib.sha256(masked.encode()).hexdigest()}
            if hasattr(body, "aria_snapshot"):
                aria = mask_secrets(await body.aria_snapshot())[:4_000]
                evidence["aria"] = {"text": aria, "sha256": hashlib.sha256(aria.encode()).hexdigest()}
        except Exception as exc:  # Evidence must not turn a completed read into a failed task.
            evidence["dom"] = {"status": "unavailable", "reason": type(exc).__name__}
        try:
            screenshot = await page.screenshot(timeout=15_000)
            if isinstance(screenshot, bytes):
                evidence["screenshot"] = {
                    "status": "captured", "sha256": hashlib.sha256(screenshot).hexdigest(), "bytes": len(screenshot),
                }
                # 해시만 남기면 "증거 저장" 이라고 보고하면서 정작 아무도 그
                # 화면을 열어 볼 수 없다. 열리는 URL 까지 같이 남긴다.
                url = await save_png(screenshot, prefix="recipe")
                if url:
                    evidence["screenshot"]["url"] = url
            elif isinstance(screenshot, str):
                evidence["screenshot"] = {"status": "captured", "reference": mask_secrets(screenshot)}
        except Exception as exc:
            evidence["screenshot"] = {"status": "unavailable", "reason": type(exc).__name__}
        return evidence

    @staticmethod
    def _narration(action: str, payload: Mapping[str, Any]) -> str:
        description = str(payload.get("description") or "").strip()
        return description or f"Smart Browser가 {action} 단계를 완료했습니다."

    async def _execute(self, page: Any, action: str, step: Mapping[str, Any]) -> Any:
        selector = str(step.get("selector") or "")
        timeout = float(step.get("timeout") or 30.0)
        timeout_ms = int(timeout * 1000)

        if action == "navigate":
            await page.goto(str(step.get("url") or ""), timeout=timeout_ms)
            return {"url": str(getattr(page, "url", step.get("url") or ""))}
        if action == "click":
            await page.click(selector, timeout=timeout_ms)
            return None
        if action == "fill":
            await page.fill(selector, str(step.get("value") or ""), timeout=timeout_ms)
            return None
        if action == "select":
            await page.select_option(selector, step.get("value"), timeout=timeout_ms)
            return None
        if action == "press":
            key = str(step.get("value") or step.get("key") or "Enter")
            if hasattr(page, "press"):
                await page.press(selector, key, timeout=timeout_ms)
            else:
                await page.press_key(key, selector=selector)
            return None
        if action == "upload":
            await page.set_input_files(selector, step.get("value"), timeout=timeout_ms)
            return None
        if action == "download":
            if hasattr(page, "download"):
                return await page.download(
                    selector,
                    download_dir=str(step.get("value") or ""),
                    timeout_seconds=timeout,
                )
            async with page.expect_download(timeout=timeout_ms) as pending:
                await page.click(selector, timeout=timeout_ms)
            download = await pending.value
            return {"suggested_filename": download.suggested_filename}
        if action == "snapshot":
            target = page.locator(selector) if selector else page.locator("body")
            if hasattr(target, "aria_snapshot"):
                return await target.aria_snapshot()
            return await target.inner_text(timeout=timeout_ms)
        if action == "api_call":
            endpoint = str(step.get("endpoint") or step.get("url") or "")
            options = step.get("value") if isinstance(step.get("value"), Mapping) else {}
            return await page.evaluate(
                "async ({endpoint, options}) => { const response = await fetch(endpoint, options); "
                "const text = await response.text(); return {status: response.status, ok: response.ok, text}; }",
                {"endpoint": endpoint, "options": dict(options)},
                timeout=timeout_ms,
            )
        raise ValueError(f"지원하지 않는 브라우저 action: {action}")


async def execute_step(payload: dict[str, Any]) -> dict[str, Any]:
    """단일 단계용 편의 함수."""
    return await BrowserRecipeExecutor()(payload)
