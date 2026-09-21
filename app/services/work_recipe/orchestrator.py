"""자연어 지시를 저장된 Work Recipe 실행으로 연결한다."""
from __future__ import annotations

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
    return await play_recipe(
        recipe,
        executor,
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
                "server_access_blocked": bool(recipe.metadata.get("server_access_blocked")),
                "requires_residential_ip": bool(recipe.metadata.get("requires_residential_ip")),
            },
        },
    )


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
