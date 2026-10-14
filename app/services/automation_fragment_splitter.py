"""시연 결과를 로그인/이동/조회 조각으로 나눠 등록한다 — 설계서 7.4·7.6, PRD FR-16·17·19, AC-06.

- 조각 이름은 `{domain_slug}_{nn}_{purpose}` (기존 `coupangeats_01_open_login` 규칙과 같다).
  nn 은 같은 도메인에서 이미 쓰인 번호 다음부터 매긴다 — 같은 도메인의 다른 자동화 조각을
  새 버전으로 덮어쓰지 않기 위해서다.
- 같은 도메인·같은 저장 로그인(credential_id)의 **승인된** 로그인 조각이 있으면 그 이름을
  선행 조각으로 연결하고 새 registration 을 만들지 않는다.
- registration 의 `requested_by` 는 사람(초안 owner_user)이다. 채팅 세션 id 를 넣지 않는다.
- 자동 승인(D1): `AUTOMATION_AUTO_APPROVE_READ` 가 켜져 있고, 조각의 max_risk 가 READ 이고,
  같은 도메인에 **사람이** 승인한 이력이 있을 때만 `policy:read_same_domain` 으로 승인한다.
  정책이 승인한 이력은 이력으로 세지 않는다(정책이 자기 근거를 만들지 않게).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.services import automation_draft_service as svc
from app.services.work_recipe import approval_notifier, registration, store
from app.services.work_recipe.schema import (
    RISK_LEVELS, RecipeInput, RecipeStep, WorkRecipe, extract_variables, risk_rank,
)

logger = logging.getLogger(__name__)

PURPOSES: tuple[str, ...] = ("login", "navigate", "query")
POLICY_DECIDER = "policy:read_same_domain"
_NON_HUMAN_DECIDERS = ("policy:", "system:")
_QUERY_ACTIONS = frozenset({"snapshot", "download", "api_call"})
_SUFFIX_LABELS = frozenset({
    "com", "net", "org", "kr", "co", "go", "or", "ne", "ac", "re", "pe", "io", "ai", "app", "biz", "info",
})
_PREFIX_LABELS = frozenset({"www", "m", "store", "login", "auth", "accounts", "app", "my"})
_SLUG_CLEAN = re.compile(r"[^a-z0-9]+")


def domain_slug(domain: str) -> str:
    """store.coupangeats.com → coupangeats, www.hometax.go.kr → hometax."""
    labels = [part for part in store.normalize_domain(domain).split(".") if part]
    while len(labels) > 1 and labels[-1] in _SUFFIX_LABELS:
        labels.pop()
    core = [part for part in labels if part not in _PREFIX_LABELS] or labels
    slug = _SLUG_CLEAN.sub("_", (core[-1] if core else "").lower()).strip("_")
    return slug or "site"


@dataclass
class Fragment:
    purpose: str
    steps: list[dict[str, Any]] = field(default_factory=list)
    name: str = ""
    reused: bool = False


def split_steps(recorded: Sequence[Mapping[str, Any]]) -> list[Fragment]:
    """기록 단계 → 조각들. 단계마다 시연기가 붙인 purpose 를 따르고, 없으면 모양으로 고른다.

    모양 규칙(설계서 7.4): 마지막 로그인 입력 다음의 제출 단계까지 = 로그인,
    마지막 조회 단계(snapshot/download)와 그 바로 앞의 고르기·입력 묶음 = 조회, 그 사이 = 이동.
    """
    purposes = [str(item.get("purpose") or "") for item in recorded]
    if not all(p in PURPOSES for p in purposes):
        purposes = _infer_purposes(recorded)
    fragments: list[Fragment] = []
    for purpose, item in zip(purposes, recorded):
        if not fragments or fragments[-1].purpose != purpose:
            fragments.append(Fragment(purpose))
        fragments[-1].steps.append(dict(item))
    return fragments


def _infer_purposes(recorded: Sequence[Mapping[str, Any]]) -> list[str]:
    steps = [item.get("step") or {} for item in recorded]
    login_end = -1
    for index, step in enumerate(steps):
        names = extract_variables(step.get("value"))
        if step.get("action") == "fill" and any(n.startswith(("secret.", "credential")) for n in names):
            login_end = index
    if login_end >= 0 and login_end + 1 < len(steps) and steps[login_end + 1].get("action") in ("press", "click"):
        login_end += 1
    query_start = len(steps)
    for index in range(len(steps) - 1, login_end, -1):
        if steps[index].get("action") in _QUERY_ACTIONS:
            query_start = index
            while query_start - 1 > login_end and steps[query_start - 1].get("action") in ("select", "fill"):
                query_start -= 1
            break
    return [
        "login" if index <= login_end else "query" if index >= query_start else "navigate"
        for index in range(len(steps))
    ]


def _nn_of(name: str, slug: str) -> int | None:
    match = re.match(rf"^{re.escape(slug)}_(\d{{2}})_", name)
    return int(match.group(1)) if match else None


def assign_names(
    fragments: list[Fragment], *, slug: str, used_numbers: set[int], reusable_login: str | None,
) -> list[Fragment]:
    """재사용할 로그인 조각은 그 이름을 쓰고, 새 조각은 도메인에서 아직 안 쓴 번호를 받는다."""
    next_nn = max(used_numbers, default=0) + 1
    for fragment in fragments:
        if fragment.purpose == "login" and reusable_login:
            fragment.name, fragment.reused = reusable_login, True
            continue
        fragment.name = f"{slug}_{next_nn:02d}_{fragment.purpose}"
        next_nn += 1
    return fragments


def build_recipe(
    fragment: Fragment, *, domain: str, requires: list[str], metadata: Mapping[str, Any],
) -> WorkRecipe:
    steps = [
        RecipeStep.from_dict(dict(item.get("step") or {}), seq=index)
        for index, item in enumerate(fragment.steps, start=1)
    ]
    inputs: list[RecipeInput] = []
    for step in steps:
        for name in step.variables():
            if name not in {item.name for item in inputs}:
                inputs.append(RecipeInput(name=name, secret=name.startswith("secret."), description="vault credential"))
    recipe = WorkRecipe(
        name=fragment.name,
        domain=store.normalize_domain(domain),
        version=1,
        description=str(metadata.get("title") or ""),
        inputs=inputs,
        steps=steps,
        metadata={
            "recorded": True,
            "credentials": "credential_scope",
            "fragment_purpose": fragment.purpose,
            "requires": list(requires),
            **{k: v for k, v in metadata.items() if k != "title"},
        },
    )
    recipe.validate_variables()
    return recipe


# ── 저장소 조회 (기존 store·registration 함수만 쓴다) ──────────────────────

def _metadata_of(row: Mapping[str, Any]) -> dict[str, Any]:
    spec = row.get("spec")
    meta = spec.get("metadata") if isinstance(spec, Mapping) else None
    return dict(meta) if isinstance(meta, Mapping) else {}


async def find_reusable_login(*, tenant_id: str, domain: str, credential_id: str | None) -> dict[str, Any] | None:
    """같은 도메인·같은 저장 로그인으로 승인돼 켜져 있는 로그인 조각(최신 버전)."""
    if not credential_id:
        return None
    rows = await store.list_recipes(tenant_id=tenant_id, domain=domain)
    for row in rows:
        meta = _metadata_of(row)
        if meta.get("fragment_purpose") == "login" and str(meta.get("credential_id") or "") == str(credential_id):
            return row
    return None


async def used_numbers(*, tenant_id: str, domain: str, slug: str) -> set[int]:
    names = [str(row.get("name") or "") for row in await store.list_recipes(tenant_id=tenant_id, domain=domain)]
    normalized = store.normalize_domain(domain)
    pending = await registration.list_registrations(tenant_id=tenant_id, status="pending")
    names.extend(str(row.get("name") or "") for row in pending if str(row.get("domain") or "") == normalized)
    return {nn for nn in (_nn_of(name, slug) for name in names) if nn is not None}


async def has_human_approval(*, tenant_id: str, domain: str) -> bool:
    normalized = store.normalize_domain(domain)
    approved = await registration.list_registrations(tenant_id=tenant_id, status="approved")
    return any(
        str(row.get("domain") or "") == normalized
        and not str(row.get("decided_by") or "").startswith(_NON_HUMAN_DECIDERS)
        for row in approved
    )


async def maybe_auto_approve(pending: Mapping[str, Any], *, tenant_id: str, domain: str) -> dict[str, Any] | None:
    """D1 조건을 모두 만족할 때만 승인한다. 아니면 None(사람 승인)."""
    if not svc.auto_approve_read_enabled():
        return None
    dry_run = pending.get("dry_run") if isinstance(pending.get("dry_run"), Mapping) else {}
    if str(dry_run.get("max_risk") or "") != "READ":
        return None
    if not await has_human_approval(tenant_id=tenant_id, domain=domain):
        return None
    return await registration.decide_registration(
        pending["id"], tenant_id=tenant_id, decision="approve", decided_by=POLICY_DECIDER,
        reason="read_only_same_domain_history",
    )


# ── 등록 ───────────────────────────────────────────────────────────────────

async def register_fragments(
    *,
    tenant_id: str,
    owner_user: str,
    draft_id: str,
    domain: str,
    title: str,
    credential_id: str | None,
    recorded: Sequence[Mapping[str, Any]],
    evidence: Mapping[str, Any],
    session_id: str | None = None,
) -> dict[str, Any]:
    """조각을 나누고 새 조각만 registration 으로 올린다. 반환: 이름·등록 id·승인 여부·최고 등급."""
    if not owner_user:
        raise ValueError("owner_user_required")
    fragments = split_steps(recorded)
    if not fragments:
        raise ValueError("no_recorded_steps")
    slug = domain_slug(domain)
    reusable = await find_reusable_login(tenant_id=tenant_id, domain=domain, credential_id=credential_id)
    numbers = await used_numbers(tenant_id=tenant_id, domain=domain, slug=slug)
    assign_names(fragments, slug=slug, used_numbers=numbers, reusable_login=(reusable or {}).get("name"))

    screen = {
        "screen_verified": bool(evidence.get("screen_verified")),
        "final_screenshot_url": str(evidence.get("final_screenshot_url") or ""),
    }
    names: list[str] = []
    registration_ids: list[str] = []
    pending_count = 0
    max_rank = 0
    for fragment in fragments:
        requires = list(names)
        names.append(fragment.name)
        if fragment.reused:
            max_rank = max(max_rank, risk_rank(str((reusable or {}).get("max_risk") or "READ")))
            continue
        meta: dict[str, Any] = {
            "title": title,
            "automation_draft_id": str(draft_id),
            "source_chat_session_id": str(session_id or ""),
            "screen_e2e": screen,
        }
        if fragment.purpose == "login" and credential_id:
            meta["credential_id"] = str(credential_id)  # id 참조만. 값은 Vault 에 있다.
        recipe = build_recipe(fragment, domain=domain, requires=requires, metadata=meta)
        max_rank = max(max_rank, risk_rank(recipe.max_risk()))
        pending = await registration.request_registration(recipe, tenant_id=tenant_id, requested_by=owner_user)
        registration_ids.append(str(pending["id"]))
        decided = await maybe_auto_approve(pending, tenant_id=tenant_id, domain=domain)
        if decided is None:
            pending_count += 1
            await approval_notifier.notify_pending_created(pending)
    return {
        "recipe_names": names,
        "registration_ids": registration_ids,
        "reused": [f.name for f in fragments if f.reused],
        "all_approved": pending_count == 0,
        "max_risk": RISK_LEVELS[max_rank],
    }
