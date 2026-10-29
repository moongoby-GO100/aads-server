"""자연어 지시를 저장된 Work Recipe 실행으로 연결한다."""
from __future__ import annotations

import hmac
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from app.services.channel_router import ActionIntent, ChannelRouter
from app.services.work_recipe.audit import GuardedRunRecorder, record_credential_use
from app.services.work_recipe.credential_scope import assert_credential_allowed
from app.services.work_recipe.executor import BrowserRecipeExecutor
from app.services.work_recipe.guard import is_internal_host
from app.services.work_recipe.player import RunResult, play_recipe
from app.services.work_recipe.human_login import (
    HumanLoginExecutor, LoginPolicyStore, validate_login_steps,
)
from app.services.work_recipe.schema import WorkRecipe, parse_recipe
from app.services.work_recipe import shinhan_corporate_manual as shinhan_manual
from app.services.work_recipe import vault_reference
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
    login_contract = recipe.metadata.get("login_contract") or {}
    if login_contract.get("submit_step"):
        validate_login_steps(recipe, login_contract)
    coupang = recipe.name in {part["name"] for part in COUPANGEATS_LOGIN_FRAGMENTS}
    if coupang:
        validate_coupangeats_recipe(recipe)
        if (
            not browser_session_id
            or browser_work_key != recipe.metadata["work_key"]
            or triggered_by == f"chat:{recipe.metadata.get('source_chat_session_id')}"
        ):
            raise ValueError("coupangeats_pc_session_or_replay_boundary_required")
        from app.browser_bridge.service import get_browser_bridge_service

        session = get_browser_bridge_service().sessions.get(browser_session_id)
        if (
            session is None
            or session.work_key != browser_work_key
            or str(session.endpoint.kind.value) != "local_agent"
        ):
            raise ValueError("coupangeats_pc_lane_required")
    if shinhan_manual.is_shinhan_recipe(recipe):
        # 금융 레시피는 락 → 서버측 승인 → Vault 해석 순서가 따로 있다. 신한 로직은 전부 그 모듈에 있다.
        return await shinhan_manual.run_financial_recipe(
            recipe,
            action_intent=action_intent,
            tenant_id=tenant_id,
            intent_inputs=intent_inputs,
            browser_session_id=browser_session_id,
            browser_work_key=browser_work_key,
            triggered_by=triggered_by,
            task_id=task_id,
        )
    shinhan_manual.assert_unregistered_runtime_host(recipe, intent_inputs)
    pc_lane = not coupang and _recipe_requires_pc_lane(recipe)
    if pc_lane:
        # Vault 해석과 단계 실행보다 먼저 막는다.
        await _assert_pc_lane_ready(recipe, browser_session_id, browser_work_key)
    resolution: vault_reference.VaultResolution | None = None
    if vault_reference.has_vault_scope(recipe):
        # 승인 판본이 선언한 {credential_id, field} 만 쓴다. 호출자 값·권한은 받지 않고 테넌트는 서버 컨텍스트에서 온다.
        resolution = await vault_reference.resolve_inputs(
            recipe,
            intent_inputs,
            tenant_id=tenant_id,
            browser_work_key=browser_work_key,
        )
        resolved_inputs = resolution.values
    else:
        if not coupang:
            _assert_secret_scope_declared(recipe)
        resolved_inputs = await _scoped_inputs(recipe, intent_inputs)
    recorder = GuardedRunRecorder(
        domain=recipe.domain,
        tenant_id=tenant_id,
        triggered_by=triggered_by,
        task_id=task_id,
        requested_by=triggered_by,
    )
    if resolution is not None:
        recorder = _VaultUseAuditRecorder(recorder, recipe, resolution)
    executor = BrowserRecipeExecutor(
        browser_session_id=browser_session_id,
        browser_work_key=browser_work_key,
        tenant_id=tenant_id,
        **({"secret_fill_steps": resolution.fill_steps} if resolution is not None else {}),
    )
    if coupang:
        step_executor = CoupangVerificationExecutor(executor, recipe, resolved_inputs)
    elif recipe.verify:
        step_executor = GenericVerificationExecutor(executor, recipe)
    else:
        step_executor = executor
    execution_contract = recipe.metadata.get("login_contract")
    auth_user_id = str(action_intent.authenticated_provenance.get("user_id") or triggered_by)
    if isinstance(execution_contract, Mapping) and execution_contract.get("account_ref"):
        from app.services.auth_challenge_orchestrator import auth_wait_user

        auth_user_id = await auth_wait_user(
            tenant_id=str(tenant_id),
            session_id=action_intent.session_id,
            provenance_user=auth_user_id,
        )
    resume_checkpoint = 0
    auth_task_id = str(action_intent.payload.get("auth_task_id") or "")
    if auth_task_id:
        import uuid
        from datetime import datetime, timezone
        from app.core.db_pool import get_pool
        from app.services.browser_task_gateway import _json_dict

        contract = recipe.metadata.get("login_contract") or {}
        async with get_pool().acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT * FROM browser_tasks WHERE tenant_id=$1 AND id=$2 FOR UPDATE",
                    uuid.UUID(str(tenant_id)),
                    uuid.UUID(auth_task_id),
                )
                if not row or row["status"] != "queued" or str(row["user_id"]) != auth_user_id:
                    raise ValueError("auth_resume_not_queued")
                saved = _json_dict(row["result"])
                scope = saved.get("auth_scope") or {}
                checks = {
                    "tenant_id": str(tenant_id),
                    "session_id": action_intent.session_id,
                    "business_ref": contract.get("business_ref"),
                    "account_ref": contract.get("account_ref"),
                    "recipe_name": recipe.name,
                    "recipe_version": recipe.version,
                }
                if any(scope.get(key) != value for key, value in checks.items()) or not saved.get(
                    "resume_artifact_id"
                ):
                    raise ValueError("auth_resume_scope_mismatch")
                if saved.get("browser_session_id", "") != str(browser_session_id or ""):
                    raise ValueError("auth_resume_browser_session_mismatch")
                if scope.get("work_key") != (
                    browser_work_key or recipe.metadata.get("work_key") or recipe.name
                ):
                    raise ValueError("auth_resume_work_key_mismatch")
                if datetime.fromisoformat(
                    scope["expires_at"].replace("Z", "+00:00")
                ) <= datetime.now(timezone.utc):
                    raise ValueError("auth_resume_expired")
                resume_checkpoint = int(scope["checkpoint"])
                # Saved variables may include secrets; never serialize them for resume.
                if any(step.save_as for step in recipe.steps if step.seq <= resume_checkpoint):
                    raise ValueError("auth_resume_saved_variables_unsupported")
        # Probe the retained live session immediately before replay. A disconnected PC,
        # expired login or changed screen must never replay a submit/send step.
        try:
            live_context = {"domain": recipe.domain, "login_contract": contract}
            if executor._route({"context": live_context})["runtime"] != (
                "pc_agent" if scope["lane"] == "pc" else "browser_agent"
            ):
                raise ValueError("auth_resume_lane_mismatch")
            await executor._get_page({"url": scope["target"], "context": live_context})
            for step in recipe.verify:
                observed = await step_executor(
                    {**step.to_dict(), "phase": "verify", "context": live_context}
                )
                if observed.get("ok") is not True:
                    raise ValueError("auth_resume_live_verification_failed")
        except Exception:
            # Release the consumed resume request so both resume and cancel work.
            async with get_pool().acquire() as conn:
                await conn.execute(
                    "UPDATE browser_tasks SET status='waiting_for_user',updated_at=NOW() "
                    "WHERE tenant_id=$1 AND id=$2 AND status='queued'",
                    uuid.UUID(str(tenant_id)),
                    uuid.UUID(auth_task_id),
                )
            raise
        # Recheck ownership after the live probe; no browser I/O under the lock.
        async with get_pool().acquire() as conn:
            async with conn.transaction():
                current = await conn.fetchrow(
                    "SELECT * FROM browser_tasks WHERE tenant_id=$1 AND id=$2 FOR UPDATE",
                    uuid.UUID(str(tenant_id)),
                    uuid.UUID(auth_task_id),
                )
                if (
                    not current
                    or current["status"] != "queued"
                    or str(current["user_id"]) != auth_user_id
                    or _json_dict(current["result"]) != saved
                ):
                    raise ValueError("auth_resume_not_queued")
                await conn.execute(
                    "UPDATE browser_tasks SET status='running',updated_at=NOW() WHERE tenant_id=$1 AND id=$2",
                    uuid.UUID(str(tenant_id)),
                    uuid.UUID(auth_task_id),
                )
        execution_contract = {
            **contract,
            "challenge": {**(contract.get("challenge") or {"kind": "manual"}), "active": False},
        }
    if (
        isinstance(execution_contract, Mapping)
        and execution_contract.get("submit_step")
        and not auth_task_id
    ):
        from app.services.auth_challenge_orchestrator import persist_auth_wait
        from app.services.browser_task_gateway import update_browser_task_status
        from datetime import datetime, timedelta, timezone
        from urllib.parse import urlsplit

        contract = execution_contract
        submit_seq = contract["submit_step"]
        if (
            not resolution or not contract.get("business_ref")
            or not contract.get("account_ref") or not recipe.verify
        ):
            raise ValueError("human_login_checkpoint_scope_required")

        async def wait_login(payload, reason):
            origin = urlsplit(
                (contract.get("observation") or {}).get("origin") or "https://" + recipe.domain
            )
            wait_seconds = contract.get("human_submit_timeout_seconds", 600)
            scope = {
                "tenant_id": str(tenant_id),
                "business_ref": contract["business_ref"],
                "account_ref": contract["account_ref"],
                "session_id": action_intent.session_id,
                "work_key": browser_work_key or recipe.metadata.get("work_key") or recipe.name,
                "lane": executor.login_lane(payload),
                "target": f"{origin.scheme}://{origin.netloc}{origin.path}",
                "recipe_name": recipe.name,
                "recipe_version": recipe.version,
                "checkpoint": submit_seq,
                "expires_at": (
                    datetime.now(timezone.utc) + timedelta(seconds=wait_seconds)
                ).isoformat(),
            }
            message = reason
            if reason == "human_submit_required":
                message = (
                    f"{recipe.domain} 사이트 로그인 버튼만 눌러 주세요 "
                    f"(PC: {executor.login_pc_label})"
                )
            return await persist_auth_wait(
                scope=scope,
                user_id=auth_user_id,
                reason=reason,
                verify=[step.to_dict() for step in recipe.verify],
                browser_session_id=str(browser_session_id or ""),
                current_step=message,
            )

        async def finish_login(task, ok, reason=""):
            updated = await update_browser_task_status(
                tenant_id=str(tenant_id),
                task_id=task["id"],
                status="completed" if ok else "failed",
                result={
                    "reason": reason or "human_login_completed",
                    "keep_browser_open": True,
                },
            )
            if updated is None:
                raise RuntimeError("login_checkpoint_task_missing")

        step_executor = HumanLoginExecutor(
            step_executor, executor, contract,
            LoginPolicyStore(
                tenant_id, recipe.domain,
                [use["credential_id"] for use in resolution.uses],
            ),
            wait_login, finish_login,
        )
    try:
        played = await play_recipe(
            recipe,
            step_executor,
            resolved_inputs,
            recorder=recorder,
            max_risk="IRREVERSIBLE",
            context={
                "domain": recipe.domain,
                "resume_checkpoint": resume_checkpoint,
                **(
                    {"login_contract": execution_contract} if execution_contract is not None else {}
                ),
                # Recipe metadata is registered/approved along with the immutable
                # recipe version.  It is the only source allowed to request a
                # native PC lane; a chat directive cannot promote itself there.
                "smart_browser": {
                    "native_auth_required": bool(recipe.metadata.get("native_auth_required")),
                    "requires_local_security_programs": bool(
                        recipe.metadata.get("requires_local_security_programs")
                    ),
                    # 봇/WAF 가 데이터센터 IP 를 막는 사이트는 처음부터 PC 레인으로 간다.
                    # native_auth_required 를 대신 켜서 우회하지 않는다.
                    "server_access_blocked": coupang
                    or pc_lane
                    or bool(recipe.metadata.get("server_access_blocked")),
                    "requires_residential_ip": bool(recipe.metadata.get("requires_residential_ip")),
                },
            },
        )
        contract = recipe.metadata.get("login_contract")
        if isinstance(contract, Mapping) and not played.ok:
            waiting = next(
                (step for step in played.steps if step.recovery.get("route") == "human_gateway"), None
            )
            if (
                waiting is not None
                and not contract.get("submit_step")
                and contract.get("business_ref")
                and contract.get("account_ref")
                and recipe.verify
            ):
                from datetime import datetime, timedelta, timezone
                from app.services.auth_challenge_orchestrator import persist_auth_wait
                from urllib.parse import urlsplit

                observation = contract.get("observation") or {}
                origin = observation.get("origin") or "https://" + recipe.domain
                parsed = urlsplit(origin)
                scope = {
                    "tenant_id": str(tenant_id),
                    "business_ref": contract["business_ref"],
                    "account_ref": contract["account_ref"],
                    "session_id": action_intent.session_id,
                    "work_key": browser_work_key or recipe.metadata.get("work_key") or recipe.name,
                    "lane": "pc"
                    if executor._route({"context": {"login_contract": contract}})["runtime"]
                    == "pc_agent"
                    else "server",
                    "target": f"{parsed.scheme}://{parsed.netloc}{parsed.path}",
                    "recipe_name": recipe.name,
                    "recipe_version": recipe.version,
                    "checkpoint": waiting.seq
                    if waiting.recovery.get("step_completed")
                    else waiting.seq - 1,
                    "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
                }
                task = await persist_auth_wait(
                    scope=scope,
                    user_id=auth_user_id,
                    reason=str(waiting.recovery.get("reason") or "auth_required"),
                    verify=[step.to_dict() for step in recipe.verify],
                    browser_session_id=str(browser_session_id or ""),
                )
                waiting.recovery.update(
                    {
                        "auth_task_id": task["id"],
                        "status": "waiting_for_user",
                        "checkpoint": waiting.seq
                        if waiting.recovery.get("step_completed")
                        else waiting.seq - 1,
                        "expires_at": scope["expires_at"],
                    }
                )
        if auth_task_id:
            from app.services.browser_task_gateway import update_browser_task_status

            await update_browser_task_status(
                tenant_id=str(tenant_id),
                task_id=auth_task_id,
                status="completed" if played.ok else "failed",
            )
        return played
    except Exception:
        if auth_task_id:
            from app.services.browser_task_gateway import update_browser_task_status

            await update_browser_task_status(
                tenant_id=str(tenant_id),
                task_id=auth_task_id,
                status="failed",
            )
        raise


class _VaultUseAuditRecorder:
    """run 이 시작되면 Vault 사용을 감사 1행씩 남긴다. 감사에는 credential_id·field·허용 여부만 들어간다."""

    def __init__(
        self, inner: Any, recipe: WorkRecipe, resolution: vault_reference.VaultResolution
    ) -> None:
        self._inner = inner
        self._recipe = recipe
        self._resolution = resolution

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def start_run(self, **kwargs: Any) -> Any:
        run_id = self._inner.start_run(**kwargs)
        if hasattr(run_id, "__await__"):
            run_id = await run_id
        for use in self._resolution.uses:
            try:
                await record_credential_use(
                    run_id,
                    self._recipe.domain,
                    self._recipe.name,
                    {
                        "credential_id": use["credential_id"],
                        "field": use["field"],
                        "decision": "allow",
                    },
                )
            except Exception as exc:  # noqa: BLE001 - 감사 실패가 실행 결과를 바꾸지 않는다
                logger.warning("vault_reference_audit_failed type=%s", type(exc).__name__)
        return run_id


def _assert_secret_scope_declared(recipe: WorkRecipe) -> None:
    """secret 입력이 있는 일반 레시피는 승인 판본에 credential_scope 가 있어야 한다.

    scope 없는 레시피가 호출자의 {value} 를 받던 경로를 닫는다. 문자열 scope(쿠팡이츠·배달 조각)는
    기존 별도 계약이라 그대로 둔다.
    """
    if not any(item.secret for item in recipe.inputs):
        return
    scope = recipe.metadata.get("credential_scope")
    if isinstance(scope, str) and scope.strip():
        return
    raise vault_reference.VaultReferenceError("credential_scope_missing")


_PC_LANES = frozenset({"pc", "pc_agent"})


def _recipe_requires_pc_lane(recipe: WorkRecipe) -> bool:
    return str(recipe.metadata.get("lane") or "").strip().lower() in _PC_LANES


async def _assert_pc_lane_ready(
    recipe: WorkRecipe, browser_session_id: str | None, browser_work_key: str | None
) -> None:
    """metadata.lane 이 pc 인 레시피는 PC Agent 세션 없이는 한 단계도 실행하지 않는다."""
    from app.browser_bridge.service import get_browser_bridge_service

    service = get_browser_bridge_service()
    session = service.sessions.get(browser_session_id) if browser_session_id else None
    if session is None or str(session.endpoint.kind.value) != "local_agent":
        raise ValueError("pc_lane_session_required")
    recipe_work_key = str(recipe.metadata.get("work_key") or "")
    if recipe_work_key and (browser_work_key != recipe_work_key or session.work_key != recipe_work_key):
        raise ValueError("pc_lane_session_required")
    if recipe.steps and recipe.steps[0].action != "navigate":
        # 조각 레시피: 앞 레시피가 만든 화면 위에서 시작하므로 현재 위치를 먼저 확인한다.
        if not _host_within(await _session_current_url(service, browser_session_id), recipe.domain):
            raise ValueError(f"recipe_precondition_not_met:{recipe.domain}")


async def _session_current_url(service: Any, browser_session_id: str) -> str:
    """세션 브라우저의 실제 현재 URL. 확인하지 못하면 빈 문자열."""
    try:
        context, _ = await service.acquire_playwright_context(session_id=browser_session_id)
        pages = list(getattr(context, "pages", []) or [])
        if not pages:
            return ""
        return str(await pages[0].evaluate("window.location.href") or "")
    except Exception:
        logger.warning("recipe_precondition_url_unavailable session=%s", browser_session_id)
        return ""


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
        contract = self.recipe.metadata.get("login_contract") or {}
        server_verified = contract.get("access") == "server_verified"
        if not internal and lane != "pc_agent" and self.recipe.metadata.get("lane") != "server" and not server_verified:
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
        collect = getattr(self.executor, "_evidence_for", None) or self.executor._collect_evidence
        evidence = await collect(page)
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
        if vault_reference.has_vault_scope(recipe):
            # Vault 참조 계약 레시피는 resolve_inputs 로만 해석한다 — 호출자 value 로 우회할 수 없다.
            raise vault_reference.VaultReferenceError("plain_secret_rejected", input_name=item.name)
        credential_ref = resolved[item.name]
        if not isinstance(credential_ref, Mapping) or "value" not in credential_ref:
            raise ValueError(
                f"secret 입력 {item.name!r}은 credential_scope reference가 필요합니다"
            )
        await assert_credential_allowed(credential_ref, recipe.domain, recipe.name)
        resolved[item.name] = credential_ref["value"]
    return resolved
