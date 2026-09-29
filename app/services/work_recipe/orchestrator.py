"""자연어 지시를 저장된 Work Recipe 실행으로 연결한다."""
from __future__ import annotations

import hmac
import re
from collections.abc import Mapping
from typing import Any

from app.services.channel_router import ActionIntent, ChannelRouter
from app.services.work_recipe.audit import GuardedRunRecorder
from app.services.work_recipe.credential_scope import assert_credential_allowed
from app.services.work_recipe.executor import BrowserRecipeExecutor
from app.services.work_recipe.player import RunResult, play_recipe
from app.services.work_recipe.schema import WorkRecipe, parse_recipe
from app.services.work_recipe.store import list_recipes, normalize_domain, row_to_recipe
from app.services.browser_recipe_registry import (
    COUPANGEATS_LOGIN_FRAGMENTS, COUPANGEATS_LOGIN_URL,
    matches_coupangeats_vault_identity,
)

_URL_HOST = re.compile(r"https?://([^/\s]+)", re.IGNORECASE)


async def resolve_recipe(directive_text: str, tenant_id: Any) -> WorkRecipe | None:
    """도메인 또는 정확한 레시피 이름이 지시에 드러난 후보만 선택한다."""
    text = str(directive_text or "").strip().casefold()
    if not text:
        return None
    hosts = {normalize_domain(match) for match in _URL_HOST.findall(text)}
    rows = await list_recipes(tenant_id=tenant_id)
    name_matches: list[WorkRecipe] = []
    domain_matches: list[WorkRecipe] = []
    for row in rows:
        name = str(row.get("name") or "").strip().casefold()
        domain = normalize_domain(str(row.get("domain") or ""))
        name_match = bool(name and name in text)
        domain_match = bool(domain and (domain in text or domain in hosts))
        if not (name_match or domain_match):
            continue
        recipe = row_to_recipe(row)
        if recipe is None and isinstance(row.get("spec"), Mapping):
            recipe = parse_recipe(row["spec"])
        if recipe is not None:
            if name_match:
                name_matches.append(recipe)
            elif domain_match:
                domain_matches.append(recipe)
    # 사용자가 이름을 명시한 단일 후보는 같은 도메인의 다른 레시피보다 강하다.
    # 이름 후보끼리도 둘 이상이면 추측하지 않고, 이름이 없을 때만 도메인을 쓴다.
    if len(name_matches) == 1:
        return name_matches[0]
    if name_matches:
        return None
    return domain_matches[0] if len(domain_matches) == 1 else None


async def run_directive(
    directive_text: str,
    tenant_id: Any,
    *,
    inputs: Mapping[str, Any] | None = None,
    browser_session_id: str | None = None,
    browser_work_key: str | None = None,
    triggered_by: str = "",
    task_id: str | None = None,
    action_intent: ActionIntent | None = None,
) -> RunResult | None:
    """Run only a routed command; raw page text has no execution path here."""
    if action_intent is None:
        # The executor boundary must never manufacture authority for raw text.
        # An authenticated ingress must route every command first.
        raise ValueError("action_intent_required")
    action_intent = ChannelRouter().validate_action_intent(
        action_intent, capability="recipe.execute"
    )
    if action_intent.tenant_id != str(tenant_id):
        raise ValueError("action_intent_tenant_mismatch")
    directive_text = str(action_intent.payload.get("directive") or "")
    # Only arguments bound into the authenticated ActionIntent may reach a
    # recipe.  This prevents an observation/resume payload from being supplied
    # alongside a valid directive after ingress routing.
    intent_inputs = action_intent.payload.get("inputs", inputs or {})
    if not isinstance(intent_inputs, Mapping):
        raise ValueError("action_intent_inputs_invalid")
    ChannelRouter().assert_no_untrusted_page_data(
        intent_inputs, envelope=action_intent, capability="recipe.execute"
    )
    recipe = await resolve_recipe(directive_text, tenant_id)
    if recipe is None:
        return None
    coupang = recipe.name in {part["name"] for part in COUPANGEATS_LOGIN_FRAGMENTS}
    if coupang:
        validate_coupangeats_recipe(recipe)
        if (not browser_session_id or browser_work_key != recipe.metadata["work_key"]
                or triggered_by == f"chat:{recipe.metadata.get('source_chat_session_id')}"):
            raise ValueError("coupangeats_pc_session_or_replay_boundary_required")
        from app.browser_bridge.service import get_browser_bridge_service

        session = get_browser_bridge_service().sessions.get(browser_session_id)
        if (session is None or session.work_key != browser_work_key
                or str(session.endpoint.kind.value) != "local_agent"):
            raise ValueError("coupangeats_pc_lane_required")
    resolved_inputs = await _scoped_inputs(recipe, intent_inputs)
    recorder = GuardedRunRecorder(
        domain=recipe.domain,
        tenant_id=tenant_id,
        triggered_by=triggered_by,
        task_id=task_id,
        requested_by=triggered_by,
    )
    executor = BrowserRecipeExecutor(
        browser_session_id=browser_session_id,
        browser_work_key=browser_work_key,
    )
    step_executor = CoupangVerificationExecutor(executor, recipe, resolved_inputs) if coupang else executor
    return await play_recipe(
        recipe,
        step_executor,
        resolved_inputs,
        recorder=recorder,
        max_risk="IRREVERSIBLE",
        context={
            "domain": recipe.domain,
            # Recipe metadata is registered/approved along with the immutable
            # recipe version.  It is the only source allowed to request a
            # native PC lane; a chat directive cannot promote itself there.
            "smart_browser": {
                "native_auth_required": bool(recipe.metadata.get("native_auth_required")),
                "requires_local_security_programs": bool(recipe.metadata.get("requires_local_security_programs")),
                # 봇/WAF 가 데이터센터 IP 를 막는 사이트는 처음부터 PC 레인으로 간다.
                # native_auth_required 를 대신 켜서 우회하지 않는다.
                "server_access_blocked": coupang or bool(recipe.metadata.get("server_access_blocked")),
                "requires_residential_ip": bool(recipe.metadata.get("requires_residential_ip")),
            },
        },
    )


def validate_coupangeats_recipe(recipe: WorkRecipe) -> None:
    """Apply the same immutable contract at registration and replay."""
    fragment = next((part for part in COUPANGEATS_LOGIN_FRAGMENTS if part["name"] == recipe.name), None)
    if fragment is None:
        return
    evidence = recipe.metadata.get("screen_e2e") or {}
    if (recipe.domain != fragment["domain"] or recipe.metadata.get("lane") != "pc_agent"
            or recipe.metadata.get("work_key") != fragment["work_key"]
            or recipe.metadata.get("starts_when") != fragment["starts_when"]
            or recipe.metadata.get("succeeds_when") != fragment["succeeds_when"]
            or not recipe.metadata.get("source_chat_session_id")
            or not recipe.steps or not recipe.verify
            or (recipe.name != "coupangeats_03_login_confirm"
                and evidence.get("url") != COUPANGEATS_LOGIN_URL)
            or (recipe.name == "coupangeats_03_login_confirm"
                and evidence.get("url") == COUPANGEATS_LOGIN_URL)
            or not evidence.get("screenshot_url")
            or not (evidence.get("run_id") or evidence.get("artifact_id"))
            or evidence.get("outcome") != fragment["succeeds_when"]):
        raise ValueError("coupangeats_recipe_contract_mismatch")
    expected = {
        "coupangeats_01_open_login": ["login_form_visible"],
        "coupangeats_02_vault_fill": ["username_matches_vault", "password_filled"],
        "coupangeats_03_login_confirm": ["authenticated_username_matches_vault", "authenticated_store_matches_vault"],
    }[recipe.name]
    if [step.extra.get("assertion") for step in recipe.verify] != expected:
        raise ValueError("coupangeats_verify_contract_mismatch")
    if recipe.name == "coupangeats_01_open_login" and recipe.steps[0].url != COUPANGEATS_LOGIN_URL:
        raise ValueError("coupangeats_login_url_mismatch")
    if recipe.name == "coupangeats_03_login_confirm" and recipe.steps[0].risk != "WRITE_EXTERNAL":
        raise ValueError("coupangeats_submit_approval_required")


class CoupangVerificationExecutor:
    """Evaluate live page state while keeping passwords out of results and audit logs."""

    def __init__(self, executor: BrowserRecipeExecutor, recipe: WorkRecipe, values: Mapping[str, Any]):
        self.executor = executor
        self.recipe = recipe
        self.values = values
        self._screen_username = ""

    async def __call__(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.executor._route(payload)["runtime"] != "pc_agent":
            return {"ok": False, "error": "pc_agent_lane_required", "route": "browser_agent"}
        if payload.get("phase") != "verify":
            result = await self.executor(payload)
            if result.get("ok") and result.get("route") != "pc_agent":
                return {"ok": False, "error": "pc_agent_lane_required", "route": result.get("route")}
            return result
        page = self.executor._page
        if page is None:
            return {"ok": False, "error": "missing_pc_page", "route": "pc_agent"}
        assertion = payload.get("assertion")
        selector = str(payload.get("selector") or "")
        try:
            # The PC Agent facade implements evaluate(), but its locator only
            # supports click/fill/aria_snapshot. Return predicates, never field
            # contents: password and identity cannot enter the audit payload.
            screen = await page.evaluate("""(args) => {
                const el = document.querySelector(args.selector);
                const visible = !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                const value = el ? String(el.value ?? el.textContent ?? '').trim() : '';
                const body = String(document.body?.innerText ?? '').toLowerCase().slice(0, 4000);
                return {url: window.location.href, visible,
                    passwordType: el?.getAttribute('type')?.toLowerCase() === 'password',
                    filled: value.length > 0,
                    usernameMatches: !!args.username && value === args.username,
                    storeMatches: !!args.store && value === args.store,
                    screenText: args.assertion === 'authenticated_username_matches_vault' ||
                        args.assertion === 'authenticated_store_matches_vault' ? value : '',
                    blocked: ['access denied', 'captcha', '로그인 실패', '비밀번호가 틀']
                        .some(marker => body.includes(marker))};
            }""", {
                "selector": selector,
                "assertion": assertion,
                "username": str(self.values.get("vault_username") or ""),
                "store": str(self.values.get("vault_store") or ""),
            })
            if not isinstance(screen, dict):
                raise ValueError("invalid_pc_screen_state")
            current_url = str(screen.get("url") or "")
            page.url = current_url  # click() leaves the facade's cached URL stale.
            designated = (self.recipe.metadata.get("credential_scope") == "yeoljeong-coupangeats"
                          and hmac.compare_digest(str(self.values.get("vault_username") or ""), "mimi77"))
            if assertion == "login_form_visible":
                valid = bool(screen.get("visible") and screen.get("passwordType")
                             and current_url == COUPANGEATS_LOGIN_URL)
            elif assertion == "username_matches_vault":
                valid = bool(designated and screen.get("visible") and screen.get("usernameMatches"))
            elif assertion == "password_filled":
                valid = bool(screen.get("visible") and screen.get("passwordType") and screen.get("filled"))
            elif assertion == "authenticated_username_matches_vault":
                valid = bool(designated and screen.get("visible") and screen.get("usernameMatches")
                             and "/merchant/login" not in current_url)
                self._screen_username = str(screen.get("screenText") or "") if valid else ""
            elif assertion == "authenticated_store_matches_vault":
                valid = bool(screen.get("visible") and matches_coupangeats_vault_identity(
                    vault_scope=str(self.recipe.metadata.get("credential_scope") or ""),
                    vault_username=str(self.values.get("vault_username") or ""),
                    vault_store=str(self.values.get("vault_store") or ""),
                    screen_username=self._screen_username,
                    screen_store=str(screen.get("screenText") or ""))
                             and "/merchant/login" not in current_url)
            else:
                valid = False
            valid = valid and not screen.get("blocked")
        except Exception:
            valid = False
        if not valid:
            return {"ok": False, "error": "coupangeats_verification_failed", "route": "pc_agent"}
        evidence = await self.executor._collect_evidence(page)
        screenshot = evidence.get("screenshot") or {}
        if screenshot.get("status") != "captured" or not screenshot.get("url"):
            return {"ok": False, "error": "coupangeats_screen_artifact_required", "route": "pc_agent"}
        return {"ok": True, "route": "pc_agent",
                "output": {"verified": True, "screenshot_path": screenshot["url"]},
                "evidence": evidence}


async def _scoped_inputs(
    recipe: WorkRecipe, inputs: Mapping[str, Any]
) -> dict[str, Any]:
    """secret 입력은 범위가 선언된 vault reference에서만 꺼낸다."""
    resolved = dict(inputs)
    for item in recipe.inputs:
        if not item.secret or item.name not in resolved:
            continue
        credential_ref = resolved[item.name]
        if not isinstance(credential_ref, Mapping) or "value" not in credential_ref:
            raise ValueError(
                f"secret 입력 {item.name!r}은 credential_scope reference가 필요합니다"
            )
        await assert_credential_allowed(credential_ref, recipe.domain, recipe.name)
        resolved[item.name] = credential_ref["value"]
    return resolved
