"""자연어 지시를 저장된 Work Recipe 실행으로 연결한다."""
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

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
    matches: list[WorkRecipe] = []
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
            matches.append(recipe)
    # 둘 이상이면 추측하지 않는다. 이름+도메인으로 유일해질 때만 실행한다.
    return matches[0] if len(matches) == 1 else None


async def run_directive(
    directive_text: str,
    tenant_id: Any,
    *,
    inputs: Mapping[str, Any] | None = None,
    browser_session_id: str | None = None,
    browser_work_key: str | None = None,
    triggered_by: str = "",
    task_id: str | None = None,
) -> RunResult | None:
    recipe = await resolve_recipe(directive_text, tenant_id)
    if recipe is None:
        return None
    resolved_inputs = await _scoped_inputs(recipe, inputs or {})
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
        context={"domain": recipe.domain},
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
