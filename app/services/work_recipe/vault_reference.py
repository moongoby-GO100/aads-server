"""승인 레시피의 서버측 Vault 참조 해석.

레시피 실행 입력은 평문 값이 아니라 ``{credential_id, field}`` 참조다. 어느 입력이 어느
자격증명의 어느 필드인지는 **승인된 레시피 판본의 metadata.credential_scope** 가 정한다.
호출자는 같은 참조를 되풀이할 수 있을 뿐 바꿀 수 없다. 값은 이 모듈이 실행 직전에 서버에서
복호화해 메모리에만 둔다.

승인 판본 metadata.credential_scope 계약 (version 1)::

    credential_scope:
      version: 1
      inputs:
        <입력명>: {credential_id: <uuid>, field: username | password}
      allowed_domains: [<레시피 도메인>]
      allowed_recipes: [<레시피 이름>]
      work_key: <선택 — 자격증명의 work_key 와 실행 세션 work_key 가 같아야 한다>

입력명 순서나 이름(credential_1/2)으로 username/password 를 추측하지 않는다.
테넌트는 인증된 서버 컨텍스트에서만 온다.
"""
from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.core import credential_vault
from app.services.work_recipe.schema import WorkRecipe, extract_variables
from app.services.work_recipe.store import normalize_domain

logger = logging.getLogger(__name__)

SCOPE_VERSION = 1
SUPPORTED_FIELDS: frozenset[str] = frozenset({"username", "password"})
_REFERENCE_KEYS = frozenset({"credential_id", "field"})
_SCOPE_KEYS = frozenset({"version", "inputs", "allowed_domains", "allowed_recipes", "work_key"})
_WORK_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$")
_SINGLE_VARIABLE = re.compile(r"^\s*\{\{\s*([A-Za-z_][A-Za-z0-9_.]*)\s*\}\}\s*$")
# credential_vault.get_credential 은 복호화에 실패하면 이 문자열을 값 자리에 넣는다.
_DECRYPT_FAILED = "[복호화 실패]"

SCOPE_CONTRACT_EXAMPLE: dict[str, Any] = {
    "version": SCOPE_VERSION,
    "inputs": {
        "<secret 입력명>": {"credential_id": "<vault credential uuid>", "field": "username|password"},
    },
    "allowed_domains": ["<레시피 domain>"],
    "allowed_recipes": ["<레시피 name>"],
    "work_key": "<선택>",
}

_VAULT_FIX = "after_vault_credential_fixed"
_REREGISTER = "after_recipe_reregistration"
_RESUME: dict[str, str] = {
    "credential_scope_missing": _REREGISTER,
    "credential_scope_invalid": _REREGISTER,
    "credential_binding_missing": _REREGISTER,
    "credential_field_unsupported": _REREGISTER,
    "secret_input_unsupported_use": _REREGISTER,
    "secret_default_forbidden": _REREGISTER,
    "credential_not_found": _VAULT_FIX,
    "credential_inactive": _VAULT_FIX,
    "credential_origin_missing": _VAULT_FIX,
    "credential_field_unavailable": _VAULT_FIX,
    "vault_unavailable": "after_vault_available",
    "vault_lookup_failed": "after_vault_available",
}


class VaultReferenceError(ValueError):
    """참조 해석 실패. 메시지는 코드뿐이다 — 값·평문은 어떤 경로로도 싣지 않는다."""

    def __init__(
        self,
        code: str,
        *,
        input_name: str = "",
        credential_id: str = "",
        field_name: str = "",
    ) -> None:
        super().__init__(code)
        self.code = code
        self.input_name = input_name
        self.credential_id = credential_id
        self.field_name = field_name

    def to_response(self) -> dict[str, Any]:
        recovery: dict[str, Any] = {
            "route": "human_gateway",
            "reason": self.code,
            "resume": _RESUME.get(self.code, "after_recipe_reregistration"),
            "auto_fallback": False,
        }
        if self.code in {"credential_scope_missing", "credential_binding_missing"}:
            recovery["payload_format"] = {"metadata": {"credential_scope": SCOPE_CONTRACT_EXAMPLE}}
        response: dict[str, Any] = {
            "status": "credential_reference_blocked",
            "error": self.code,
            "recovery": recovery,
        }
        if self.input_name:
            response["input"] = self.input_name
        if self.credential_id:
            response["credential_id"] = self.credential_id
        if self.field_name:
            response["field"] = self.field_name
        return response


@dataclass(frozen=True)
class Binding:
    credential_id: str
    field: str


@dataclass(frozen=True)
class VaultScope:
    inputs: dict[str, Binding]
    allowed_domains: frozenset[str]
    allowed_recipes: frozenset[str]
    work_key: str


@dataclass(repr=False)
class VaultResolution:
    """해석 결과. repr 에 값이 나오지 않도록 직접 정의한다."""

    values: dict[str, Any]
    secret_names: frozenset[str]
    fill_steps: frozenset[int]
    uses: list[dict[str, str]] = field(default_factory=list)

    def __repr__(self) -> str:
        return f"VaultResolution(secret_inputs={sorted(self.secret_names)})"


def has_vault_scope(recipe: WorkRecipe) -> bool:
    """승인 판본이 Vault 참조 계약(매핑 형태)을 선언했는가. 문자열 scope 는 기존 별도 계약이다."""
    return isinstance(recipe.metadata.get("credential_scope"), Mapping)


def _uuid_text(value: Any) -> str:
    try:
        return str(uuid.UUID(str(value))).lower()
    except (ValueError, AttributeError, TypeError):
        return ""


def _string_set(raw: Any, *, domains: bool = False) -> frozenset[str]:
    if isinstance(raw, str) or not isinstance(raw, Sequence):
        return frozenset()
    items = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            return frozenset()
        items.append(normalize_domain(item) if domains else item.strip())
    return frozenset(items)


def parse_scope(recipe: WorkRecipe) -> VaultScope:
    """승인 메타데이터를 읽고 구조를 검증한다. 등록 시점과 재생 시점이 같은 코드를 쓴다."""
    raw = recipe.metadata.get("credential_scope")
    if not isinstance(raw, Mapping):
        raise VaultReferenceError("credential_scope_missing")
    if set(raw) - _SCOPE_KEYS or raw.get("version") != SCOPE_VERSION:
        raise VaultReferenceError("credential_scope_invalid")

    raw_inputs = raw.get("inputs")
    if not isinstance(raw_inputs, Mapping) or not raw_inputs:
        raise VaultReferenceError("credential_scope_invalid")
    bindings: dict[str, Binding] = {}
    for name, ref in raw_inputs.items():
        if not isinstance(ref, Mapping) or set(ref) != _REFERENCE_KEYS:
            raise VaultReferenceError("credential_scope_invalid", input_name=str(name))
        credential_id = _uuid_text(ref.get("credential_id"))
        if not credential_id:
            raise VaultReferenceError("credential_scope_invalid", input_name=str(name))
        field_name = ref.get("field")
        if not isinstance(field_name, str) or field_name not in SUPPORTED_FIELDS:
            raise VaultReferenceError(
                "credential_field_unsupported", input_name=str(name), credential_id=credential_id
            )
        bindings[str(name)] = Binding(credential_id, field_name)

    domains = _string_set(raw.get("allowed_domains"), domains=True)
    recipes = _string_set(raw.get("allowed_recipes"))
    if not domains or not recipes:
        raise VaultReferenceError("credential_scope_invalid")
    if normalize_domain(recipe.domain) not in domains or recipe.name not in recipes:
        raise VaultReferenceError("credential_scope_invalid")

    work_key = raw.get("work_key")
    if work_key in (None, ""):
        work_key = ""
    elif not isinstance(work_key, str) or not _WORK_KEY.fullmatch(work_key.strip()):
        raise VaultReferenceError("credential_scope_invalid")
    else:
        work_key = work_key.strip()
    recipe_work_key = str(recipe.metadata.get("work_key") or "").strip()
    if recipe_work_key and recipe_work_key != work_key:
        raise VaultReferenceError("credential_scope_invalid")

    secret_names = {item.name for item in recipe.inputs if item.secret}
    if any(item.secret and item.default is not None for item in recipe.inputs):
        raise VaultReferenceError("secret_default_forbidden")
    for name in sorted(secret_names - set(bindings)):
        raise VaultReferenceError("credential_binding_missing", input_name=name)
    if set(bindings) - secret_names:
        raise VaultReferenceError("credential_scope_invalid")
    return VaultScope(bindings, domains, recipes, work_key)


def secret_fill_steps(recipe: WorkRecipe, secret_names: frozenset[str]) -> frozenset[int]:
    """secret 입력을 쓰는 단계를 찾는다. secret 은 fill 의 value 단독 치환으로만 허용한다."""
    fills: set[int] = set()
    for step in [*recipe.steps, *recipe.verify]:
        used_elsewhere = {
            name
            for attr in ("url", "selector", "wait_for", "endpoint")
            for name in extract_variables(getattr(step, attr))
        }
        if used_elsewhere & secret_names:
            raise VaultReferenceError("secret_input_unsupported_use")
        value_vars = set(extract_variables(step.value))
        if not value_vars & secret_names:
            continue
        match = _SINGLE_VARIABLE.match(step.value) if isinstance(step.value, str) else None
        if step.action != "fill" or match is None or match.group(1) not in secret_names:
            raise VaultReferenceError("secret_input_unsupported_use")
        fills.add(int(step.seq))
    return frozenset(fills)


def _check_caller_reference(name: str, supplied: Any, binding: Binding) -> None:
    """호출자가 참조를 보냈다면 승인된 바인딩과 정확히 같아야 한다. 값·권한 필드는 거부한다."""
    if supplied is None:
        return
    if not isinstance(supplied, Mapping):
        raise VaultReferenceError("plain_secret_rejected", input_name=name)
    if set(supplied) - _REFERENCE_KEYS:
        raise VaultReferenceError("caller_authority_rejected", input_name=name)
    if set(supplied) != _REFERENCE_KEYS:
        raise VaultReferenceError("credential_reference_invalid", input_name=name)
    if (_uuid_text(supplied.get("credential_id")) != binding.credential_id
            or supplied.get("field") != binding.field):
        raise VaultReferenceError(
            "credential_reference_mismatch",
            input_name=name, credential_id=binding.credential_id, field_name=binding.field,
        )


def _assert_credential_usable(
    item: Mapping[str, Any],
    *,
    binding: Binding,
    name: str,
    scope: VaultScope,
    recipe: WorkRecipe,
    tenant_id: str,
    browser_work_key: str | None,
) -> None:
    def blocked(code: str) -> VaultReferenceError:
        return VaultReferenceError(
            code, input_name=name, credential_id=binding.credential_id, field_name=binding.field
        )

    row_tenant = _uuid_text(item.get("tenant_id"))
    if not row_tenant or row_tenant != _uuid_text(tenant_id):
        raise blocked("credential_not_found")
    # agent_vault 행은 조회 SQL 이 is_active 로 거른다. e2e 행은 컬럼을 직접 확인한다.
    if item.get("vault_source") != "agent_vault" and item.get("is_active") is not True:
        raise blocked("credential_inactive")
    host = normalize_domain(str(item.get("origin") or item.get("url") or ""))
    if not host:
        raise blocked("credential_origin_missing")
    recipe_host = normalize_domain(recipe.domain)
    if host != recipe_host or host not in scope.allowed_domains:
        raise blocked("credential_origin_mismatch")

    metadata = item.get("metadata") if isinstance(item.get("metadata"), Mapping) else {}
    narrowed_recipes = _string_set(metadata.get("allowed_recipes"))
    if metadata.get("allowed_recipes") not in (None, []) and recipe.name not in narrowed_recipes:
        raise blocked("credential_recipe_not_allowed")
    narrowed_domains = _string_set(metadata.get("allowed_domains"), domains=True)
    if metadata.get("allowed_domains") not in (None, []) and recipe_host not in narrowed_domains:
        raise blocked("credential_origin_mismatch")

    credential_work_key = str(item.get("work_key") or "").strip()
    if credential_work_key != scope.work_key:
        raise blocked("credential_work_key_mismatch")
    if scope.work_key and str(browser_work_key or "").strip() != scope.work_key:
        raise blocked("work_key_mismatch")


async def resolve_inputs(
    recipe: WorkRecipe,
    inputs: Mapping[str, Any],
    *,
    tenant_id: Any,
    browser_work_key: str | None = None,
) -> VaultResolution:
    """secret 입력을 승인 바인딩대로 Vault 에서 읽는다. 하나라도 어긋나면 어떤 값도 돌려주지 않는다."""
    scope: VaultScope | None = None
    name = ""
    binding: Binding | None = None
    try:
        scope = parse_scope(recipe)
        secret_names = frozenset(scope.inputs)
        fills = secret_fill_steps(recipe, secret_names)
        for name, binding in scope.inputs.items():
            _check_caller_reference(name, inputs.get(name), binding)
        if not tenant_id:
            raise VaultReferenceError("tenant_context_required")

        fetched: dict[str, Mapping[str, Any]] = {}
        resolved = {key: value for key, value in inputs.items() if key not in secret_names}
        uses: list[dict[str, str]] = []
        try:
            for name, binding in scope.inputs.items():
                if binding.credential_id not in fetched:
                    item = await credential_vault.get_credential(
                        binding.credential_id,
                        include_secrets=True,
                        tenant_id=str(tenant_id),
                        require_active=True,
                    )
                    if not item:
                        raise VaultReferenceError(
                            "credential_not_found", input_name=name,
                            credential_id=binding.credential_id, field_name=binding.field,
                        )
                    fetched[binding.credential_id] = item
                item = fetched[binding.credential_id]
                _assert_credential_usable(
                    item, binding=binding, name=name, scope=scope, recipe=recipe,
                    tenant_id=str(tenant_id), browser_work_key=browser_work_key,
                )
                value = item.get(binding.field)
                if not isinstance(value, str) or not value or value == _DECRYPT_FAILED:
                    raise VaultReferenceError(
                        "credential_field_unavailable", input_name=name,
                        credential_id=binding.credential_id, field_name=binding.field,
                    )
                resolved[name] = value
                uses.append({"input": name, "credential_id": binding.credential_id, "field": binding.field})
        except VaultReferenceError:
            raise
        except credential_vault.VaultUnavailableError:
            raise VaultReferenceError(
                "vault_unavailable", input_name=name, credential_id=binding.credential_id if binding else ""
            ) from None
        except (ValueError, TypeError):
            raise VaultReferenceError(
                "credential_reference_invalid", input_name=name,
                credential_id=binding.credential_id if binding else "",
            ) from None
        except Exception as exc:  # noqa: BLE001 - DB/복호화 예외는 형식만 남기고 값·메시지는 버린다
            logger.warning("vault_reference_lookup_failed type=%s", type(exc).__name__)
            raise VaultReferenceError(
                "vault_lookup_failed", input_name=name,
                credential_id=binding.credential_id if binding else "",
            ) from None
        finally:
            fetched.clear()
    except VaultReferenceError as exc:
        logger.warning(
            "vault_reference_blocked recipe=%s code=%s input=%s credential_id=%s field=%s",
            recipe.name, exc.code, exc.input_name, exc.credential_id, exc.field_name,
        )
        raise
    for use in uses:
        logger.info(
            "vault_reference_allowed recipe=%s input=%s credential_id=%s field=%s",
            recipe.name, use["input"], use["credential_id"], use["field"],
        )
    return VaultResolution(resolved, secret_names, fills, uses)
