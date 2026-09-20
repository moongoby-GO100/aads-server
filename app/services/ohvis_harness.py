"""OHVIS harness registry and read APIs.

This module makes the existing OHVIS pieces visible as one product contract:
execution harness, LangGraph runtime, LangChain tool surface,
LangSmith-compatible observability, LLM Wiki, Hermes patterns, and Skill Find.
It is intentionally read-oriented and degrades gracefully when the foundation
tables have not been migrated yet.
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import inspect
import json
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jsonschema import Draft202012Validator, SchemaError, ValidationError

if TYPE_CHECKING:
    from app.services.channel_router import ActionIntent


PROJECTS = ("AADS", "KIS", "GO100", "SF", "NTV2", "NAS", "CEO")

# migration 163 (+ 164 정합화) — internal LangSmith-compatible LLMOps ledger.
# app.services.llmops_store.LLMOPS_TABLES와 같은 집합을 가리킨다.
LLMOPS_TABLES = (
    "llmops_traces",
    "llmops_spans",
    "llmops_tool_calls",
    "llmops_datasets",
    "llmops_examples",
    "llmops_experiments",
    "llmops_scores",
    "llmops_feedback",
)

FOUNDATION_TABLES = (
    "ops_skill_library",
    "ops_skill_versions",
    "ops_skill_runs",
    "ohvis_wiki_sources",
    "ohvis_wiki_pages",
    "ohvis_wiki_links",
    "ohvis_wiki_error_book",
    "ohvis_harness_traces",
) + LLMOPS_TABLES

RISK_POLICIES: dict[str, dict[str, Any]] = {
    "read": {
        "decision": "allow",
        "approval_required": False,
        "examples": ["file_read", "select_query", "health_check", "wiki_search"],
    },
    "write": {
        "decision": "approve",
        "approval_required": True,
        "examples": ["file_patch", "non_destructive_db_upsert", "docs_update"],
    },
    "deploy": {
        "decision": "approve",
        "approval_required": True,
        "examples": ["blue_green_deploy", "nginx_cutover", "standby_sync"],
    },
    "auth": {
        "decision": "respond",
        "approval_required": True,
        "examples": ["captcha", "otp", "account_login", "credential_vault"],
    },
    "financial": {
        "decision": "approve",
        "approval_required": True,
        "examples": ["order_submit", "broker_action", "cash_transfer"],
    },
    "destructive": {
        "decision": "reject",
        "approval_required": True,
        "examples": ["drop_table", "truncate", "force_push", "shutdown"],
    },
}

SKILL_VERSION_STATES = frozenset({"draft", "candidate", "shadow", "active", "deprecated", "quarantined"})
HIGH_RISK_TIERS = frozenset({"write", "deploy", "auth", "financial", "destructive"})
_EXECUTOR_NAME_RE = re.compile(r"^[a-z][a-z0-9_.-]{2,119}$")
SkillExecutor = Callable[[Mapping[str, Any]], Any | Awaitable[Any]]
_SKILL_EXECUTORS: dict[str, SkillExecutor] = {}


class SkillRegistryError(ValueError):
    """Stable, API-safe failure raised by the executable skill registry."""

    def __init__(self, code: str, detail: str = "", *, status_code: int = 422) -> None:
        self.code = code
        self.detail = detail or code
        self.status_code = status_code
        super().__init__(self.detail)


def register_skill_executor(name: str, executor: SkillExecutor) -> None:
    """Register a code-owned callable; DB values can only select from this map.

    Registration is deliberately explicit.  This module never evaluates code or
    imports a module named by a manifest.
    """
    if not _EXECUTOR_NAME_RE.fullmatch(name) or not callable(executor):
        raise ValueError("invalid skill executor registration")
    existing = _SKILL_EXECUTORS.get(name)
    if existing is not None and existing is not executor:
        raise ValueError(f"skill executor already registered: {name}")
    _SKILL_EXECUTORS[name] = executor


def unregister_skill_executor(name: str, *, executor: SkillExecutor | None = None) -> None:
    """Test/plugin cleanup without allowing a caller to replace another owner."""
    existing = _SKILL_EXECUTORS.get(name)
    if existing is not None and (executor is None or existing is executor):
        del _SKILL_EXECUTORS[name]


def _contract_echo_executor(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Side-effect-free executor used for registry/API contract verification."""
    return {"input": dict(payload)}


register_skill_executor("ohvis.contract-echo", _contract_echo_executor)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def validate_skill_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and normalize the immutable executable contract."""
    if not isinstance(manifest, Mapping):
        raise SkillRegistryError("invalid_manifest", "manifest must be an object")
    required = {
        "skill_id", "version", "input_schema", "output_schema", "executor",
        "allowed_tools", "timeout_seconds", "retry", "idempotency",
        "preconditions", "postconditions", "evidence", "risk_tier", "status", "provenance",
    }
    missing = sorted(required - set(manifest))
    if missing:
        raise SkillRegistryError("manifest_fields_missing", ", ".join(missing))
    normalized = dict(manifest)
    if not str(normalized["skill_id"]).strip() or not str(normalized["version"]).strip():
        raise SkillRegistryError("invalid_skill_identity")
    executor = str(normalized["executor"])
    if not _EXECUTOR_NAME_RE.fullmatch(executor):
        raise SkillRegistryError("invalid_executor")
    for key in ("input_schema", "output_schema"):
        schema = normalized[key]
        if not isinstance(schema, Mapping):
            raise SkillRegistryError("invalid_json_schema", f"{key} must be an object")
        try:
            Draft202012Validator.check_schema(schema)
        except SchemaError as exc:
            raise SkillRegistryError("invalid_json_schema", f"{key}: {exc.message}") from exc
    for key in ("allowed_tools", "preconditions", "postconditions", "evidence"):
        if not isinstance(normalized[key], list):
            raise SkillRegistryError("invalid_manifest_field", key)
        if any(not isinstance(v, str) or not v for v in normalized[key]):
            raise SkillRegistryError("invalid_manifest_field", key)
    if normalized["status"] not in SKILL_VERSION_STATES:
        raise SkillRegistryError("invalid_version_status")
    if normalized["risk_tier"] not in RISK_POLICIES:
        raise SkillRegistryError("invalid_risk_tier")
    timeout = normalized["timeout_seconds"]
    if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= 300:
        raise SkillRegistryError("invalid_timeout")
    retry = normalized["retry"]
    if not isinstance(retry, Mapping) or set(retry) - {"max_attempts", "backoff_seconds"}:
        raise SkillRegistryError("invalid_retry_policy")
    attempts = retry.get("max_attempts", 1)
    backoff = retry.get("backoff_seconds", 0)
    if not isinstance(attempts, int) or isinstance(attempts, bool) or not 1 <= attempts <= 5:
        raise SkillRegistryError("invalid_retry_policy")
    if not isinstance(backoff, (int, float)) or isinstance(backoff, bool) or not 0 <= backoff <= 30:
        raise SkillRegistryError("invalid_retry_policy")
    idempotency = normalized["idempotency"]
    idempotency_modes = {"required", "optional", "forbidden"}
    if not isinstance(idempotency, Mapping) or idempotency.get("mode") not in idempotency_modes:
        raise SkillRegistryError("invalid_idempotency_policy")
    if attempts > 1 and idempotency.get("mode") != "required":
        raise SkillRegistryError("retry_requires_idempotency")
    provenance = normalized["provenance"]
    if not isinstance(provenance, Mapping) or not provenance.get("source"):
        raise SkillRegistryError("invalid_provenance")
    normalized["retry"] = {"max_attempts": attempts, "backoff_seconds": float(backoff)}
    normalized["idempotency"] = dict(idempotency)
    normalized["provenance"] = dict(provenance)
    return normalized


def _validate_instance(schema: Mapping[str, Any], value: Any, code: str) -> None:
    try:
        Draft202012Validator(schema).validate(value)
    except ValidationError as exc:
        path = ".".join(str(item) for item in exc.absolute_path) or "$"
        raise SkillRegistryError(code, f"{path}: {exc.message}") from exc


def _row_dict(row: Any) -> dict[str, Any]:
    return dict(row) if row is not None else {}


def _manifest_for_status(manifest: Mapping[str, Any], status: str) -> dict[str, Any]:
    """Return the lifecycle-only representation of an immutable contract."""
    return {**manifest, "status": status}


def _validate_persisted_skill_version(row: Mapping[str, Any]) -> dict[str, Any]:
    """Reject a stored version whose lifecycle and immutable payload disagree."""
    stored_manifest = row["manifest"]
    if isinstance(stored_manifest, str):
        try:
            stored_manifest = json.loads(stored_manifest)
        except json.JSONDecodeError as exc:
            raise SkillRegistryError("stored_skill_contract_mismatch", status_code=409) from exc
    manifest = validate_skill_manifest(stored_manifest)
    if manifest["status"] != row["status"]:
        raise SkillRegistryError("stored_skill_lifecycle_mismatch", status_code=409)
    try:
        content = json.loads(row["content"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise SkillRegistryError("stored_skill_contract_mismatch", status_code=409) from exc
    if content != manifest or _sha256(manifest) != row["content_sha256"]:
        raise SkillRegistryError("stored_skill_contract_mismatch", status_code=409)
    return manifest


def _enforce_executable_risk_policy(manifest: Mapping[str, Any]) -> None:
    """Fail closed when a risk tier is prohibited rather than approvable.

    Human approval can authorize an otherwise permitted high-risk operation,
    but it must never override an absolute policy rejection such as destructive
    actions.
    """
    policy = RISK_POLICIES.get(str(manifest.get("risk_tier"))) or {}
    if policy.get("decision") == "reject":
        raise SkillRegistryError("skill_policy_rejected", status_code=403)


async def create_skill(*, tenant_id: str, slug: str, title: str, description: str,
                       projects: list[str], intents: list[str], actor: str) -> dict[str, Any]:
    from app.core.db_pool import get_pool
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO ops_skill_library
               (tenant_id,slug,title,description,projects,intents,source_path,metadata)
               VALUES($1::uuid,$2,$3,$4,$5::text[],$6::text[],$7,$8::jsonb)
               RETURNING *""",
            tenant_id, slug, title, description, projects, intents,
            f"site-skill:{slug}", json.dumps({"created_by": actor, "canonical": "site_skill"}),
        )
        return _row_dict(row)


async def list_skill_manifests(*, tenant_id: str, slug: str | None = None,
                               status: str | None = None) -> list[dict[str, Any]]:
    from app.core.db_pool import get_pool
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            """SELECT l.id::text AS skill_id,l.slug,l.title,l.description,l.projects,l.intents,
                      l.enabled,v.id::text AS version_id,v.version,v.status,v.manifest,
                      v.content_sha256,v.created_at
               FROM ops_skill_library l LEFT JOIN ops_skill_versions v ON v.skill_id=l.id
               WHERE l.tenant_id=$1::uuid AND ($2::text IS NULL OR l.slug=$2)
                 AND ($3::text IS NULL OR v.status=$3)
               ORDER BY l.slug,v.created_at DESC""",
            tenant_id, slug, status,
        )
        return [_row_dict(row) for row in rows]


async def update_skill(*, tenant_id: str, skill_id: str, title: str,
                       description: str, projects: list[str], intents: list[str]) -> dict[str, Any]:
    from app.core.db_pool import get_pool
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            """UPDATE ops_skill_library SET title=$3,description=$4,projects=$5::text[],
                      intents=$6::text[],updated_at=clock_timestamp()
               WHERE tenant_id=$1::uuid AND id=$2::uuid RETURNING *""",
            tenant_id, skill_id, title, description, projects, intents,
        )
        if not row:
            raise SkillRegistryError("skill_not_found", status_code=404)
        return _row_dict(row)


async def disable_skill(*, tenant_id: str, skill_id: str) -> dict[str, Any]:
    """Soft-delete a canonical skill while preserving immutable runs/versions."""
    from app.core.db_pool import get_pool
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            """UPDATE ops_skill_library SET enabled=FALSE,updated_at=clock_timestamp()
               WHERE tenant_id=$1::uuid AND id=$2::uuid RETURNING *""", tenant_id, skill_id,
        )
        if not row:
            raise SkillRegistryError("skill_not_found", status_code=404)
        return _row_dict(row)


async def add_skill_version(*, tenant_id: str, skill_id: str,
                            manifest: Mapping[str, Any]) -> dict[str, Any]:
    normalized = validate_skill_manifest(manifest)
    if str(normalized["skill_id"]) != skill_id:
        raise SkillRegistryError("skill_id_mismatch")
    if normalized["status"] != "candidate":
        raise SkillRegistryError("candidate_required_for_new_version")
    digest = _sha256(normalized)
    from app.core.db_pool import get_pool
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO ops_skill_versions
               (skill_id,version,content_sha256,content,status,manifest)
               SELECT l.id,$3,$4,$5,$6,$7::jsonb FROM ops_skill_library l
               WHERE l.id=$2::uuid AND l.tenant_id=$1::uuid
               RETURNING *""",
            tenant_id, skill_id, normalized["version"], digest, _canonical_json(normalized),
            normalized["status"], json.dumps(normalized),
        )
        if not row:
            raise SkillRegistryError("skill_not_found", status_code=404)
        return _row_dict(row)


async def validate_stored_skill(*, tenant_id: str, skill_id: str, version: str) -> dict[str, Any]:
    from app.core.db_pool import get_pool
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            """SELECT v.status,v.manifest,v.content,v.content_sha256 FROM ops_skill_versions v
               JOIN ops_skill_library l ON l.id=v.skill_id
               WHERE l.tenant_id=$1::uuid AND l.id=$2::uuid AND v.version=$3""",
            tenant_id, skill_id, version,
        )
    if not row:
        raise SkillRegistryError("skill_version_not_found", status_code=404)
    manifest = _validate_persisted_skill_version(row)
    return {"valid": True, "manifest": manifest,
            "executor_registered": manifest["executor"] in _SKILL_EXECUTORS}


async def promote_skill_version(*, tenant_id: str, skill_id: str, version: str,
                                actor: str, evidence: list[dict[str, Any]],
                                idempotency_key: str,
                                focused_results: Mapping[str, Any],
                                affected_regressions: Mapping[str, Any],
                                candidate_metrics: Mapping[str, Any],
                                active_metrics: Mapping[str, Any]) -> dict[str, Any]:
    if not evidence:
        raise SkillRegistryError("promotion_evidence_required")
    evidence_types = {
        str(item.get("type")) for item in evidence if isinstance(item, Mapping)
    }
    if "aads_handover_db" not in evidence_types:
        raise SkillRegistryError("aads_handover_db_evidence_required")
    release_evidence = next(
        (item for item in evidence if isinstance(item, Mapping) and item.get("type") == "release_state"), None
    )
    if not release_evidence or any(key not in release_evidence for key in ("commit", "push", "deploy")):
        raise SkillRegistryError("actual_commit_push_deploy_state_required")
    from app.services.golden_promotion_gate import evaluate_promotion_gate
    decision = evaluate_promotion_gate(
        focused_results=focused_results, affected_regressions=affected_regressions,
        candidate_metrics=candidate_metrics, active_metrics=active_metrics,
    )
    from app.core.db_pool import get_pool
    async with get_pool().acquire() as conn, conn.transaction():
        prior = await conn.fetchrow(
            """SELECT decision,to_status,reason_codes FROM browser_promotion_ledgers
               WHERE tenant_id=$1::uuid AND idempotency_key=$2 FOR UPDATE""",
            tenant_id, idempotency_key,
        )
        if prior:
            return {"idempotent": True, **_row_dict(prior)}
        row = await conn.fetchrow(
            """SELECT v.id,v.status,v.manifest,v.content,v.content_sha256 FROM ops_skill_versions v
                   JOIN ops_skill_library l ON l.id=v.skill_id
                   WHERE l.tenant_id=$1::uuid AND l.id=$2::uuid AND v.version=$3 FOR UPDATE""",
            tenant_id, skill_id, version,
        )
        if not row:
            raise SkillRegistryError("skill_version_not_found", status_code=404)
        manifest = _validate_persisted_skill_version(row)
        if row["status"] not in {"candidate", "shadow"}:
            raise SkillRegistryError("version_not_promotable", status_code=409)
        _enforce_executable_risk_policy(manifest)
        if manifest["executor"] not in _SKILL_EXECUTORS:
            raise SkillRegistryError("executor_not_registered", status_code=409)
        target_status = "shadow" if row["status"] == "candidate" else "active"
        if not decision.passed:
            quarantined_manifest = _manifest_for_status(manifest, "quarantined")
            await conn.execute(
                """UPDATE ops_skill_versions SET status='quarantined',manifest=$2::jsonb,
                          content=$3,content_sha256=$4,quarantine_reason=$5 WHERE id=$1""",
                row["id"], json.dumps(quarantined_manifest), _canonical_json(quarantined_manifest),
                _sha256(quarantined_manifest), ";".join(decision.reasons),
            )
            await _write_promotion_ledger(
                conn, tenant_id=tenant_id, skill_id=skill_id, version_id=str(row["id"]),
                idempotency_key=idempotency_key, actor=actor, from_status=row["status"],
                to_status="quarantined", decision="blocked", reasons=decision.reasons,
                focused_results=focused_results, affected_regressions=affected_regressions,
                candidate_metrics=candidate_metrics, active_metrics=active_metrics, evidence=evidence,
            )
            return {"status": "quarantined", "reasons": list(decision.reasons)}
        if target_status == "shadow":
            shadow_manifest = _manifest_for_status(manifest, "shadow")
            updated = await conn.fetchrow(
                """UPDATE ops_skill_versions SET status='shadow',manifest=$2::jsonb,
                          content=$3,content_sha256=$4,promotion_evidence=$5::jsonb
                   WHERE id=$1 RETURNING *""",
                row["id"], json.dumps(shadow_manifest), _canonical_json(shadow_manifest),
                _sha256(shadow_manifest), json.dumps(evidence),
            )
            await _write_promotion_ledger(
                conn, tenant_id=tenant_id, skill_id=skill_id, version_id=str(row["id"]),
                idempotency_key=idempotency_key, actor=actor, from_status="candidate",
                to_status="shadow", decision="promoted", reasons=(), focused_results=focused_results,
                affected_regressions=affected_regressions, candidate_metrics=candidate_metrics,
                active_metrics=active_metrics, evidence=evidence,
            )
            return _row_dict(updated)
        active_rows = await conn.fetch(
            """SELECT id,status,manifest,content,content_sha256 FROM ops_skill_versions
               WHERE skill_id=$1::uuid AND status='active' FOR UPDATE""", skill_id,
        )
        for active in active_rows:
            active_manifest = _validate_persisted_skill_version(active)
            retired_manifest = _manifest_for_status(active_manifest, "deprecated")
            await conn.execute(
                """UPDATE ops_skill_versions
                   SET status='deprecated',manifest=$2::jsonb,content=$3,content_sha256=$4
                   WHERE id=$1""",
                active["id"], json.dumps(retired_manifest), _canonical_json(retired_manifest),
                _sha256(retired_manifest),
            )
        updated_manifest = _manifest_for_status(manifest, "active")
        updated_digest = _sha256(updated_manifest)
        updated = await conn.fetchrow(
            """UPDATE ops_skill_versions SET status='active',manifest=$2::jsonb,
                          content=$3,content_sha256=$4,promoted_at=clock_timestamp(),
                          promoted_by=$5,promotion_evidence=$6::jsonb,previous_active_id=$7
                   WHERE id=$1 RETURNING *""",
            row["id"], json.dumps(updated_manifest), _canonical_json(updated_manifest),
            updated_digest, actor, json.dumps(evidence), active_rows[0]["id"] if active_rows else None,
        )
        await _write_promotion_ledger(
            conn, tenant_id=tenant_id, skill_id=skill_id, version_id=str(row["id"]),
            idempotency_key=idempotency_key, actor=actor, from_status="shadow", to_status="active",
            decision="promoted", reasons=(), focused_results=focused_results,
            affected_regressions=affected_regressions, candidate_metrics=candidate_metrics,
            active_metrics=active_metrics, evidence=evidence,
        )
        return _row_dict(updated)


async def _write_promotion_ledger(conn: Any, *, tenant_id: str, skill_id: str,
                                  version_id: str, idempotency_key: str, actor: str,
                                  from_status: str, to_status: str, decision: str,
                                  reasons: Any, focused_results: Mapping[str, Any],
                                  affected_regressions: Mapping[str, Any],
                                  candidate_metrics: Mapping[str, Any], active_metrics: Mapping[str, Any],
                                  evidence: list[dict[str, Any]]) -> None:
    await conn.execute(
        """INSERT INTO browser_promotion_ledgers
           (tenant_id,artifact_type,artifact_id,version_id,idempotency_key,requested_by,
            from_status,to_status,decision,reason_codes,focused_results,affected_regressions,
            candidate_metrics,active_metrics,evidence)
           VALUES($1::uuid,'site_skill',$2::uuid,$3::uuid,$4,$5,$6,$7,$8,$9::jsonb,
                  $10::jsonb,$11::jsonb,$12::jsonb,$13::jsonb,$14::jsonb)""",
        tenant_id, skill_id, version_id, idempotency_key, actor, from_status, to_status,
        decision, json.dumps(list(reasons)), json.dumps(dict(focused_results)),
        json.dumps(dict(affected_regressions)), json.dumps(dict(candidate_metrics)),
        json.dumps(dict(active_metrics)), json.dumps(evidence),
    )


async def rollback_skill_version(*, tenant_id: str, skill_id: str, actor: str,
                                 idempotency_key: str, reason: str,
                                 evidence: list[dict[str, Any]]) -> dict[str, Any]:
    """Atomically restore previous_active; learned evidence is never deleted."""
    if not reason or not evidence:
        raise SkillRegistryError("rollback_reason_and_evidence_required")
    from app.core.db_pool import get_pool
    async with get_pool().acquire() as conn, conn.transaction():
        prior = await conn.fetchrow(
            """SELECT decision,to_status,reason_codes FROM browser_promotion_ledgers
               WHERE tenant_id=$1::uuid AND idempotency_key=$2 FOR UPDATE""",
            tenant_id, idempotency_key,
        )
        if prior:
            return {"idempotent": True, **_row_dict(prior)}
        current = await conn.fetchrow(
            """SELECT v.* FROM ops_skill_versions v JOIN ops_skill_library l ON l.id=v.skill_id
               WHERE l.tenant_id=$1::uuid AND v.skill_id=$2::uuid AND v.status='active' FOR UPDATE""",
            tenant_id, skill_id,
        )
        if not current or not current["previous_active_id"]:
            raise SkillRegistryError("previous_active_not_available", status_code=409)
        previous = await conn.fetchrow(
            "SELECT * FROM ops_skill_versions WHERE id=$1 AND skill_id=$2::uuid FOR UPDATE",
            current["previous_active_id"], skill_id,
        )
        if not previous or previous["status"] != "deprecated":
            raise SkillRegistryError("previous_active_invalid", status_code=409)
        current_manifest = _manifest_for_status(_validate_persisted_skill_version(current), "deprecated")
        previous_manifest = _manifest_for_status(_validate_persisted_skill_version(previous), "active")
        await conn.execute(
            """UPDATE ops_skill_versions SET status='deprecated',manifest=$2::jsonb,
               content=$3,content_sha256=$4 WHERE id=$1""",
            current["id"], json.dumps(current_manifest), _canonical_json(current_manifest), _sha256(current_manifest),
        )
        restored = await conn.fetchrow(
            """UPDATE ops_skill_versions SET status='active',manifest=$2::jsonb,
               content=$3,content_sha256=$4,promoted_at=clock_timestamp(),promoted_by=$5
               WHERE id=$1 RETURNING *""",
            previous["id"], json.dumps(previous_manifest), _canonical_json(previous_manifest),
            _sha256(previous_manifest), actor,
        )
        await _write_promotion_ledger(
            conn, tenant_id=tenant_id, skill_id=skill_id, version_id=str(current["id"]),
            idempotency_key=idempotency_key, actor=actor, from_status="active",
            to_status="active", decision="rolled_back", reasons=(reason,),
            focused_results={}, affected_regressions={}, candidate_metrics={}, active_metrics={}, evidence=evidence,
        )
        return _row_dict(restored)


def _approval_matches(approval: Mapping[str, Any] | None, *, tenant_id: str,
                      skill_id: str, version: str, input_hash: str,
                      channel_provenance: Mapping[str, Any]) -> bool:
    if not approval or approval.get("decision") != "approved":
        return False
    scope = approval.get("approval_scope") or {}
    if isinstance(scope, str):
        scope = json.loads(scope)
    used = int(scope.get("used", 0))
    maximum = int(approval.get("max_executions", 1))
    return (
        str(approval.get("tenant_id")) == tenant_id
        and scope.get("skill_id") == skill_id
        and scope.get("version") == version
        and scope.get("input_hash") == input_hash
        and scope.get("channel_router_provenance") == dict(channel_provenance)
        and used < maximum
    )


async def execute_skill(
    *, tenant_id: str, skill_id: str, version: str,
    input_data: Mapping[str, Any], idempotency_key: str | None,
    action_intent: ActionIntent, approval_id: str | None = None,
) -> dict[str, Any]:
    """Execute only an active, schema-valid, code-registered skill version."""
    from app.services.channel_router import ChannelRouter

    ChannelRouter().validate_action_intent(action_intent, capability="skill.execute")
    if action_intent.tenant_id != tenant_id:
        raise SkillRegistryError("tenant_mismatch", status_code=403)
    expected_payload = {"skill_id": skill_id, "version": version, "input": dict(input_data)}
    if dict(action_intent.payload) != expected_payload:
        raise SkillRegistryError("action_intent_payload_mismatch", status_code=403)
    input_hash = _sha256(input_data)
    from app.core.db_pool import get_pool
    async with get_pool().acquire() as conn:  # noqa: SIM117 - asyncpg transaction typing.
        async with conn.transaction():
            row = await conn.fetchrow(
                """SELECT l.slug,l.risk_tier,v.id AS version_id,v.status,v.manifest,
                          v.content,v.content_sha256
                   FROM ops_skill_library l JOIN ops_skill_versions v ON v.skill_id=l.id
                   WHERE l.tenant_id=$1::uuid AND l.id=$2::uuid AND v.version=$3
                     AND v.status='active' AND l.enabled IS TRUE FOR SHARE""",
                tenant_id, skill_id, version,
            )
            if not row:
                raise SkillRegistryError("active_skill_version_not_found", status_code=404)
            manifest = _validate_persisted_skill_version(row)
            _enforce_executable_risk_policy(manifest)
            executor = _SKILL_EXECUTORS.get(manifest["executor"])
            if executor is None:
                raise SkillRegistryError("executor_not_registered", status_code=409)
            _validate_instance(
                manifest["input_schema"], input_data, "input_schema_validation_failed"
            )
            mode = manifest["idempotency"]["mode"]
            if mode == "required" and not idempotency_key:
                raise SkillRegistryError("idempotency_key_required")
            if mode == "forbidden" and idempotency_key:
                raise SkillRegistryError("idempotency_key_forbidden")
            # Reserve the idempotency key before consuming a Human Gateway
            # approval.  ON CONFLICT waits for a concurrent transaction, then
            # deterministically returns its existing run instead of leaking a
            # uniqueness error as a 500 or consuming approval twice.
            run = await conn.fetchrow(
                """INSERT INTO ops_skill_runs
                   (tenant_id,skill_id,skill_version_id,skill_slug,project,status,input,input_hash,
                    idempotency_key,policy_decision,channel_provenance,approval_id)
                   VALUES($1::uuid,$2::uuid,$3,$4,$5,'running',$6::jsonb,$7,$8,
                          $9::jsonb,$10::jsonb,$11::uuid)
                   ON CONFLICT (tenant_id,idempotency_key) WHERE idempotency_key IS NOT NULL
                   DO NOTHING
                   RETURNING id::text""",
                tenant_id, skill_id, row["version_id"], row["slug"],
                (manifest.get("provenance") or {}).get("project"), json.dumps(input_data), input_hash,
                idempotency_key, json.dumps(RISK_POLICIES[manifest["risk_tier"]]),
                json.dumps(dict(action_intent.authenticated_provenance)), approval_id,
            )
            if not run and idempotency_key:
                existing = await conn.fetchrow(
                    """SELECT * FROM ops_skill_runs WHERE tenant_id=$1::uuid
                       AND idempotency_key=$2""", tenant_id, idempotency_key,
                )
                if not existing:
                    raise SkillRegistryError("idempotency_reservation_failed", status_code=409)
                wrong_input = existing["input_hash"] != input_hash
                wrong_skill = str(existing["skill_id"]) != skill_id
                if wrong_input or wrong_skill:
                    raise SkillRegistryError("idempotency_conflict", status_code=409)
                return _row_dict(existing)
            approval = None
            if manifest["risk_tier"] in HIGH_RISK_TIERS:
                approval = await conn.fetchrow(
                    """SELECT id::text,tenant_id::text,decision,approval_scope,max_executions
                       FROM agent_permission_requests
                       WHERE id=$1::uuid AND tenant_id=$2::uuid
                         AND expires_at > clock_timestamp() FOR UPDATE""",
                    approval_id, tenant_id,
                ) if approval_id else None
                if not _approval_matches(_row_dict(approval) if approval else None,
                                         tenant_id=tenant_id, skill_id=skill_id,
                                         version=version, input_hash=input_hash,
                                         channel_provenance=action_intent.authenticated_provenance):
                    raise SkillRegistryError("human_gateway_approval_required", status_code=403)
                await conn.execute(
                    """UPDATE agent_permission_requests
                       SET approval_scope=jsonb_set(approval_scope,'{used}',
                           to_jsonb(COALESCE((approval_scope->>'used')::int,0)+1),true),
                           updated_at=clock_timestamp()
                       WHERE id=$1::uuid""", approval_id,
                )
    started = time.monotonic()
    attempts = manifest["retry"]["max_attempts"]
    result: Any = None
    error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            if inspect.iscoroutinefunction(executor):
                value = executor(input_data)
            else:
                value = asyncio.to_thread(executor, input_data)
            result = await asyncio.wait_for(value, timeout=manifest["timeout_seconds"])
            _validate_instance(
                manifest["output_schema"], result, "output_schema_validation_failed"
            )
            _canonical_json(result)  # JSONB persistence must be possible before success.
            error = None
            break
        except Exception as exc:  # noqa: BLE001 - executor failures are persisted uniformly.
            error = exc
            if attempt < attempts and manifest["retry"]["backoff_seconds"]:
                await asyncio.sleep(manifest["retry"]["backoff_seconds"])
    latency_ms = int((time.monotonic() - started) * 1000)
    status = "failed" if error else "succeeded"
    output = {} if error else result
    async with get_pool().acquire() as conn:
        updated = await conn.fetchrow(
            """UPDATE ops_skill_runs SET status=$2,output=$3::jsonb,error=$4,
                      result_hash=$5,evidence=$6::jsonb,latency_ms=$7,cost_usd=0,
                      completed_at=clock_timestamp() WHERE id=$1::uuid RETURNING *""",
            run["id"], status, json.dumps(output), str(error)[:2000] if error else None,
            _sha256(output) if error is None else None,
            json.dumps(manifest["evidence"]), latency_ms,
        )
    if error:
        raise SkillRegistryError("skill_execution_failed", str(error), status_code=502) from error
    return _row_dict(updated)


@dataclass(frozen=True)
class SkillSpec:
    slug: str
    title: str
    projects: tuple[str, ...]
    intents: tuple[str, ...]
    risk_tier: str
    source: str
    allowed_tools: tuple[str, ...]
    validation: tuple[str, ...]
    terms: tuple[str, ...]


BUILTIN_SKILLS: tuple[SkillSpec, ...] = (
    SkillSpec(
        slug="aads-bluegreen-release",
        title="AADS blue-green release",
        projects=("AADS",),
        intents=("deploy", "release", "ops"),
        risk_tier="deploy",
        source="builtin",
        allowed_tools=("git", "docker", "deploy.sh", "curl", "query_database"),
        validation=("clean release SHA", "candidate health", "same digest standby", "5m P0/P1 monitor"),
        terms=("aads", "bluegreen", "deploy", "release", "배포", "릴리스"),
    ),
    SkillSpec(
        slug="runner-recovery",
        title="Pipeline Runner recovery",
        projects=PROJECTS,
        intents=("task_query", "pipeline", "ops", "recovery"),
        risk_tier="write",
        source="builtin",
        allowed_tools=("pipeline_runner_status", "read_task_logs", "terminate_task"),
        validation=("status requery", "log evidence", "stale/error separation"),
        terms=("runner", "pipeline", "stale", "approval", "러너", "작업", "복구"),
    ),
    SkillSpec(
        slug="authenticated-site-collector",
        title="Authenticated site collector",
        projects=("AADS", "CEO"),
        intents=("browser_collection", "pc_agent", "auth", "marketing"),
        risk_tier="auth",
        source="builtin",
        allowed_tools=("pc_agent", "browser_bridge", "credential_vault", "browser_tasks"),
        validation=("captcha/otp bypass blocked", "same work_key resume", "dry-run before collection"),
        terms=("login", "collector", "captcha", "otp", "pc agent", "browser", "로그인", "수집"),
    ),
    SkillSpec(
        slug="store-assistant-channel-collector",
        title="Store assistant channel collector",
        projects=("AADS", "CEO"),
        intents=("browser_collection", "store_assistant", "marketing"),
        risk_tier="auth",
        source="builtin",
        allowed_tools=("pc_agent", "browser_bridge", "browser_recipes"),
        validation=("site profile", "account policy", "manual challenge resume"),
        terms=("매장비서", "배민", "스마트플레이스", "쿠팡이츠", "채널", "수집"),
    ),
    SkillSpec(
        slug="go100-market-open-check",
        title="GO100 market open check",
        projects=("GO100",),
        intents=("ops", "finance", "audit"),
        risk_tier="financial",
        source="builtin",
        allowed_tools=("query_project_database", "run_remote_command", "read_remote_file"),
        validation=("stock names included", "read-only first", "order gate respected"),
        terms=("go100", "장초반", "진입", "매매", "종목", "market"),
    ),
    SkillSpec(
        slug="kis-broker-health",
        title="KIS broker health and risk gate",
        projects=("KIS",),
        intents=("ops", "finance", "health"),
        risk_tier="financial",
        source="builtin",
        allowed_tools=("query_project_database", "run_remote_command"),
        validation=("broker session checked", "order risk gate", "read-only report"),
        terms=("kis", "브로커", "계좌", "주문", "체결", "broker"),
    ),
    SkillSpec(
        slug="ntv2-merchant-contract",
        title="NTV2 merchant contract workflow",
        projects=("NTV2",),
        intents=("contract", "merchant", "docs"),
        risk_tier="write",
        source="builtin",
        allowed_tools=("read_remote_file", "run_remote_command", "export_data"),
        validation=("template source", "tenant scope", "document preview"),
        terms=("ntv2", "newtalk", "입점", "계약서", "merchant", "contract"),
    ),
    SkillSpec(
        slug="sf-video-pipeline-health",
        title="ShortFlow video pipeline health",
        projects=("SF",),
        intents=("ops", "video", "health"),
        risk_tier="read",
        source="builtin",
        allowed_tools=("run_remote_command", "list_remote_dir", "read_remote_file"),
        validation=("queue count", "worker health", "latest error sample"),
        terms=("shortflow", "sf", "video", "queue", "worker", "영상"),
    ),
    SkillSpec(
        slug="nas-image-job-health",
        title="NAS image job health",
        projects=("NAS",),
        intents=("ops", "image", "health"),
        risk_tier="read",
        source="builtin",
        allowed_tools=("run_remote_command", "list_remote_dir"),
        validation=("storage capacity", "job queue", "recent failures"),
        terms=("nas", "image", "storage", "job", "이미지", "용량"),
    ),
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _module_available(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def _tokens(text: str) -> set[str]:
    return {token.lower() for token in re.findall(r"[A-Za-z0-9가-힣_.-]+", text or "")}


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return parsed
        except Exception:
            pass
        return [value]
    return [value]


async def _table_exists(conn: Any, table: str) -> bool:
    value = await conn.fetchval(
        """
        SELECT EXISTS (
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema='public' AND table_name=$1
        )
        """,
        table,
    )
    return bool(value)


async def _tables_exist(conn: Any, tables: tuple[str, ...]) -> dict[str, bool]:
    """여러 테이블의 존재 여부를 한 번의 왕복으로 확인한다.

    상태 엔드포인트는 대시보드가 주기적으로 호출한다. 테이블당 1쿼리로 돌면
    FOUNDATION_TABLES가 늘어날 때마다 왕복이 그대로 늘어나므로 한 번에 묻는다.
    """
    rows = await conn.fetch(
        """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema='public' AND table_name = ANY($1::text[])
        """,
        list(tables),
    )
    present = {row["table_name"] for row in rows}
    return {table: table in present for table in tables}


async def _safe_count(conn: Any, table: str, where: str = "", *args: Any) -> int | None:
    if not await _table_exists(conn, table):
        return None
    query = f"SELECT COUNT(*)::int FROM {table}"
    if where:
        query += f" WHERE {where}"
    return await conn.fetchval(query, *args)


def _skill_to_dict(skill: SkillSpec, score: int = 0, match_reason: list[str] | None = None) -> dict[str, Any]:
    return {
        "slug": skill.slug,
        "title": skill.title,
        "projects": list(skill.projects),
        "intents": list(skill.intents),
        "risk_tier": skill.risk_tier,
        "policy": RISK_POLICIES.get(skill.risk_tier, RISK_POLICIES["read"]),
        "source": skill.source,
        "allowed_tools": list(skill.allowed_tools),
        "validation": list(skill.validation),
        "score": score,
        "match_reason": match_reason or [],
    }


def _score_skill(
    *,
    query_terms: set[str],
    project: str | None,
    intent: str | None,
    slug: str,
    title: str,
    projects: list[str] | tuple[str, ...],
    intents: list[str] | tuple[str, ...],
    terms: set[str] | None = None,
) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []
    project_set = {item.upper() for item in projects}
    if project and (project in project_set or "CEO" in project_set and project == "CEO"):
        score += 3
        reasons.append(f"project:{project}")
    if intent and intent in set(intents):
        score += 2
        reasons.append(f"intent:{intent}")
    haystack = terms or _tokens(" ".join([slug, title, " ".join(projects), " ".join(intents)]))
    matches = sorted(query_terms & haystack)
    if matches:
        score += len(matches) * 2
        reasons.append("terms:" + ",".join(matches[:5]))
    if not query_terms and not project and not intent:
        score = 1
        reasons.append("default")
    return score, reasons


async def _fetch_db_skills() -> list[dict[str, Any]]:
    try:
        from app.core.db_pool import get_pool

        async with get_pool().acquire() as conn:
            if not await _table_exists(conn, "ops_skill_library"):
                return []
            rows = await conn.fetch(
                """
                SELECT slug, title, description, projects, intents, risk_tier,
                       allowed_tools, validation, source_path
                FROM ops_skill_library
                WHERE enabled IS TRUE
                ORDER BY updated_at DESC, slug
                LIMIT 200
                """
            )
            return [dict(row) for row in rows]
    except Exception:
        return []


def scan_repository_skills() -> list[dict[str, Any]]:
    """Return local SKILL.md files as Skill Find candidates."""
    candidates: list[dict[str, Any]] = []
    for base in (".claude/skills", ".codex/skills"):
        root = _repo_root() / base
        if not root.exists():
            continue
        for path in sorted(root.glob("*/SKILL.md")):
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                content = ""
            slug = path.parent.name
            first_heading = next(
                (line.lstrip("# ").strip() for line in content.splitlines() if line.strip().startswith("#")),
                slug,
            )
            candidates.append({
                "slug": slug,
                "title": first_heading or slug,
                "projects": list(PROJECTS),
                "intents": ["skill", "ops"],
                "risk_tier": "write" if "deploy" in content.lower() else "read",
                "policy": RISK_POLICIES["write" if "deploy" in content.lower() else "read"],
                "source": str(path.relative_to(_repo_root())),
                "allowed_tools": [],
                "validation": ["read SKILL.md before action", "follow skill instructions"],
                "content_preview": content[:400],
            })
    return candidates


async def get_harness_status(project: str | None = None) -> dict[str, Any]:
    """Summarize OHVIS harness implementation and database readiness."""
    modules = {
        "langgraph": _module_available("langgraph"),
        "langchain_core": _module_available("langchain_core"),
        "langchain_mcp_adapters": _module_available("langchain_mcp_adapters"),
        "langsmith": _module_available("langsmith"),
        "langfuse": _module_available("langfuse"),
        "langchain": _module_available("langchain"),
    }

    db: dict[str, Any] = {"available": False}
    try:
        from app.core.db_pool import get_pool

        async with get_pool().acquire() as conn:
            table_state = await _tables_exist(conn, FOUNDATION_TABLES)
            db = {
                "available": True,
                "foundation_tables": table_state,
                "ohvis_tasks": {
                    "total": await _safe_count(conn, "ohvis_tasks"),
                    "running": await _safe_count(conn, "ohvis_tasks", "status='running'"),
                    "stale": await _safe_count(conn, "ohvis_tasks", "status='stale'"),
                },
                "ohvis_loops": {
                    "total": await _safe_count(conn, "ohvis_loops"),
                    "active": await _safe_count(conn, "ohvis_loops", "status='active'"),
                },
                "memory_facts": {
                    "total": await _safe_count(conn, "memory_facts", "superseded_by IS NULL"),
                    "project": await _safe_count(
                        conn,
                        "memory_facts",
                        "project=$1 AND superseded_by IS NULL",
                        project,
                    )
                    if project
                    else None,
                },
                "prompt_assets": {
                    "enabled": await _safe_count(conn, "prompt_assets", "enabled IS TRUE"),
                },
            }
    except Exception as exc:
        db = {"available": False, "error": str(exc)[:200]}

    components = [
        {
            "key": "harness",
            "status": "implemented",
            "evidence": ["this service", "ohvis task API", "risk policy registry"],
            "gap": "graph_run_id is advisory until migration is applied",
        },
        {
            "key": "langgraph",
            "status": "implemented" if modules["langgraph"] else "missing_dependency",
            "evidence": ["pyproject dependency", "app.graph.builder StateGraph"],
            "gap": "task/runner/loop durable run linkage still partial",
        },
        {
            "key": "langchain",
            "status": "partial" if modules["langchain_core"] else "missing_dependency",
            "evidence": ["langchain_core/provider packages", "MCP adapter"],
            "gap": "middleware adapter is exposed as policy, not yet runtime-enforced everywhere",
        },
        {
            "key": "langsmith",
            "status": "compatible_foundation" if modules["langsmith"] else "missing_dependency",
            "evidence": ["langsmith import", "ohvis_harness_traces migration"],
            "gap": "external LangSmith export remains opt-in and not enabled here",
        },
        {
            "key": "llm_wiki",
            "status": "foundation_ready" if db.get("foundation_tables", {}).get("ohvis_wiki_pages") else "memory_only",
            "evidence": ["memory_facts", "wiki migration", "wiki search endpoint"],
            "gap": "automatic report compiler is not wired yet",
        },
        {
            "key": "hermes",
            "status": "pattern_foundation",
            "evidence": ["skill find", "risk policies", "recommendation endpoint"],
            "gap": "external Hermes Agent runtime is intentionally not embedded",
        },
        {
            # 상태는 "8개 테이블이 전부 있다"까지만 말한다. llmops_traces 하나만
            # 보면 부분 적용된 DB가 정상으로 보이고, 테이블 존재가 적재 동작을
            # 보증하지도 않는다. 실제 동작은 /ohvis/llmops/status가 건수까지 본다.
            "key": "llmops",
            "status": (
                "foundation_ready"
                if all(db.get("foundation_tables", {}).get(t) for t in LLMOPS_TABLES)
                else "migration_pending"
            ),
            "evidence": [
                "migrations/163_ohvis_internal_llmops_foundation.sql",
                "migrations/164_llmops_ledger_schema_reconcile.sql",
                "/api/v1/ohvis/llmops/status",
                "rule evaluator rule_v1",
            ],
            "gap": "LLM-as-judge and external LangSmith egress stay opt-in and disabled",
        },
        {
            "key": "skill_find",
            "status": "implemented",
            "evidence": ["builtin skill registry", "repository SKILL.md scanner", "search endpoint"],
            "gap": "DB-backed skill version sync requires migration application",
        },
    ]

    return {
        "project": project,
        "modules": modules,
        "db": db,
        "components": components,
        "risk_policies": RISK_POLICIES,
        "repository_skills": scan_repository_skills(),
    }


async def find_skills(
    query: str,
    project: str | None = None,
    intent: str | None = None,
    limit: int = 5,
) -> dict[str, Any]:
    """Return reusable OHVIS skills for a natural-language task."""
    query_terms = _tokens(query)
    project_norm = project.upper() if project else None
    scored: list[dict[str, Any]] = []
    seen_slugs: set[str] = set()

    for db_skill in await _fetch_db_skills():
        projects = [str(item) for item in _as_list(db_skill.get("projects"))]
        intents = [str(item) for item in _as_list(db_skill.get("intents"))]
        terms = _tokens(
            " ".join(
                str(db_skill.get(key, ""))
                for key in ("slug", "title", "description", "source_path")
            )
        )
        score, reasons = _score_skill(
            query_terms=query_terms,
            project=project_norm,
            intent=intent,
            slug=str(db_skill.get("slug") or ""),
            title=str(db_skill.get("title") or ""),
            projects=projects,
            intents=intents,
            terms=terms,
        )
        if score > 0:
            slug = str(db_skill.get("slug") or "")
            seen_slugs.add(slug)
            scored.append({
                "slug": slug,
                "title": db_skill.get("title"),
                "projects": projects,
                "intents": intents,
                "risk_tier": db_skill.get("risk_tier") or "read",
                "policy": RISK_POLICIES.get(db_skill.get("risk_tier") or "read", RISK_POLICIES["read"]),
                "source": db_skill.get("source_path") or "ops_skill_library",
                "allowed_tools": [str(item) for item in _as_list(db_skill.get("allowed_tools"))],
                "validation": [str(item) for item in _as_list(db_skill.get("validation"))],
                "score": score + 1,
                "match_reason": reasons + ["db_seed"],
            })

    for skill in BUILTIN_SKILLS:
        if skill.slug in seen_slugs:
            continue
        score, reasons = _score_skill(
            query_terms=query_terms,
            project=project_norm,
            intent=intent,
            slug=skill.slug,
            title=skill.title,
            projects=skill.projects,
            intents=skill.intents,
            terms=set(skill.terms),
        )
        if score > 0:
            scored.append(_skill_to_dict(skill, score, reasons))

    for repo_skill in scan_repository_skills():
        haystack = _tokens(
            " ".join(
                str(repo_skill.get(key, ""))
                for key in ("slug", "title", "source", "content_preview")
            )
        )
        matches = sorted(query_terms & haystack)
        score = len(matches) * 2
        reasons = ["repo_skill"] if score else []
        if project_norm and project_norm in repo_skill.get("projects", []):
            score += 1
            reasons.append(f"project:{project_norm}")
        if score > 0:
            item = dict(repo_skill)
            item["score"] = score
            item["match_reason"] = reasons + (["terms:" + ",".join(matches[:5])] if matches else [])
            scored.append(item)

    scored.sort(key=lambda item: (item.get("score", 0), item.get("slug", "")), reverse=True)
    return {
        "query": query,
        "project": project_norm,
        "intent": intent,
        "skills": scored[: max(1, min(limit, 20))],
        "policy_note": "High-risk skills return approval policy only; execution remains gated.",
    }


async def search_wiki(query: str, project: str | None = None, limit: int = 10) -> dict[str, Any]:
    """Search OHVIS wiki pages when migrated, then fall back to memory_facts."""
    limit = max(1, min(limit, 50))
    terms = [term for term in _tokens(query) if len(term) >= 2][:6]
    if not terms:
        return {"query": query, "project": project, "results": [], "source": "none"}

    try:
        from app.core.db_pool import get_pool

        async with get_pool().acquire() as conn:
            if await _table_exists(conn, "ohvis_wiki_pages"):
                pattern = "%" + "%".join(terms) + "%"
                rows = await conn.fetch(
                    """
                    SELECT id::text, project, slug, title, summary, updated_at
                    FROM ohvis_wiki_pages
                    WHERE ($1::text IS NULL OR project=$1)
                      AND (LOWER(title || ' ' || COALESCE(summary,'') || ' ' || COALESCE(body,'')) LIKE LOWER($2))
                    ORDER BY updated_at DESC
                    LIMIT $3
                    """,
                    project,
                    pattern,
                    limit,
                )
                if rows:
                    return {
                        "query": query,
                        "project": project,
                        "source": "ohvis_wiki_pages",
                        "results": [dict(row) for row in rows],
                    }

            pattern = "%" + "%".join(terms) + "%"
            rows = await conn.fetch(
                """
                SELECT id::text, project, category, subject, detail, confidence, created_at, updated_at
                FROM memory_facts
                WHERE superseded_by IS NULL
                  AND ($1::text IS NULL OR project=$1)
                  AND LOWER(COALESCE(subject,'') || ' ' || COALESCE(detail,'') || ' ' || COALESCE(context_snippet,'')) LIKE LOWER($2)
                ORDER BY confidence DESC NULLS LAST, updated_at DESC
                LIMIT $3
                """,
                project,
                pattern,
                limit,
            )
            return {
                "query": query,
                "project": project,
                "source": "memory_facts",
                "results": [dict(row) for row in rows],
            }
    except Exception as exc:
        return {"query": query, "project": project, "results": [], "source": "error", "error": str(exc)[:200]}


async def recommend_hermes_improvements(
    goal: str,
    project: str | None = None,
    recent_failure: str | None = None,
) -> dict[str, Any]:
    """Map a task goal to Hermes-style closed-loop improvement actions."""
    skill_matches = await find_skills(goal, project=project, limit=3)
    actions = [
        {
            "phase": "recall",
            "action": "Search OHVIS wiki/memory and previous task cards before execution.",
            "endpoint": "/api/v1/ohvis/harness/wiki/search",
        },
        {
            "phase": "select_skill",
            "action": "Use top Skill Find candidate and apply its risk policy before tools.",
            "endpoint": "/api/v1/ohvis/harness/skill-find",
        },
        {
            "phase": "execute_with_gate",
            "action": "Pause for approve/respond/reject where risk policy requires it.",
            "policy": RISK_POLICIES,
        },
        {
            "phase": "learn",
            "action": "Persist reusable procedure or error-book candidate after completion.",
            "tables": ["ops_skill_runs", "ohvis_wiki_error_book", "experience_memory"],
        },
    ]
    if recent_failure:
        actions.append({
            "phase": "self_improve",
            "action": "Open a skill improvement candidate from the failure and require replay validation.",
            "failure": recent_failure[:500],
        })
    return {
        "goal": goal,
        "project": project,
        "recommended_skills": skill_matches["skills"],
        "closed_loop_actions": actions,
        "guardrail": "Hermes Agent patterns are absorbed internally; external autonomous runtime is not granted deploy/DB/financial authority.",
    }
