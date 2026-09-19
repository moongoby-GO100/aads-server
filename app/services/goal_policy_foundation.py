"""W-14F fail-closed policy decision and execution boundary.

The evaluator owns policy interpretation and the append-only ledger write.  The
executor accepts only a ledger-backed, authenticated envelope and re-reads all
mutable epochs immediately before execution.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import uuid4

from app.core.goal_work_hierarchy_policy import (
    SENSITIVE_KEYS,
    action_requires_mandatory_human,
)

CANONICALIZATION_VERSION = "RFC8785"
HASH_ALGORITHM = "SHA-256"
SIGNATURE_ALGORITHM = "HMAC-SHA-256"
_PATCH_OPS = frozenset({"add", "remove", "replace", "move", "copy", "test"})
_RISK_ORDER = {"A0": 0, "A1": 1, "A2": 2, "A3": 3}


class PolicyContractError(ValueError):
    """A stable public policy error carrying its HTTP/error contract."""

    def __init__(self, status_code: int, code: str) -> None:
        super().__init__(code)
        self.status_code = status_code
        self.code = code


class BoundaryDecision(StrEnum):
    ALLOW = "ALLOW"
    DENY = "DENY"


class ApprovalRoute(StrEnum):
    NONE = "NONE"
    NOTIFY = "NOTIFY"
    PROJECT_APPROVAL = "PROJECT_APPROVAL"
    CEO_APPROVAL = "CEO_APPROVAL"
    INDEPENDENT_REVIEW = "INDEPENDENT_REVIEW"
    PROJECT_AND_INDEPENDENT = "PROJECT_AND_INDEPENDENT"
    CEO_AND_INDEPENDENT = "CEO_AND_INDEPENDENT"


class AutomationEligibility(StrEnum):
    BASELINE_AUTO = "BASELINE_AUTO"
    GRANT_REQUIRED = "GRANT_REQUIRED"
    MANDATORY_HUMAN = "MANDATORY_HUMAN"
    NOT_EXECUTABLE = "NOT_EXECUTABLE"


class ApplicationResult(StrEnum):
    AUTO = "AUTO"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    DENY = "DENY"
    NOT_EXECUTABLE = "NOT_EXECUTABLE"


def _reject_constant(value: str) -> None:
    raise PolicyContractError(422, "invalid_patch")


def _pairs_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise PolicyContractError(422, "invalid_patch")
        value[key] = item
    return value


def parse_patch_json(raw: str | bytes) -> list[dict[str, Any]]:
    """Parse JSON without losing duplicate-property or malformed-number evidence."""
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_pairs_without_duplicates,
            parse_constant=_reject_constant,
            parse_float=Decimal,
            parse_int=Decimal,
        )
    except PolicyContractError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise PolicyContractError(422, "invalid_patch") from exc
    return validate_patch(value)


def _valid_pointer(value: Any) -> bool:
    if not isinstance(value, str) or (value and not value.startswith("/")):
        return False
    return not re.search(r"~(?![01])", value)


def validate_patch(value: Any) -> list[dict[str, Any]]:
    """Validate RFC 6902 shape while retaining operation array order."""
    if not isinstance(value, list):
        raise PolicyContractError(422, "invalid_patch")
    for operation in value:
        if not isinstance(operation, dict) or operation.get("op") not in _PATCH_OPS:
            raise PolicyContractError(422, "invalid_patch")
        if not _valid_pointer(operation.get("path")):
            raise PolicyContractError(422, "invalid_patch")
        op = operation["op"]
        allowed = {"op", "path", "value"} if op in {"add", "replace", "test"} else {"op", "path"}
        if op in {"move", "copy"}:
            allowed.add("from")
            if not _valid_pointer(operation.get("from")):
                raise PolicyContractError(422, "invalid_patch")
        if set(operation) - allowed:
            raise PolicyContractError(422, "invalid_patch")
        if (op in {"add", "replace", "test"}) != ("value" in operation):
            raise PolicyContractError(422, "invalid_patch")
        _assert_json_numbers(operation)
    return value


def _assert_json_numbers(value: Any) -> None:
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return
    if isinstance(value, (float, Decimal)) and not math.isfinite(float(value)):
        raise PolicyContractError(422, "invalid_patch")
    if isinstance(value, (int, float, Decimal)):
        return
    if isinstance(value, list):
        for item in value:
            _assert_json_numbers(item)
        return
    if isinstance(value, dict):
        for item in value.values():
            _assert_json_numbers(item)
        return
    raise PolicyContractError(422, "invalid_patch")


def _canonical_number(value: float | Decimal) -> str:
    if isinstance(value, bool):
        raise TypeError("boolean is not a number here")
    # RFC 8785 delegates number serialization to ECMAScript/IEEE-754.  Parse
    # through binary64 first so values such as 333333333.33333329 are rendered
    # with the same shortest round-trippable representation as JSON.stringify.
    try:
        binary64 = float(value)
    except (OverflowError, ValueError) as exc:
        raise PolicyContractError(422, "invalid_patch") from exc
    if not math.isfinite(binary64):
        raise PolicyContractError(422, "invalid_patch")
    decimal = Decimal(str(binary64))
    if decimal == 0:
        return "0"
    # JCS uses ECMAScript's shortest representation.  This normal form covers
    # JSON/DB policy values and applies the RFC thresholds for exponent form.
    adjusted = decimal.adjusted()
    if -6 <= adjusted < 21:
        rendered = format(decimal, "f")
        if "." in rendered:
            rendered = rendered.rstrip("0").rstrip(".")
        return rendered or "0"
    normalized = decimal.normalize()
    coefficient = format(normalized.scaleb(-normalized.adjusted()), "f").rstrip("0").rstrip(".")
    exponent = normalized.adjusted()
    return f"{coefficient}e{'+' if exponent >= 0 else ''}{exponent}"


def canonicalize(value: Any, *, version: str = CANONICALIZATION_VERSION) -> bytes:
    """Return UTF-8 JCS bytes; unsupported versions always fail closed."""
    if version != CANONICALIZATION_VERSION:
        raise PolicyContractError(422, "unsupported_canonicalization_version")
    if value is None:
        return b"null"
    if value is True:
        return b"true"
    if value is False:
        return b"false"
    if isinstance(value, str):
        if any(0xD800 <= ord(char) <= 0xDFFF for char in value):
            raise PolicyContractError(422, "invalid_patch")
        return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
    if isinstance(value, (int, float, Decimal)):
        return _canonical_number(value).encode()
    if isinstance(value, list):
        return b"[" + b",".join(canonicalize(item, version=version) for item in value) + b"]"
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise PolicyContractError(422, "invalid_patch")
        members = []
        for key in sorted(value, key=lambda item: item.encode("utf-16-be", "surrogatepass")):
            members.append(canonicalize(key, version=version) + b":" + canonicalize(value[key], version=version))
        return b"{" + b",".join(members) + b"}"
    raise PolicyContractError(422, "invalid_patch")


def canonical_hash(value: Any, *, version: str = CANONICALIZATION_VERSION) -> str:
    return "sha256:" + hashlib.sha256(canonicalize(value, version=version)).hexdigest()


def patch_hash(patch: Sequence[Mapping[str, Any]], *, version: str = CANONICALIZATION_VERSION) -> str:
    validate_patch(patch)
    return canonical_hash(patch, version=version)


def _pointer(parent: str, key: str | int) -> str:
    token = str(key).replace("~", "~0").replace("/", "~1")
    return f"{parent}/{token}"


def sanitize_context(value: Any) -> tuple[Any, list[str], list[str]]:
    """Remove secret values and retain JSON Pointer metadata only."""
    erased: list[str] = []
    masked: list[str] = []

    def visit(item: Any, path: str, key: str = "") -> Any:
        normalized = key.lower().replace("-", "_")
        secret = normalized in SENSITIVE_KEYS or any(part in normalized for part in ("password", "secret", "token"))
        if secret:
            erased.append(path)
            return None
        if isinstance(item, Mapping):
            return {str(k): visit(v, _pointer(path, str(k)), str(k)) for k, v in item.items()}
        if isinstance(item, list):
            return [visit(v, _pointer(path, index)) for index, v in enumerate(item)]
        return item

    return visit(value, ""), erased, masked


def _sensitive_patch_pointers(patch: Sequence[Mapping[str, Any]]) -> list[str]:
    pointers: list[str] = []
    for operation in patch:
        path = operation.get("path")
        if not isinstance(path, str):
            continue
        tokens = [token.replace("~1", "/").replace("~0", "~") for token in path.split("/")[1:]]
        if any(
            token.lower().replace("-", "_") in SENSITIVE_KEYS
            or any(part in token.lower() for part in ("password", "secret", "token"))
            for token in tokens
        ):
            pointers.append(path)
    return pointers


@dataclass(frozen=True)
class SigningKey:
    key_id: str
    version: int
    secret: bytes

    def __post_init__(self) -> None:
        if not self.key_id or self.version < 1 or len(self.secret) < 32:
            raise ValueError("invalid decision signing key")


def _signature_payload(envelope: Mapping[str, Any]) -> dict[str, Any]:
    signed_fields = (
        "decision_id", "tenant_id", "project", "principal_session_id",
        "assignment_id", "target_type", "target_id", "action",
        "decision_input_hash", "patch_hash", "precondition_snapshot_hash",
        "effective_application_result", "policy_version", "target_version",
        "kill_switch_epoch", "deny_policy_epoch", "assignment_epoch",
        "grant_revocation_epoch", "ancestor_revocation_epoch", "decided_at",
        "canonicalization_version", "hash_algorithm", "signature_key_id",
        "signature_key_version",
    )
    return {name: envelope[name] for name in signed_fields}


def sign_envelope(envelope: Mapping[str, Any], key: SigningKey) -> str:
    digest = hmac.new(key.secret, canonicalize(_signature_payload(envelope)), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def verify_envelope_signature(envelope: Mapping[str, Any], key: SigningKey) -> bool:
    if envelope.get("signature_algorithm") != SIGNATURE_ALGORITHM:
        return False
    if envelope.get("signature_key_id") != key.key_id or envelope.get("signature_key_version") != key.version:
        return False
    expected = sign_envelope(envelope, key)
    return hmac.compare_digest(str(envelope.get("signature", "")), expected)


def derive_result(boundary: str, eligibility: str, *, grant_matched: bool) -> ApplicationResult:
    if boundary == BoundaryDecision.DENY:
        return ApplicationResult.DENY
    if eligibility == AutomationEligibility.NOT_EXECUTABLE:
        return ApplicationResult.NOT_EXECUTABLE
    if eligibility == AutomationEligibility.BASELINE_AUTO:
        return ApplicationResult.AUTO
    if eligibility == AutomationEligibility.GRANT_REQUIRED and grant_matched:
        return ApplicationResult.AUTO
    return ApplicationResult.APPROVAL_REQUIRED


@dataclass(frozen=True)
class EvaluationRequest:
    tenant_id: str
    project: str
    workspace_kind: str
    principal_session_id: str
    assignment_id: str
    target_type: str
    target_id: str
    action: str
    base_version: int
    patch: Sequence[Mapping[str, Any]]
    environment: str
    risk_factors: Sequence[str]
    policy_version: str
    precondition_snapshot_hash: str
    target_version: int
    risk_tier: str
    approval_route: str
    boundary_decision: str = "ALLOW"
    automation_eligibility: str = "GRANT_REQUIRED"
    grant_id: str | None = None
    grant_version: int | None = None
    remaining_uses: int | None = None
    trace_id: str | None = None
    span_id: str | None = None
    correlation_id: str | None = None


def _decision_input(request: EvaluationRequest, computed_patch_hash: str) -> dict[str, Any]:
    return {
        "tenant_id": request.tenant_id, "project": request.project,
        "workspace_kind": request.workspace_kind,
        "principal_session_id": request.principal_session_id,
        "assignment_id": request.assignment_id, "target_type": request.target_type,
        "target_id": request.target_id, "action": request.action,
        "base_version": request.base_version, "patch_hash": computed_patch_hash,
        "environment": request.environment, "risk_factors": list(request.risk_factors),
        "precondition_snapshot_hash": request.precondition_snapshot_hash,
        "policy_version": request.policy_version, "grant_id": request.grant_id,
        "grant_version": request.grant_version,
    }


async def evaluate_and_persist(
    conn: Any, request: EvaluationRequest, *, signing_key: SigningKey,
    original_engine_result: str, error_policy_ids: Sequence[str] = (),
    error_kinds: Sequence[str] = (), entity_resolution_status: str = "resolved",
    reason_codes: Sequence[str] = (), mutable_epochs: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    """Evaluate and synchronously append the decision; any write error escapes."""
    if request.workspace_kind not in {"project", "ceo"} or request.risk_tier not in _RISK_ORDER:
        raise PolicyContractError(422, "invalid_policy_input")
    computed_patch_hash = patch_hash(request.patch)
    boundary = BoundaryDecision(request.boundary_decision)
    eligibility = AutomationEligibility(request.automation_eligibility)
    route = ApprovalRoute(request.approval_route)
    reasons = list(dict.fromkeys(reason_codes))
    if entity_resolution_status in {"partial", "failed"}:
        boundary, eligibility, route = BoundaryDecision.DENY, AutomationEligibility.NOT_EXECUTABLE, ApprovalRoute.NONE
        reasons.append("entity_resolution_failed")
    elif error_policy_ids or error_kinds:
        reasons.append("policy_evaluation_error")
        if eligibility != AutomationEligibility.MANDATORY_HUMAN:
            eligibility = AutomationEligibility.MANDATORY_HUMAN
    if request.risk_tier == "A3" or action_requires_mandatory_human(
        request.action, request.environment, request.risk_factors
    ):
        eligibility = AutomationEligibility.MANDATORY_HUMAN
        route = ApprovalRoute.CEO_APPROVAL
        reasons.append("mandatory_human")
    grant_matched = request.grant_id is not None and request.grant_version is not None
    result = derive_result(boundary, eligibility, grant_matched=grant_matched)
    if (error_policy_ids or error_kinds or entity_resolution_status != "resolved") and result == ApplicationResult.AUTO:
        result = ApplicationResult.APPROVAL_REQUIRED
    decided_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    epochs = {name: int((mutable_epochs or {}).get(name, 0)) for name in (
        "kill_switch_epoch", "deny_policy_epoch", "assignment_epoch",
        "grant_revocation_epoch", "ancestor_revocation_epoch",
    )}
    decision_id = str(uuid4())
    input_hash = canonical_hash(_decision_input(request, computed_patch_hash))
    envelope: dict[str, Any] = {
        "decision_id": decision_id, "boundary_decision": boundary.value,
        "tenant_id": request.tenant_id, "project": request.project,
        "workspace_kind": request.workspace_kind,
        "principal_session_id": request.principal_session_id,
        "assignment_id": request.assignment_id,
        "target_type": request.target_type, "target_id": request.target_id,
        "action": request.action, "base_version": request.base_version,
        "environment": request.environment, "target_version": request.target_version,
        "approval_route": route.value, "automation_eligibility": eligibility.value,
        "risk_tier": request.risk_tier, "result": result.value,
        "reason_codes": reasons, "policy_version": request.policy_version,
        "matched_grant_id": request.grant_id, "grant_version": request.grant_version,
        "remaining_uses": request.remaining_uses, "decision_input_hash": input_hash,
        "patch_hash": computed_patch_hash,
        "precondition_snapshot_hash": request.precondition_snapshot_hash,
        "original_engine_result": original_engine_result,
        "effective_application_result": result.value,
        "error_policy_ids": list(error_policy_ids), "error_kinds": list(error_kinds),
        "entity_resolution_status": entity_resolution_status, "fallback_route": None,
        "erased": [], "masked": [], "masking_policy_version": 1,
        "trace_id": request.trace_id, "span_id": request.span_id,
        "correlation_id": request.correlation_id, "decided_at": decided_at,
        "canonicalization_version": CANONICALIZATION_VERSION,
        "hash_algorithm": HASH_ALGORITHM, "signature_algorithm": SIGNATURE_ALGORITHM,
        "signature_key_id": signing_key.key_id, "signature_key_version": signing_key.version,
        **epochs,
    }
    envelope["signature"] = sign_envelope(envelope, signing_key)
    diagnostics, erased, masked = sanitize_context({
        "patch": list(request.patch),
        "error_policy_ids": list(error_policy_ids), "error_kinds": list(error_kinds)
    })
    # Diagnostics never retain the patch body.  The sanitizer traversal above
    # is solely used to derive auditable JSON Pointer metadata.
    diagnostics.pop("patch", None)
    erased = list(dict.fromkeys([*erased, *_sensitive_patch_pointers(request.patch)]))
    envelope["erased"], envelope["masked"] = erased, masked
    # No AUTO is observable unless this awaited append succeeds.  Callers must
    # keep this inside their mutation/reservation transaction.
    await conn.execute(
        """INSERT INTO goal_policy_decisions
          (id,tenant_id,project,workspace_kind,principal_session_id,assignment_id,
           target_type,target_id,action,base_version,environment,boundary_decision,
           approval_route,automation_eligibility,risk_tier,result,reason_codes,
           policy_version,matched_grant_id,grant_version,decision_input_hash,patch_hash,
           precondition_snapshot_hash,canonicalization_version,hash_algorithm,diagnostics,
           original_engine_result,effective_application_result,erased,masked,
           masking_policy_version,kill_switch_epoch,deny_policy_epoch,assignment_epoch,
           grant_revocation_epoch,ancestor_revocation_epoch,target_version,trace_id,span_id,
           correlation_id,signature_algorithm,signature_key_id,signature_key_version,
           signature,decided_at)
         VALUES($1::uuid,$2::uuid,$3,$4,$5::uuid,$6::uuid,$7,$8::uuid,$9,$10,$11,$12,
                $13,$14,$15,$16,$17::text[],$18::uuid,$19::uuid,$20,$21,$22,$23,$24,$25,
                $26::jsonb,$27,$28,$29::jsonb,$30::jsonb,$31,$32,$33,$34,$35,$36,$37,
                $38,$39,$40,$41,$42,$43,$44,$45::timestamptz)""",
        decision_id, request.tenant_id, request.project, request.workspace_kind,
        request.principal_session_id, request.assignment_id, request.target_type,
        request.target_id, request.action, request.base_version, request.environment,
        boundary.value, route.value, eligibility.value, request.risk_tier, result.value,
        reasons, request.policy_version, request.grant_id, request.grant_version,
        input_hash, computed_patch_hash, request.precondition_snapshot_hash,
        CANONICALIZATION_VERSION, HASH_ALGORITHM, json.dumps(diagnostics),
        original_engine_result, result.value, json.dumps(erased), json.dumps(masked), 1,
        epochs["kill_switch_epoch"], epochs["deny_policy_epoch"], epochs["assignment_epoch"],
        epochs["grant_revocation_epoch"], epochs["ancestor_revocation_epoch"],
        request.target_version, request.trace_id, request.span_id, request.correlation_id,
        SIGNATURE_ALGORITHM, signing_key.key_id, signing_key.version,
        envelope["signature"], decided_at,
    )
    return envelope


async def verify_immediately_before_execution(
    conn: Any, envelope: Mapping[str, Any], *, signing_key: SigningKey,
    expected_input: Mapping[str, Any], accepted_results: Sequence[str] = (ApplicationResult.AUTO,),
) -> tuple[bool, str | None]:
    """Re-read every mutable fence and authenticate the decision at execution time."""
    if envelope.get("canonicalization_version") != CANONICALIZATION_VERSION:
        return False, "unsupported_canonicalization_version"
    if envelope.get("hash_algorithm") != HASH_ALGORITHM or not verify_envelope_signature(envelope, signing_key):
        return False, "invalid_decision_signature"
    if envelope.get("effective_application_result") not in accepted_results:
        return False, "decision_not_auto"
    if canonical_hash(expected_input) != envelope.get("decision_input_hash"):
        return False, "decision_input_mismatch"
    identity_fields = (
        "tenant_id", "project", "principal_session_id", "assignment_id",
        "target_type", "target_id", "action", "base_version", "environment",
    )
    for name in identity_fields:
        if envelope.get(name) != expected_input.get(name):
            return False, "decision_identity_mismatch"
    state = await conn.fetchrow(
        """SELECT target_version,policy_version::text,precondition_snapshot_hash,
                  kill_switch_epoch,deny_policy_epoch,assignment_epoch,
                  grant_revocation_epoch,ancestor_revocation_epoch,
                  kill_switch_active,policy_active,assignment_active,grant_chain_active
             FROM goal_policy_execution_fences($1::uuid,$2::uuid,$3::uuid,$4::uuid)""",
        envelope["decision_id"], expected_input["tenant_id"],
        expected_input["target_id"], expected_input["assignment_id"],
    )
    if not state:
        return False, "entity_resolution_failed"
    if state.get("kill_switch_active"):
        return False, "kill_switch_active"
    if not state.get("policy_active", False):
        return False, "stale_policy"
    if not state.get("assignment_active", False):
        return False, "stale_assignment"
    if not state.get("grant_chain_active", False):
        return False, "stale_grant_chain"
    comparisons = {
        "target_version": envelope.get("target_version"),
        "policy_version": envelope.get("policy_version"),
        "precondition_snapshot_hash": envelope.get("precondition_snapshot_hash"),
        "kill_switch_epoch": envelope.get("kill_switch_epoch"),
        "deny_policy_epoch": envelope.get("deny_policy_epoch"),
        "assignment_epoch": envelope.get("assignment_epoch"),
        "grant_revocation_epoch": envelope.get("grant_revocation_epoch"),
        "ancestor_revocation_epoch": envelope.get("ancestor_revocation_epoch"),
    }
    for name, expected in comparisons.items():
        if state[name] != expected:
            return False, f"stale_{name}"
    return True, None


def enforce_budget_overrun(grant: Mapping[str, Any], actual: Mapping[str, Any]) -> dict[str, Any]:
    """T58: any measured overrun stales the grant and blocks new execution."""
    limits = {"files": "max_files", "rows": "max_rows", "cost_usd": "max_cost_usd"}
    overruns = [name for name, limit in limits.items() if Decimal(str(actual.get(name, 0))) > Decimal(str(grant.get(limit, 0)))]
    return {"overrun": bool(overruns), "overrun_dimensions": overruns,
            "grant_status": "stale" if overruns else str(grant.get("status", "active")),
            "allow_new_execution": not overruns}


async def reserve_single_grant(
    conn: Any, *, tenant_id: str, project: str, execution_key: str,
    decision_id: str, grant_id: str, grant_version: int,
) -> Mapping[str, Any]:
    """Atomically reserve one grant; never composes grants or uses SKIP LOCKED."""
    existing = await conn.fetchrow(
        """SELECT id::text,grant_id::text,grant_version,reservation_state
             FROM goal_auto_approval_use_reservations
            WHERE tenant_id=$1::uuid AND execution_key=$2""",
        tenant_id, execution_key,
    )
    if existing:
        if str(existing["grant_id"]) != grant_id or int(existing["grant_version"]) != grant_version:
            raise PolicyContractError(409, "idempotency_key_reused")
        return existing
    grant = await conn.fetchrow(
        """UPDATE goal_auto_approval_grants
              SET used_executions=used_executions+1,last_used_at=clock_timestamp()
            WHERE tenant_id=$1::uuid AND project=$2 AND id=$3::uuid
              AND grant_version=$4 AND status='active'
              AND transaction_timestamp() BETWEEN valid_from AND expires_at
              AND used_executions < max_executions
          RETURNING id::text,grant_version,max_executions-used_executions AS remaining_uses""",
        tenant_id, project, grant_id, grant_version,
    )
    if not grant:
        raise PolicyContractError(422, "grant_unavailable")
    return await conn.fetchrow(
        """INSERT INTO goal_auto_approval_use_reservations
              (tenant_id,project,execution_key,decision_id,grant_id,grant_version,reservation_state)
            VALUES($1::uuid,$2,$3,$4::uuid,$5::uuid,$6,'reserved')
            RETURNING id::text,grant_id::text,grant_version,reservation_state""",
        tenant_id, project, execution_key, decision_id, grant_id, grant_version,
    )
