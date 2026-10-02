"""Recorded WorkRecipe dry-run and B-scope registration approval."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from typing import Any

from app.core.db_pool import get_pool
from app.services.work_recipe import store
from app.services.work_recipe.schema import WorkRecipe, parse_recipe


class RegistrationError(RuntimeError):
    """The registration request is missing or cannot transition."""


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

        spec = _json_object(row["spec"])
        recipe = parse_recipe(spec)
        await _validate_coupangeats_registration(recipe, tenant)
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
