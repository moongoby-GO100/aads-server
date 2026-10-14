"""자연어 지시를 저장된 Work Recipe 실행으로 연결한다."""
from __future__ import annotations

import hmac
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from app.services.channel_router import ActionIntent, ChannelRouter
from app.services.work_recipe.audit import GuardedRunRecorder
from app.services.work_recipe.credential_scope import assert_credential_allowed
from app.services.work_recipe.executor import BrowserRecipeExecutor
from app.services.work_recipe.guard import is_internal_host
from app.services.work_recipe.player import RunResult, play_recipe
from app.services.work_recipe.schema import WorkRecipe, parse_recipe
from app.services.work_recipe import shinhan_corporate_manual as shinhan_manual
from app.services.work_recipe.store import list_recipes, normalize_domain, row_to_recipe
from app.services.browser_recipe_registry import (
    COUPANGEATS_LOGIN_FRAGMENTS, COUPANGEATS_LOGIN_URL,
    matches_coupangeats_vault_identity,
)

logger = logging.getLogger(__name__)

_URL_HOST = re.compile(r"https?://([^/\s]+)", re.IGNORECASE)


_DATE_WORDS = re.compile(
    r"그저께|그제|어제|오늘|내일|이번\s*주|지난\s*주|저번\s*주|이번\s*달|지난\s*달|저번\s*달|금주|전주|금일|전일|당일"
)
_REQUEST_ENDING = re.compile(
    r"^(.*?)(해줘|해주세요|해줄래|받아줘|받아주세요|알려줘|알려주세요|보여줘|보여주세요|가져와|가져와줘|뽑아줘|줘|주세요)$"
)
_NON_WORD = re.compile(r"[^\w]+", re.UNICODE)


def _squash(value: str) -> str:
    return _NON_WORD.sub("", _DATE_WORDS.sub(" ", value.casefold()))


def _content_tokens(text: str) -> list[str]:
    tokens: list[str] = []
    for raw in _DATE_WORDS.sub(" ", text.casefold()).split():
        token = _NON_WORD.sub("", raw)
        ending = _REQUEST_ENDING.match(token)
        if ending:
            token = ending.group(1)
        if len(token) >= 2:
            tokens.append(token)
    return tokens


def _draft_phrase_matches(text: str, tokens: list[str], phrase: str) -> bool:
    squashed = _squash(phrase)
    if len(squashed) < 2:
        return False
    return squashed in _squash(text) or bool(tokens and all(token in squashed for token in tokens))


async def _draft_matches(directive_text: str, tenant_id: Any) -> list[tuple[Mapping[str, Any], str]]:
    """active 자동화 초안 중 title·aliases 가 지시와 맞는 것과, 맞은 문구."""
    from app.services import automation_draft_service as drafts

    if not drafts.drafts_enabled():
        return []
    try:
        active = await drafts.list_active_drafts(tenant_id=str(tenant_id))
    except Exception:
        logger.warning("automation_draft_lookup_failed", exc_info=True)
        return []
    tokens = _content_tokens(directive_text)
    hits: list[tuple[Mapping[str, Any], str]] = []
    for draft in active:
        phrases = [str(draft.get("title") or ""), *(str(a) for a in draft.get("aliases") or [])]
        phrase = next((p for p in phrases if _draft_phrase_matches(directive_text, tokens, p)), None)
        if phrase is not None:
            hits.append((draft, phrase))
    return hits


def _row_recipe(row: Mapping[str, Any]) -> WorkRecipe | None:
    recipe = row_to_recipe(row)
    if recipe is None and isinstance(row.get("spec"), Mapping):
        recipe = parse_recipe(row["spec"])
    return recipe


@dataclass(frozen=True)
class RecipeMatch:
    """말로 찾은 레시피 한 건과 그 매칭의 강도.

    needs_confirmation 은 초안 제목·별칭으로만 잡혔고 날짜어·요청어미를 뺀 핵심어가
    하나뿐인 경우다 — "매출 알려줘" 처럼 다른 뜻의 말도 같은 자동화로 잡힐 수 있어
    사용자가 확인하기 전에는 실행하지 않는다.
    """

    recipe: WorkRecipe | None = None
    needs_confirmation: bool = False
    draft_id: str = ""
    draft_title: str = ""
    matched_phrase: str = ""
    matched_draft_ids: frozenset[str] = field(default_factory=frozenset)


async def _resolve_all(
    directive_text: str, tenant_id: Any
) -> tuple[list[WorkRecipe], list[tuple[Mapping[str, Any], str]]]:
    """(후보 레시피, 초안 매칭). 초안 매칭은 이름·도메인 매칭이 없을 때만 채워진다."""
    text = str(directive_text or "").strip().casefold()
    if not text:
        return [], []
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
        recipe = _row_recipe(row)
        if recipe is not None:
            if name_match:
                name_matches.append(recipe)
            elif domain_match:
                domain_matches.append(recipe)
    # 사용자가 이름을 명시한 단일 후보는 같은 도메인의 다른 레시피보다 강하다.
    # 이름 후보가 있으면 도메인 후보는 쓰지 않는다.
    if name_matches:
        return name_matches, []
    if domain_matches:
        return domain_matches, []
    if not rows:
        return [], []
    hits = await _draft_matches(directive_text, tenant_id)
    names = {str(n) for draft, _ in hits for n in draft.get("recipe_names") or []}
    if not names:
        return [], []
    found: list[WorkRecipe] = []
    for row in rows:
        if str(row.get("name") or "") in names:
            recipe = _row_recipe(row)
            if recipe is not None:
                found.append(recipe)
    return found, hits


async def resolve_recipe_candidates(directive_text: str, tenant_id: Any) -> list[WorkRecipe]:
    """지시에 맞는 레시피 후보 전체. 도메인·이름 매칭이 우선이고, 없을 때만 자동화 제목·별칭을 본다."""
    return (await _resolve_all(directive_text, tenant_id))[0]


async def resolve_recipe_match(directive_text: str, tenant_id: Any) -> RecipeMatch:
    """후보가 정확히 하나일 때만 선택하고, 그 매칭이 확인 없이 실행해도 되는 강도인지 함께 돌려준다."""
    candidates, hits = await _resolve_all(directive_text, tenant_id)
    if len(candidates) != 1:
        return RecipeMatch()
    recipe = candidates[0]
    if not hits:
        return RecipeMatch(recipe=recipe)
    own = [(d, p) for d, p in hits if recipe.name in {str(n) for n in d.get("recipe_names") or []}]
    if not own:
        return RecipeMatch(recipe=recipe)
    draft, phrase = own[0]
    return RecipeMatch(
        recipe=recipe,
        needs_confirmation=len(_content_tokens(directive_text)) <= 1,
        draft_id=str(draft.get("id") or ""),
        draft_title=str(draft.get("title") or ""),
        matched_phrase=phrase,
        matched_draft_ids=frozenset(str(d.get("id") or "") for d, _ in own),
    )


async def resolve_recipe(directive_text: str, tenant_id: Any) -> WorkRecipe | None:
    """후보가 정확히 하나일 때만 선택한다. 둘 이상이거나 확인이 필요한 매칭이면 추측하지 않고 None."""
    match = await resolve_recipe_match(directive_text, tenant_id)
    return None if match.needs_confirmation else match.recipe


@dataclass(frozen=True)
class RecipeConfirmation:
    """확인 필요 매칭 — 실행하지 않고 사용자에게 되묻는 결과. RunResult 대신 돌려준다."""

    match: RecipeMatch

    status = "confirmation_required"

    @property
    def card(self) -> dict[str, Any]:
        from app.services import automation_draft_service as drafts

        title = self.match.draft_title
        card = drafts.build_card("active", title=title)
        card["headline"] = "실행할까요?"
        card["body"] = f"{title}를 실행할까요?"
        card["buttons"] = [
            {"id": "confirm_run", "label": "실행", "primary": True},
            {"id": "decline_run", "label": "아니요", "primary": False},
        ]
        return card

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "draft_id": self.match.draft_id,
            "draft_title": self.match.draft_title,
            "matched_phrase": self.match.matched_phrase,
            "card": self.card,
        }


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
) -> RunResult | RecipeConfirmation | None:
    """Run only a routed command; raw page text has no execution path here.

    핵심어 하나로만 잡힌 초안 매칭은 실행하지 않고 RecipeConfirmation 을 돌려준다.
    payload.confirmed_draft_id 가 매칭된 초안과 같을 때만 실행한다.
    """
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
        # resolve_recipe 는 확인 필요 매칭도 None 으로 돌려준다 — 그 경우만 따로 가려낸다.
        match = await _confirmation_match(directive_text, tenant_id)
        if match is None:
            return None
        confirmed = str(action_intent.payload.get("confirmed_draft_id") or "")
        if not confirmed or confirmed not in match.matched_draft_ids:
            return RecipeConfirmation(match)
        recipe = match.recipe
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
    if shinhan_manual.is_shinhan_recipe(recipe):
        # 금융 레시피는 락 → 서버측 승인 → Vault 해석 순서가 따로 있다. 신한 로직은 전부 그 모듈에 있다.
        return await shinhan_manual.run_financial_recipe(
            recipe, action_intent=action_intent, tenant_id=tenant_id, intent_inputs=intent_inputs,
            browser_session_id=browser_session_id, browser_work_key=browser_work_key,
            triggered_by=triggered_by, task_id=task_id,
        )
    shinhan_manual.assert_unregistered_runtime_host(recipe, intent_inputs)
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
    if coupang:
        step_executor = CoupangVerificationExecutor(executor, recipe, resolved_inputs)
    elif recipe.verify:
        step_executor = GenericVerificationExecutor(executor, recipe)
    else:
        step_executor = executor
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


async def _confirmation_match(directive_text: str, tenant_id: Any) -> RecipeMatch | None:
    from app.services import automation_draft_service as drafts

    if not drafts.drafts_enabled():
        return None
    match = await resolve_recipe_match(directive_text, tenant_id)
    return match if match.needs_confirmation and match.recipe is not None else None


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


GENERIC_VERIFY_ASSERTIONS = frozenset({"element_visible", "url_equals", "text_contains"})

_GENERIC_VERIFY_SCRIPT = """(args) => {
    const norm = s => String(s ?? '').replace(/\\s+/g, ' ').trim().toLowerCase();
    const el = args.selector ? document.querySelector(args.selector) : null;
    const visible = !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
    const scope = args.selector ? el : document.body;
    const contains = !!scope && !!args.expected &&
        norm(scope.innerText ?? scope.textContent).includes(norm(args.expected));
    return {url: window.location.href, visible, contains};
}"""


def _url_host(url: str) -> str:
    match = _URL_HOST.match(str(url or "").strip())
    return normalize_domain(match.group(1)) if match else ""


def _host_within(url: str, domain: str) -> bool:
    host, base = _url_host(url), normalize_domain(domain)
    return bool(host and base and (host == base or host.endswith("." + base)))


class GenericVerificationExecutor:
    """Read-only verify phase for recipes that are not tied to one vendor.

    Only predicates leave the browser: page text and field values never enter
    the result.  A server-lane browser may vouch only for internal hosts; any
    other domain must be verified from the PC lane.
    """

    def __init__(self, executor: BrowserRecipeExecutor, recipe: WorkRecipe):
        self.executor = executor
        self.recipe = recipe

    async def __call__(self, payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("phase") != "verify":
            return await self.executor(payload)
        lane = self.executor._route(payload)["runtime"]
        internal = is_internal_host(self.recipe.domain)
        if not internal and lane != "pc_agent":
            return {"ok": False, "error": "pc_agent_lane_required", "route": lane}
        page = self.executor._page
        if page is None:
            return {"ok": False, "error": "missing_verify_page", "route": lane}
        assertion = payload.get("assertion")
        expected = payload.get("expected")
        selector = str(payload.get("selector") or "")
        if (assertion not in GENERIC_VERIFY_ASSERTIONS
                or (assertion == "element_visible" and not selector)
                or (assertion != "element_visible" and not str(expected or ""))):
            return {"ok": False, "error": "unsupported_verify_assertion", "route": lane}
        try:
            screen = await page.evaluate(_GENERIC_VERIFY_SCRIPT, {
                "selector": selector, "expected": str(expected or ""),
            })
            if not isinstance(screen, dict):
                raise ValueError("invalid_verify_screen_state")
            current_url = str(screen.get("url") or "")
            if assertion == "element_visible":
                valid = bool(screen.get("visible"))
            elif assertion == "text_contains":
                valid = bool(screen.get("contains"))
            else:
                valid = current_url.rstrip("/") == str(expected).rstrip("/")
            valid = valid and _host_within(current_url, self.recipe.domain)
        except Exception:
            valid = False
        if not valid:
            return {"ok": False, "error": "verification_failed", "route": lane}
        evidence = await self.executor._collect_evidence(page)
        if self.executor._evidence_shows_block({"evidence": evidence}):
            return {"ok": False, "error": "verification_blocked_page", "route": lane}
        for key in ("dom", "aria"):
            if isinstance(evidence.get(key), dict):
                evidence[key].pop("text", None)
        screenshot = evidence.get("screenshot") or {}
        if screenshot.get("status") != "captured" or not screenshot.get("url"):
            return {"ok": False, "error": "verification_screen_artifact_required", "route": lane}
        return {"ok": True, "route": lane,
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
