"""Recorded WorkRecipe dry-run and B-scope registration approval."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from typing import Any

from app.core.db_pool import get_pool
from app.services.work_recipe import store
from app.services.work_recipe import shinhan_corporate_manual as shinhan_manual
from app.services.work_recipe.schema import RISK_LEVELS, WorkRecipe, parse_recipe, risk_rank


class RegistrationError(RuntimeError):
    """The registration request is missing or cannot transition."""


class HumanDeciderRequired(RegistrationError):
    """The caller is a service key, internal system user or agent session, not a signed-in person."""


class SelfApprovalBlocked(RegistrationError):
    """The requester tried to approve their own registration."""


class HighRiskConfirmationRequired(RegistrationError):
    """WRITE_EXTERNAL or higher needs an explicit confirm_high_risk=true on approve."""


HIGH_RISK_CONFIRM_FROM = "WRITE_EXTERNAL"

_SERVICE_EMAIL_SUFFIX = "@aads.internal"
_SERVICE_MEMBERSHIP_IDS = frozenset({"internal-pipeline-call", "service-monitor-key"})
_NON_HUMAN_ID_PREFIXES = ("system", "chat:", "session:", "agent", "runner", "service", "pipeline")


def _principal_ids(principal: Mapping[str, Any]) -> tuple[str, str, str]:
    user = principal.get("user") or {}
    membership = principal.get("membership") or {}
    user_id = str(user.get("user_id") or user.get("id") or membership.get("user_id") or "").strip()
    email = str(user.get("email") or "").strip()
    return user_id, email, str(membership.get("id") or "").strip()


def is_human_principal(principal: Mapping[str, Any]) -> bool:
    """True only for a signed-in person (SaaS user JWT, or the env admin login whose sub is 'admin').

    app.auth.get_current_user builds service principals with user_id 'system:*', email
    system@aads.internal and membership ids 'internal-pipeline-call' / 'service-monitor-key';
    both service paths also carry user_role='system', as does the env admin JWT, so the role
    alone cannot separate them — the user id does.
    """
    user = principal.get("user") or {}
    user_id, email, membership_id = _principal_ids(principal)
    if not user_id and not email:
        return False
    if membership_id in _SERVICE_MEMBERSHIP_IDS:
        return False
    lowered_id, lowered_email = user_id.lower(), email.lower()
    if lowered_email.endswith(_SERVICE_EMAIL_SUFFIX):
        return False
    if lowered_id.startswith(_NON_HUMAN_ID_PREFIXES) or "agent" in lowered_id:
        return False
    if str(user.get("user_role") or "").lower() == "system" and lowered_id != "admin":
        return False
    return True


def decider_identity(principal: Mapping[str, Any]) -> str:
    """Authenticated email, else user id — the only value ever stored as decided_by."""
    user_id, email, _ = _principal_ids(principal)
    return email or user_id


def _same_identity(left: Any, right: Any) -> bool:
    a, b = str(left or "").strip().lower(), str(right or "").strip().lower()
    return bool(a) and a == b


def _spec_max_risk(spec: Mapping[str, Any]) -> str:
    """Highest risk over steps+verify of a stored spec; an unknown value counts as the highest."""
    highest = 0
    for key in ("steps", "verify"):
        items = spec.get(key)
        for item in items if isinstance(items, list) else []:
            raw = item.get("risk") if isinstance(item, Mapping) else None
            try:
                highest = max(highest, risk_rank(raw))
            except ValueError:
                highest = len(RISK_LEVELS) - 1
    return RISK_LEVELS[highest]


def requires_high_risk_confirmation(max_risk: str) -> bool:
    try:
        return risk_rank(max_risk) >= risk_rank(HIGH_RISK_CONFIRM_FROM)
    except ValueError:
        return True


def registration_error_status(exc: RegistrationError) -> int:
    if isinstance(exc, (HumanDeciderRequired, SelfApprovalBlocked)):
        return 403
    if str(exc) == "registration_not_found":
        return 404
    return 409


def _tenant_uuid(value: Any) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    return uuid.UUID(str(value))


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        parsed = json.loads(value)
        if isinstance(parsed, Mapping):
            return dict(parsed)
    raise RegistrationError("registration spec is not an object")


_VERIFY_KEYS = frozenset({"risk", "action", "assertion", "expected", "selector", "description", "phase"})
_FLOW_METADATA_KEYS = ("starts_when", "succeeds_when", "lane")


def normalize_verify_steps(raw: Any) -> list[dict[str, Any]]:
    """Validate register_e2e verify items against what GenericVerificationExecutor can run.

    Raises ValueError (never drops silently) so the caller can return it as a registration error.
    """
    from app.services.work_recipe.orchestrator import GENERIC_VERIFY_ASSERTIONS

    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("invalid_verify_spec")
    allowed = ", ".join(sorted(GENERIC_VERIFY_ASSERTIONS))
    result: list[dict[str, Any]] = []
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, Mapping):
            raise ValueError(f"invalid_verify_step:{index}")
        unknown = sorted(set(item) - _VERIFY_KEYS)
        if unknown:
            raise ValueError(f"unsupported_verify_field:{index}:{','.join(map(str, unknown))}")
        if item.get("phase") not in (None, "verify"):
            raise ValueError(f"invalid_verify_step:{index}")
        assertion = item.get("assertion")
        if assertion not in GENERIC_VERIFY_ASSERTIONS:
            raise ValueError(f"unsupported_verify_assertion:{index}:{assertion} (allowed: {allowed})")
        action = str(item.get("action") or "snapshot").strip()
        if action != "snapshot":
            raise ValueError(f"unsupported_verify_action:{index}:{action}")
        risk = str(item.get("risk") or "READ").strip().upper()
        if risk != "READ":
            raise ValueError(f"verify_risk_must_be_read:{index}")
        selector = str(item.get("selector") or "").strip()
        expected = item.get("expected")
        if assertion == "element_visible":
            if not selector:
                raise ValueError(f"verify_selector_required:{index}")
        elif not str(expected or ""):
            raise ValueError(f"verify_expected_required:{index}")
        step: dict[str, Any] = {"risk": risk, "action": action, "assertion": assertion}
        if expected is not None and str(expected) != "":
            step["expected"] = str(expected)
        if selector:
            step["selector"] = selector
        if item.get("description"):
            step["description"] = str(item["description"])
        result.append(step)
    return result


def normalize_flow_metadata(source: Mapping[str, Any]) -> dict[str, str]:
    """Pick starts_when/succeeds_when/lane from a mapping; non-string values are errors."""
    result: dict[str, str] = {}
    for key in _FLOW_METADATA_KEYS:
        value = source.get(key)
        if value is None or value == "":
            continue
        if not isinstance(value, str):
            raise ValueError(f"invalid_{key}")
        result[key] = value.strip()[:1000]
    return result


def build_dry_run(recipe: WorkRecipe, *, proposed_version: int) -> dict[str, Any]:
    """Return the exact, secret-free registration preview shown before approval."""
    return {
        "scope": "recipe_registration",
        "name": recipe.name,
        "domain": store.normalize_domain(recipe.domain),
        "proposed_version": int(proposed_version),
        "max_risk": recipe.max_risk(),
        "permissions": sorted({step.risk for step in [*recipe.steps, *recipe.verify]}),
        "inputs": [
            {"name": item.name, "secret": bool(item.secret), "description": item.description}
            for item in recipe.inputs
        ],
        "steps": [
            {
                "seq": step.seq,
                "phase": "step",
                "action": step.action,
                "risk": step.risk,
                "description": step.description,
                "evidence": "screenshot_or_step_audit",
            }
            for step in recipe.steps
        ]
        + [
            {
                "seq": step.seq,
                "phase": "verify",
                "action": step.action,
                "risk": step.risk,
                "description": step.description,
                "evidence": "verification_step_audit",
            }
            for step in recipe.verify
        ],
    }


async def request_registration(
    recipe: WorkRecipe,
    *,
    tenant_id: Any,
    requested_by: str = "",
) -> dict[str, Any]:
    """Persist a dry-run draft. It is not visible to the recipe player yet."""
    await _validate_coupangeats_registration(recipe, tenant_id)
    tenant = _tenant_uuid(tenant_id)
    domain = store.normalize_domain(recipe.domain)
    proposed_version = await store.next_version(name=recipe.name, domain=domain, tenant_id=tenant)
    if recipe.name in {"coupangeats_01_open_login", "coupangeats_02_vault_fill"}:
        proposed_version = max(proposed_version, 2)
    spec = recipe.to_dict()
    spec["version"] = proposed_version
    canonical = json.dumps(spec, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    spec_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    dry_run = build_dry_run(recipe, proposed_version=proposed_version)

    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO work_recipe_registration_requests (
                tenant_id, name, domain, spec_hash, spec, yaml_source,
                dry_run, requested_by
            ) VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7::jsonb, $8)
            ON CONFLICT (tenant_id, domain, name, spec_hash)
                WHERE status = 'pending'
            DO UPDATE SET updated_at = NOW()
            RETURNING *
            """,
            tenant,
            recipe.name,
            domain,
            spec_hash,
            canonical,
            recipe.to_yaml(),
            json.dumps(dry_run, ensure_ascii=False),
            str(requested_by or ""),
        )
    return _row(row)


async def get_registration(registration_id: Any, *, tenant_id: Any) -> dict[str, Any] | None:
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM work_recipe_registration_requests WHERE id=$1::uuid AND tenant_id=$2",
            str(registration_id),
            _tenant_uuid(tenant_id),
        )
    return _row(row) if row else None


async def list_registrations(
    *, tenant_id: Any, status: str = "pending"
) -> list[dict[str, Any]]:
    """List registration requests visible to one tenant, newest request first."""
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT * FROM work_recipe_registration_requests
             WHERE tenant_id=$1 AND status=$2
             ORDER BY requested_at DESC, id DESC
            """,
            _tenant_uuid(tenant_id),
            str(status or "pending").strip().lower(),
        )
    return [_row(row) for row in rows]


def _object_or_empty(value: Any) -> dict[str, Any]:
    try:
        return _json_object(value)
    except (RegistrationError, ValueError):
        return {}


def registration_summary(row: Mapping[str, Any]) -> dict[str, Any]:
    """Secret-free approval-screen fields derived from a registration row (additive only).

    Step values, fill inputs and descriptions are never copied — only action, URL host,
    selector and risk.
    """
    from urllib.parse import urlsplit

    from app.services.work_recipe.audit import mask_secrets, mask_url

    spec = _object_or_empty(row.get("spec"))
    dry_run = _object_or_empty(row.get("dry_run"))
    metadata = spec.get("metadata") if isinstance(spec.get("metadata"), Mapping) else {}
    evidence = metadata.get("screen_e2e") if isinstance(metadata.get("screen_e2e"), Mapping) else {}
    steps = [s for s in spec.get("steps") or [] if isinstance(s, Mapping)]
    verify = [s for s in spec.get("verify") or [] if isinstance(s, Mapping)]

    def describe(step: Mapping[str, Any], seq: int, phase: str) -> dict[str, Any]:
        try:
            host = urlsplit(str(step.get("url") or "")).hostname or ""
        except ValueError:
            host = ""
        return {
            "seq": step.get("seq") or seq,
            "phase": phase,
            "action": str(step.get("action") or ""),
            "risk": str(step.get("risk") or "READ").upper(),
            "url_host": host,
            "selector": mask_secrets(step.get("selector") or "")[:200],
        }

    evidence_url = next(
        (str(evidence[k]) for k in ("screenshot_url", "snapshot_ref", "url") if evidence.get(k)), ""
    )
    return {
        "domain": str(row.get("domain") or ""),
        "name": str(row.get("name") or ""),
        "version": dry_run.get("proposed_version") or spec.get("version"),
        "max_risk": _spec_max_risk(spec) if spec else str(dry_run.get("max_risk") or "READ"),
        "step_count": len(steps),
        "verify_count": len(verify),
        "requested_by": row.get("requested_by"),
        "requested_at": row.get("requested_at"),
        "screen_evidence_url": mask_url(evidence_url)[:1000],
        "step_summaries": [describe(st, i, "step") for i, st in enumerate(steps, start=1)]
        + [describe(st, len(steps) + i, "verify") for i, st in enumerate(verify, start=1)],
    }


async def decide_registration_as_human(
    registration_id: Any,
    *,
    tenant_id: Any,
    decision: str,
    principal: Mapping[str, Any],
    reason: str = "",
    confirm_high_risk: bool = False,
) -> dict[str, Any]:
    """The only entry for person-initiated decisions (dashboard and chat card).

    decided_by is taken from the authenticated principal, never from a request body.
    """
    if not is_human_principal(principal):
        raise HumanDeciderRequired("human_decider_required")
    decider = decider_identity(principal)
    verdict = str(decision or "").strip().lower()
    if verdict == "approve":
        row = await get_registration(registration_id, tenant_id=tenant_id)
        if row is None:
            raise RegistrationError("registration_not_found")
        user_id, email, _ = _principal_ids(principal)
        if _same_identity(row.get("requested_by"), email) or _same_identity(
            row.get("requested_by"), user_id
        ):
            raise SelfApprovalBlocked("self_approval_blocked")
        spec = _object_or_empty(row.get("spec"))
        max_risk = _spec_max_risk(spec) if spec else str(
            _object_or_empty(row.get("dry_run")).get("max_risk") or "IRREVERSIBLE"
        )
        if requires_high_risk_confirmation(max_risk) and confirm_high_risk is not True:
            raise HighRiskConfirmationRequired("high_risk_confirmation_required")
    return await decide_registration(
        registration_id,
        tenant_id=tenant_id,
        decision=decision,
        decided_by=decider,
        reason=reason,
    )


async def decide_registration(
    registration_id: Any,
    *,
    tenant_id: Any,
    decision: str,
    decided_by: str,
    reason: str = "",
) -> dict[str, Any]:
    """Approve once and create the immutable recipe version in the same transaction."""
    verdict = str(decision or "").strip().lower()
    if verdict not in {"approve", "reject"}:
        raise RegistrationError("decision must be approve or reject")
    tenant = _tenant_uuid(tenant_id)

    async with get_pool().acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            """
                SELECT * FROM work_recipe_registration_requests
                 WHERE id=$1::uuid AND tenant_id=$2
                 FOR UPDATE
                """,
            str(registration_id),
            tenant,
        )
        if row is None:
            raise RegistrationError("registration_not_found")
        if str(row["status"]) != "pending":
            raise RegistrationError(f"registration_already_decided:{row['status']}")

        if verdict == "reject":
            updated = await conn.fetchrow(
                """
                    UPDATE work_recipe_registration_requests
                       SET status='rejected', decided_by=$3, decision_reason=$4,
                           decided_at=NOW(), updated_at=NOW()
                     WHERE id=$1::uuid AND tenant_id=$2
                     RETURNING *
                    """,
                str(registration_id),
                tenant,
                str(decided_by or ""),
                str(reason or ""),
            )
            return _row(updated)

        if _same_identity(row["requested_by"], decided_by):
            raise SelfApprovalBlocked("self_approval_blocked")

        spec = _json_object(row["spec"])
        recipe = parse_recipe(spec)
        await _validate_coupangeats_registration(recipe, tenant)
        try:
            shinhan_manual.validate_manifest(recipe)
        except shinhan_manual.FinancialManualBlocked as exc:
            raise RegistrationError(f"financial_manifest_invalid:{exc.code}") from exc
        lock_key = f"{tenant}:{row['domain']}:{row['name']}"
        await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", lock_key)
        version = int(
            await conn.fetchval(
                """
                SELECT COALESCE(MAX(version), 0) + 1 FROM work_recipes
                 WHERE tenant_id=$1 AND domain=$2 AND name=$3
                """,
                tenant,
                str(row["domain"]),
                str(row["name"]),
            )
        )
        if recipe.name in {"coupangeats_01_open_login", "coupangeats_02_vault_fill"}:
            version = max(version, 2)
        stored_spec = recipe.to_dict()
        stored_spec["version"] = version
        recipe_row = await conn.fetchrow(
            """
                INSERT INTO work_recipes (
                    tenant_id, name, domain, version, description, spec,
                    yaml_source, max_risk, enabled, created_by
                ) VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7,$8,TRUE,$9)
                RETURNING *
                """,
            tenant,
            recipe.name,
            store.normalize_domain(recipe.domain),
            version,
            recipe.description,
            json.dumps(stored_spec, ensure_ascii=False),
            recipe.to_yaml(),
            recipe.max_risk(),
            str(decided_by or ""),
        )
        updated = await conn.fetchrow(
            """
                UPDATE work_recipe_registration_requests
                   SET status='approved', decided_by=$3, decision_reason=$4,
                       recipe_id=$5, decided_at=NOW(), updated_at=NOW()
                 WHERE id=$1::uuid AND tenant_id=$2
                 RETURNING *
                """,
            str(registration_id),
            tenant,
            str(decided_by or ""),
            str(reason or ""),
            recipe_row["id"],
        )
    result = _row(updated)
    result["recipe"] = store.row_to_dict(recipe_row)
    from app.services.ovis_recipe import sync_legacy_reference

    result["recipe"]["ovis_recipe_ref"] = await sync_legacy_reference(
        tenant_id=tenant,
        canonical_key=f"work:{store.normalize_domain(recipe.domain)}:{recipe.name}",
        version=str(version),
        status="active",
        source_type="work_recipe",
        source_id=recipe_row["id"],
        definition=result["recipe"],
        approval_scope={
            "legacy": "work_recipe_registration",
            "registration_id": str(registration_id),
            "requested_by": str(row["requested_by"]),
            "decided_by": str(decided_by or ""),
            "decision": "approved",
        },
    )
    return result


async def _validate_coupangeats_registration(recipe: WorkRecipe, tenant_id: Any) -> None:
    from app.services.browser_recipe_registry import (
        COUPANGEATS_LOGIN_FRAGMENTS, evidence_matches_run,
        lookup_coupangeats_run_evidence,
    )
    if recipe.name not in {part["name"] for part in COUPANGEATS_LOGIN_FRAGMENTS}:
        return
    from app.services.work_recipe.orchestrator import validate_coupangeats_recipe

    validate_coupangeats_recipe(recipe)
    evidence = recipe.metadata["screen_e2e"]
    source_session = str(recipe.metadata.get("source_chat_session_id") or "")
    if not source_session:
        raise RegistrationError("coupangeats_recording_session_required")
    trusted = await lookup_coupangeats_run_evidence(
        tenant_id=str(tenant_id),
        chat_session_id=source_session,
        run_id=evidence.get("run_id"),
        artifact_id=evidence.get("artifact_id"),
    )
    if (not evidence_matches_run(evidence, trusted, fragment_name=recipe.name)
            or trusted["chat_session_id"] != source_session):
        raise RegistrationError("coupangeats_untrusted_screen_evidence")


def _row(row: Any) -> dict[str, Any]:
    data = dict(row)
    for key in ("id", "tenant_id", "recipe_id"):
        if data.get(key) is not None:
            data[key] = str(data[key])
    for key in ("spec", "dry_run"):
        value = data.get(key)
        if isinstance(value, str):
            data[key] = json.loads(value)
    return data
