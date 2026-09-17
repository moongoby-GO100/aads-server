"""Vault 자격증명의 도메인 × 레시피 사용 범위 검사 (FR-7)."""
from __future__ import annotations

from typing import Any, Mapping

from app.services.work_recipe import audit
from app.services.work_recipe.store import normalize_domain


class CredentialScopeViolation(RuntimeError):
    """자격증명이 선언된 범위를 벗어나 사용되려 할 때 발생한다."""


def _field(source: Any, name: str, default: Any = None) -> Any:
    if isinstance(source, Mapping):
        return source.get(name, default)
    return getattr(source, name, default)


def _scope(credential_ref: Any) -> Mapping[str, Any]:
    metadata = _field(credential_ref, "metadata", {})
    return metadata if isinstance(metadata, Mapping) else {}


def _declared_values(credential_ref: Any, plural: str, singular: str) -> set[str]:
    metadata = _scope(credential_ref)
    raw = _field(credential_ref, plural, None)
    if raw is None:
        raw = metadata.get(plural, metadata.get(singular))
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple, set, frozenset)):
        return set()
    return {str(value).strip() for value in raw if str(value).strip()}


async def assert_credential_allowed(
    credential_ref: Any, domain: str, recipe_name: str
) -> None:
    """범위를 검사한 뒤 실제 사용 감사 행을 남긴다; 불일치는 즉시 차단한다."""
    requested_domain = normalize_domain(domain)
    requested_recipe = str(recipe_name or "").strip()
    allowed_domains = {
        normalize_domain(value)
        for value in _declared_values(credential_ref, "allowed_domains", "domain")
    }
    allowed_recipes = _declared_values(
        credential_ref, "allowed_recipes", "recipe_name"
    )

    if not requested_domain or requested_domain not in allowed_domains:
        raise CredentialScopeViolation(
            f"credential domain scope violation: {requested_domain or '<empty>'}"
        )
    if not requested_recipe or requested_recipe not in allowed_recipes:
        raise CredentialScopeViolation(
            f"credential recipe scope violation: {requested_recipe or '<empty>'}"
        )

    reference = (
        _field(credential_ref, "credential_ref", None)
        or _field(credential_ref, "id", None)
        or _field(credential_ref, "name", None)
        or "vault-credential"
    )
    await audit.record_credential_use(
        _field(credential_ref, "run_id", None),
        requested_domain,
        requested_recipe,
        reference,
    )
