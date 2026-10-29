"""Work Recipe 단계를 Browser Bridge에서 실행하는 어댑터."""
from __future__ import annotations

import hashlib
import inspect
import logging
import re
from collections.abc import Callable, Iterable, Mapping
from typing import Any
from urllib.parse import urlsplit

from app.browser_bridge.aads_adapter import acquire_browser_context
from app.browser_bridge.service import BrowserBridgeError, get_browser_bridge_service
from app.services.screenshot_store import save_png
from app.services.work_recipe.audit import mask_secrets
from app.services.work_recipe.guard import (
    SELF_REPORTED_CLAIM_KEYS,
    ApprovalMissing,
    AutoReviewBlocked,
    AutoReviewDecision,
    AutoReviewVerdict,
    RiskLevel,
    assert_not_page_derived,
    autoreview_step,
    classify_step,
    requires_approval,
)
from app.services.work_recipe.schema import (
    ALLOWED_ACTIONS,
    OVERLAY_DEFAULT_MAX_DISMISS,
    OVERLAY_SENSITIVE_MARKERS,
    overlay_host_matches,
)
from app.services.work_recipe.store import normalize_domain
from app.services.work_recipe.shinhan_corporate_manual import (
    FinancialManualBlocked,
    assert_unregistered_navigation,
)

logger = logging.getLogger(__name__)

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


_OVERLAY_PROBE_SCRIPT = """(args) => {
    const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
    const root = document.querySelector(args.root);
    const rootVisible = visible(root);
    const close = rootVisible ? root.querySelector(args.close) : null;
    return {
        url: window.location.href,
        rootVisible,
        closeVisible: visible(close),
        text: rootVisible ? String(root.innerText ?? root.textContent ?? '').slice(0, 600) : '',
    };
}"""


class SecretFillBlocked(RuntimeError):
    """비밀 입력을 화면에 넣기 직전 검사에서 막혔다. 메시지는 코드뿐이다(값을 싣지 않는다)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


_FRAME_SELECTOR = re.compile(r"enter-frame|internal:|>>\s*(?:css=)?i?frame", re.IGNORECASE)
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_SECRET_WITHHELD = {"status": "withheld", "reason": "secret_on_screen"}


class OverlayNotDismissible(RuntimeError):
    """허용 목록의 모달이지만 안전하게 닫지 못했다 — 사람에게 넘긴다."""

    def __init__(self, reason: str, dismissed: list[dict[str, Any]]):
        super().__init__(reason)
        self.reason = reason
        self.dismissed = dismissed


def smart_browser_route(context: Mapping[str, Any] | None = None) -> dict[str, str]:
    """Choose an explicit execution lane without silently escalating to PC.

    Callers may assert ``native_auth_required`` only for a known local-security
    requirement.  A site that a datacenter IP cannot reach at all declares
    ``server_access_blocked`` instead — a separate, honest reason that keeps the
    native-auth flag meaningful.  Authentication challenges themselves are
    handled by the Human Gateway after the Browser Agent observes them.
    """
    context = context or {}
    if "login_contract" in context:
        from app.services.work_recipe.login_contract import contract_route
        return contract_route(context["login_contract"])
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
        instruction: Any = None,
        custom_rules: list[Mapping[str, Any]] | None = None,
        pre_approvals: list[Any] | None = None,
        approval_verifier: Callable[[Mapping[str, Any], RiskLevel], Any] | None = None,
        secret_fill_steps: Iterable[int] | None = None,
        tenant_id: Any = None,
    ) -> None:
        # 공용 팝업 목록(browser_site_overlays) 조회 범위. 서버 코드만 생성자로 넣는다.
        self._tenant_id = tenant_id
        # 아래 네 값은 서버 코드가 생성자로만 넣는다. payload 로 들어온 값은 신뢰하지 않는다.
        self.instruction = instruction
        self.custom_rules = list(custom_rules or [])
        self.pre_approvals = list(pre_approvals or [])
        self._approval_verifier = approval_verifier
        # Vault 참조로 해석된 비밀을 입력하는 단계 seq. 서버 코드만 생성자로 넣는다.
        self._secret_fill_steps = frozenset(int(seq) for seq in (secret_fill_steps or ()))
        # 비밀이 입력된 화면의 실제 URL. 같은 화면에 머무는 동안 증거(DOM/aria/스크린샷)를 남기지 않는다.
        self._secret_screen_url = ""
        self.browser_session_id = browser_session_id
        self.browser_work_key = browser_work_key
        self.page_texts = list(page_texts or [])
        self._context: Any = None
        self._page: Any = None
        # 봇/WAF 차단을 만나 PC 레인으로 한 번 넘어가면 같은 recipe 의 남은
        # 단계도 그 레인에서 이어간다. 레인이 단계마다 흔들리면 로그인 세션이 끊긴다.
        self._pc_lane_forced = False
        # 로그인 단계를 이미 지난(또는 context 가 인증 필요를 선언한) 실행에서만 "로그인 회귀" 를 본다.
        # 공개 페이지의 "로그인" 링크 때문에 조회 레시피가 멈추면 안 된다.
        self._authenticated_run = False
        # 서비스 초기화/설정 오류를 첫 단계보다 앞에서 드러낸다.
        self._service = get_browser_bridge_service()

    @property
    def login_page(self):
        """Retained live page for read-only login checkpoint observations."""
        return self._page

    def login_lane(self, payload):
        return "pc" if self._route(payload)["runtime"] == "pc_agent" else "server"

    @property
    def login_pc_label(self):
        return getattr(self._page, "_agent_id", "") or "연결된 PC"

    async def __call__(self, payload: dict[str, Any]) -> dict[str, Any]:
        action = str(payload.get("action") or "")
        if action not in ALLOWED_ACTIONS:
            return {"ok": False, "error": f"지원하지 않는 브라우저 action: {action}"}
        try:
            # 치환이 끝난 최종 URL 을 단계 실행 직전에 검사한다(금융 미등록 레시피의 신한 접근 차단).
            assert_unregistered_navigation(payload)
        except FinancialManualBlocked as exc:
            return {"ok": False, "status": "blocked", "route": "browser_agent", "llm_calls": 0,
                    "error": f"financial_manual_blocked:{exc.code}"}

        route = self._route(payload)
        if route["runtime"] == "human_gateway":
            return {"ok": False, "status": "waiting_for_user", "error": route["reason"],
                    "recovery": {"route": "human_gateway", "reason": route["reason"], "resume": "same_work_session"}}
        result = await self._attempt(action, payload)
        # 봇 차단은 예외로만 오지 않는다. 쿠팡처럼 "Access Denied" 한 장을
        # 200 으로 돌려주면 단계는 성공하고 증거만 차단 화면이 된다(2026-09-21 실측).
        blocked = (result.get("recovery") or {}).get("reason") == "server_ip_blocked" or (
            bool(result.get("ok")) and self._evidence_shows_block(result)
        )
        if not blocked:
            return result
        recovery = result.get("recovery") or {}
        if "login_contract" in self._context_payload(payload):
            return {**result, "ok": False, "status": "waiting_for_user",
                    "access": "server_restricted_pc_unverified",
                    "recovery": {"route": "human_gateway", "reason": "pc_verification_required", "resume": "same_work_session"}}
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
    def _step_targets_login(payload: Mapping[str, Any]) -> bool:
        """이 단계 자체가 로그인 화면을 열거나 채우는 단계인가 (그러면 로그인 화면은 정상이다)."""
        from app.services.auth_challenge_orchestrator import classify_portal_state

        context = payload.get("context")
        recipe_name = str(context.get("recipe_name") or "") if isinstance(context, Mapping) else ""
        intent = " ".join(
            str(payload.get(key) or "") for key in ("url", "selector", "description")
        ) + " " + recipe_name
        if re.search(r"login|signin|sign-in|logon|auth|로그인|인증", intent, re.IGNORECASE):
            return True
        return classify_portal_state(url=str(payload.get("url") or ""), text="").state in {
            "login_required", "session_expired",
        }

    def _auth_regression(self, payload: Mapping[str, Any], evidence: Mapping[str, Any]) -> str:
        """로그인 단계가 아닌데 화면이 로그인/세션 만료로 돌아갔으면 사유 코드, 아니면 빈 문자열."""
        from app.services.auth_challenge_orchestrator import classify_portal_state

        if str(payload.get("action") or "") == "dismiss_overlays":
            return ""
        if self._step_targets_login(payload):
            self._authenticated_run = True
            return ""
        context = self._context_payload(payload)
        if not (self._authenticated_run or context.get("requires_auth") is True):
            return ""
        dom = evidence.get("dom")
        text = str(dom.get("text") or "") if isinstance(dom, Mapping) else ""
        if len(text) > _BLOCK_PAGE_TEXT_LIMIT:
            return ""
        decision = classify_portal_state(url=str(evidence.get("url") or ""), text=text)
        return decision.reason_code if decision.state in {"login_required", "session_expired"} else ""

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
        level = RiskLevel.READ
        try:
            level = classify_step(payload, domain=self._domain(payload))
            review = await self._gate(payload, level)
            self._assert_trusted_arguments(payload)
            page = await self._get_page(payload)
            contract = self._context_payload(payload).get("login_contract")
            if isinstance(contract, Mapping) and (contract.get("challenge") or {}).get("active"):
                return {"ok": False, "status": "waiting_for_user", "route": "human_gateway",
                        "error": "active_auth_challenge",
                        "recovery": {"route": "human_gateway", "reason": "active_auth_challenge", "resume": "same_work_session"}}
            if isinstance(contract, Mapping) and contract.get("oauth"):
                from app.services.work_recipe.login_contract import assert_oauth_scope
                opener = await page.evaluate("(() => { try { return window.opener ? window.opener.location.origin + window.opener.location.pathname : location.origin + location.pathname; } catch (_) { return ''; } })()")
                assert_oauth_scope(contract, current_url=await self._live_href(page),
                                   opener_url=opener, target_url=payload.get("url") or contract["oauth"]["target"])
            output = await self._execute(page, action, payload)
            if isinstance(contract, Mapping):
                challenge_visible = await page.evaluate("""() => {
                    const selectors = ['input[autocomplete="one-time-code"]', '[data-auth-challenge="active"]'];
                    return selectors.some(s => [...document.querySelectorAll(s)].some(el =>
                        !el.disabled && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)));
                }""")
                if challenge_visible is True:
                    return {"ok": False, "status": "waiting_for_user", "route": "human_gateway",
                            "error": "active_auth_challenge", "evidence": {},
                            "recovery": {"route": "human_gateway", "reason": "active_auth_challenge", "resume": "same_work_session", "step_completed": True}}
            evidence = await self._evidence_for(page)
            regression = self._auth_regression(payload, evidence)
            if regression:
                # 자동 재로그인·재시도·발송 재실행을 하지 않는다. 사람이 Vault 경로로 복구한다.
                return {
                    "ok": False,
                    "status": "auth_required",
                    "error": "auth_required: 로그인 화면으로 되돌아갔습니다",
                    "risk": level.value,
                    "llm_calls": 0,
                    "route": "human_gateway",
                    "evidence": evidence,
                    "recovery": {
                        "route": "human_gateway",
                        "reason": "auth_required",
                        "signal": regression,
                        "resume": "login_then_retry_same_step",
                        "recovery_path": "vault_credential_scope",
                        "auto_relogin": False,
                        "auto_retry": False,
                    },
                }
            return {
                "ok": True,
                "output": output,
                "risk": level.value,
                "llm_calls": 0,
                "narration": self._narration(action, payload),
                "route": self._route(payload)["runtime"],
                "evidence": evidence,
                "autoreview": review.to_dict(),
            }
        except (ApprovalMissing, AutoReviewBlocked) as exc:
            reason = "handoff_required" if isinstance(exc, AutoReviewBlocked) else "approval_required"
            review = exc.decision
            return {
                "ok": False,
                "status": "blocked",
                "error": mask_secrets(str(exc)),
                "risk": level.value,
                "llm_calls": 0,
                "route": "human_gateway",
                "evidence": {},
                "recovery": {
                    "route": "human_gateway",
                    "reason": reason,
                    "resume": "after_approval" if reason == "approval_required" else "human_completes_step",
                },
                **({"autoreview": review.to_dict()} if review else {}),
            }
        except SecretFillBlocked as exc:
            return {
                "ok": False,
                "status": "blocked",
                "error": f"secret_fill_blocked:{exc.code}",
                "risk": level.value,
                "llm_calls": 0,
                "route": "human_gateway",
                "evidence": {},
                "recovery": {
                    "route": "human_gateway",
                    "reason": exc.code,
                    "resume": "after_recipe_or_session_fixed",
                    "auto_retry": False,
                },
            }
        except OverlayNotDismissible as exc:
            return {
                "ok": False,
                "status": "blocked",
                "error": f"overlay_not_dismissible:{exc.reason}",
                "risk": level.value,
                "llm_calls": 0,
                "route": "human_gateway",
                "evidence": {},
                "output": {"dismissed": exc.dismissed},
                "recovery": {
                    "route": "human_gateway",
                    "reason": f"overlay_{exc.reason}",
                    "resume": "human_completes_step",
                },
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

    async def _gate(self, payload: Mapping[str, Any], level: RiskLevel) -> AutoReviewDecision:
        """실행 직전 게이트. 통과하면 심사 결정을, 막으면 GuardError 계열 예외를 던진다."""
        context = self._context_payload(payload)
        rules = list(self.custom_rules)
        supplied_rules = context.get("custom_rules")
        if isinstance(supplied_rules, (list, tuple)):
            rules.extend(supplied_rules)  # payload 규칙은 조이는 효과만 유효하다(guard 가 거른다)
        claims = {
            key: value
            for source in (context, payload)
            for key, value in source.items()
            if key in SELF_REPORTED_CLAIM_KEYS
        }
        review = autoreview_step(
            payload,
            instruction=self.instruction if self.instruction is not None else context.get("instruction"),
            custom_rules=rules,
            claims=claims,
            pre_approvals=self.pre_approvals,
            domain=self._domain(payload),
        )

        # 1) 정적 승인 요구는 모드와 무관하게 항상 집행한다 — 판정값을 버리던 결함의 교정.
        if (
            requires_approval(level)
            and review.verdict is not AutoReviewVerdict.PRE_APPROVED
            and not await self._approval_verified(payload, level)
        ):
            raise ApprovalMissing(level.value, "static_level", review)

        # 2) 동적 심사는 enforce 에서만 막고, observe 는 사유만 남긴다.
        if review.needs_human:
            if not review.enforced:
                logger.warning(
                    "work_recipe autoreview(observe) would block: verdict=%s level=%s reasons=%s",
                    review.verdict.value, review.level.value, ",".join(review.reasons),
                )
            elif review.verdict is AutoReviewVerdict.HANDOFF:
                raise AutoReviewBlocked(review)
            elif not await self._approval_verified(payload, review.level):
                raise ApprovalMissing(review.level.value, ",".join(review.reasons), review)
        return review

    async def _approval_verified(self, payload: Mapping[str, Any], required: RiskLevel) -> bool:
        """서버가 확인한 승인만 인정한다. payload 의 approved=true 같은 플래그는 보지 않는다.

        확인 수단은 둘이다. 생성자로 주입한 ``approval_verifier`` (True 를 그대로
        돌려줘야 통과), 없으면 context 의 ``approval_id``+``run_id`` 를 DB 승인 행과
        대조한다(승인됨·같은 run·같은 step_seq·같은 action·요구 등급 이상). 어떤
        오류든 미승인으로 본다.
        """
        try:
            if self._approval_verifier is not None:
                outcome = self._approval_verifier(payload, required)
                if inspect.isawaitable(outcome):
                    outcome = await outcome
                return outcome is True
            context = self._context_payload(payload)
            approval_id, run_id = context.get("approval_id"), context.get("run_id")
            if not approval_id or not run_id:
                return False
            from app.services.work_recipe import approval as approval_module

            row = await approval_module.get_approval(approval_id)
            if not row or str(row.get("status") or "") != approval_module.STATUS_APPROVED:
                return False
            if str(row.get("run_id") or "") != str(run_id):
                return False
            if int(row.get("step_seq")) != int(payload.get("seq")):
                return False
            if row.get("action") and str(row["action"]) != str(payload.get("action") or ""):
                return False
            return RiskLevel.from_any(row.get("risk")).rank >= required.rank
        except Exception:  # noqa: BLE001 - 확인 실패는 곧 미승인
            return False

    @staticmethod
    def _domain(payload: Mapping[str, Any]) -> str:
        context = payload.get("context")
        return str(context.get("domain") or "") if isinstance(context, Mapping) else ""

    def _assert_trusted_arguments(self, payload: Mapping[str, Any]) -> None:
        # Vault 에서 온 비밀 값은 페이지 유래 검사에서 뺀다 — 위반 메시지가 값을 발췌해 싣기 때문이다.
        keys = ("url", "selector", "endpoint") if self._is_secret_fill(payload) else (
            "url", "selector", "value", "endpoint")
        arguments = {key: payload.get(key) for key in keys if payload.get(key) is not None}
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
            browser_lane="pc" if route["runtime"] == "pc_agent" else "server",
        )
        if self._context is None:
            raise BrowserBridgeError(error or "브라우저 컨텍스트를 확보하지 못했습니다")
        pages = list(getattr(self._context, "pages", []) or [])
        self._page = pages[0] if pages else await self._context.new_page()
        return self._page

    def _is_secret_fill(self, step: Mapping[str, Any]) -> bool:
        if str(step.get("action") or "") != "fill" or not self._secret_fill_steps:
            return False
        try:
            return int(step.get("seq")) in self._secret_fill_steps
        except (TypeError, ValueError):
            return False

    @staticmethod
    async def _live_href(page: Any) -> str:
        """facade 가 캐시한 page.url 이 아니라 브라우저가 지금 보는 실제 주소."""
        try:
            return str(await page.evaluate("window.location.href") or "")
        except Exception:  # noqa: BLE001 - 확인 못 하면 빈 값 = 불허
            return ""

    @staticmethod
    def _href_allowed(href: str, domain: str) -> bool:
        try:
            parts = urlsplit(href)
            host = (parts.hostname or "").lower().removeprefix("www.")
            scheme = parts.scheme.lower()
            has_userinfo = bool(parts.username or parts.password)
        except ValueError:
            return False
        wanted = normalize_domain(domain)
        if not host or not wanted or host != wanted or has_userinfo:
            return False
        return scheme == "https" or (scheme == "http" and host in _LOOPBACK_HOSTS)

    async def _fill_secret(self, page: Any, step: Mapping[str, Any], selector: str, timeout_ms: int) -> None:
        """비밀 입력 직전 검사: 같은 탭의 실제 주소가 레시피 도메인과 정확히 같은 https 인가."""
        domain = self._domain(step)
        if not selector or _FRAME_SELECTOR.search(selector):
            raise SecretFillBlocked("secret_fill_selector_not_allowed")
        before = await self._live_href(page)
        if not self._href_allowed(before, domain):
            raise SecretFillBlocked("secret_fill_origin_mismatch")
        pages = list(getattr(self._context, "pages", None) or [])
        if (self._page is not None and page is not self._page) or (pages and page not in pages):
            raise SecretFillBlocked("secret_fill_tab_changed")
        self._secret_screen_url = before
        try:
            await page.fill(selector, str(step.get("value") or ""), timeout=timeout_ms)
        except Exception as exc:  # noqa: BLE001
            # 원 예외는 버린다 — 호출 로그/메시지에 입력 값이 실릴 수 있다.
            raise RuntimeError(f"secret_fill_failed:{type(exc).__name__}") from None
        after = await self._live_href(page)
        self._secret_screen_url = after or "unknown"
        if not self._href_allowed(after, domain):
            try:
                await page.fill(selector, "", timeout=timeout_ms)
            except Exception:  # noqa: BLE001
                pass
            raise SecretFillBlocked("secret_fill_origin_changed")

    async def _evidence_for(self, page: Any) -> dict[str, Any]:
        """비밀이 입력된 화면에 머무는 동안은 DOM/aria/스크린샷을 남기지 않는다."""
        if self._secret_screen_url:
            live = await self._live_href(page)
            if not live or live == self._secret_screen_url:
                return {
                    "url": mask_secrets(live), "dom": dict(_SECRET_WITHHELD),
                    "aria": dict(_SECRET_WITHHELD), "screenshot": dict(_SECRET_WITHHELD),
                }
            self._secret_screen_url = ""
        return await self._collect_evidence(page)

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

    async def _dismiss_overlays(self, page: Any, step: Mapping[str, Any], timeout_ms: int) -> dict[str, Any]:
        """허용 목록의 모달만 닫는다. 없으면 no-op, 닫힘은 재조회로 검증, 횟수 상한을 둔다."""
        try:
            max_dismiss = max(1, int(step.get("max_dismiss") or OVERLAY_DEFAULT_MAX_DISMISS))
        except (TypeError, ValueError):
            max_dismiss = OVERLAY_DEFAULT_MAX_DISMISS
        raw_overlays = step.get("overlays")
        overlays = [o for o in raw_overlays if isinstance(o, Mapping)] if isinstance(raw_overlays, list) else []
        dismissed: list[dict[str, Any]] = []
        if not overlays:
            # 단계에 적힌 overlays 가 없으면 현재 호스트의 공용 목록(approved)을 쓴다. 단계 overlays 가 있으면 그것이 우선.
            from app.services.work_recipe.site_overlays import approved_overlays_for_host, overlays_to_step_specs

            host = urlsplit(str(getattr(page, "url", "") or "")).hostname or ""
            overlays = overlays_to_step_specs(await approved_overlays_for_host(host, self._tenant_id))
        if not overlays:
            return {"overlays_status": "no_op", "reason": "no_allowlist", "dismissed": dismissed}

        async def probe(overlay: Mapping[str, Any]) -> Mapping[str, Any]:
            state = await page.evaluate(
                _OVERLAY_PROBE_SCRIPT, {"root": overlay["root"], "close": overlay["close"]}
            )
            return state if isinstance(state, Mapping) else {}

        for _ in range(max_dismiss):
            progressed = False
            for overlay in overlays:
                state = await probe(overlay)
                host = (urlsplit(str(state.get("url") or getattr(page, "url", "") or "")).hostname or "")
                if not overlay_host_matches(host, str(overlay.get("domain") or "")) or not state.get("rootVisible"):
                    continue
                if OVERLAY_SENSITIVE_MARKERS.search(str(state.get("text") or "")):
                    raise OverlayNotDismissible("sensitive_content", dismissed)
                if not state.get("closeVisible"):
                    raise OverlayNotDismissible("close_control_missing", dismissed)
                await page.click(f"{overlay['root']} {overlay['close']}", timeout=timeout_ms)
                after = await probe(overlay)
                if after.get("rootVisible"):
                    raise OverlayNotDismissible("not_closed_after_click", dismissed)
                dismissed.append({"domain": overlay["domain"], "root": overlay["root"], "verified_closed": True})
                progressed = True
            if not progressed:
                break
        else:
            # 상한만큼 닫았는데도 허용 목록 모달이 다시 떠 있으면 무한 루프 대신 사람에게 넘긴다.
            for overlay in overlays:
                state = await probe(overlay)
                host = (urlsplit(str(state.get("url") or "")).hostname or "")
                if state.get("rootVisible") and overlay_host_matches(host, str(overlay.get("domain") or "")):
                    raise OverlayNotDismissible("reappeared_over_limit", dismissed)
        return {
            "overlays_status": "dismissed" if dismissed else "no_op",
            "reason": "" if dismissed else "no_overlay_present",
            "dismissed": dismissed,
        }

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
            if self._is_secret_fill(step):
                await self._fill_secret(page, step, selector, timeout_ms)
                return None
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
        if action == "dismiss_overlays":
            return await self._dismiss_overlays(page, step, timeout_ms)
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
