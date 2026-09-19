"""Tenant-scoped canonical knowledge for OVIS Smart Browser.

Browser observations remain untrusted.  This module persists only structural
templates, opaque evidence references, curated semantic summaries, executable
skill provenance, and short-lived live observations.  Raw DOM, credentials,
payment identifiers, and page-authored commands are rejected before storage.
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from app.core.db_pool import get_pool

_SENSITIVE_WORD = re.compile(
    r"(?:password|passwd|passcode|secret|api[_ -]?key|access[_ -]?token|refresh[_ -]?token|"
    r"authorization|bearer|otp|one[ _-]?time|cookie|session[_ -]?id|card[_ -]?number|"
    r"resident|ssn|주민등록|비밀번호|인증번호|카드번호)",
    re.IGNORECASE,
)
_SENSITIVE_VALUE = re.compile(
    r"(?:\b\d{6}[- ]?[1-4]\d{6}\b|\b(?:\d[ -]?){13,19}\b|"
    r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b|"
    r"\b(?:sk|pk|api)[_-][A-Za-z0-9_-]{16,}\b)",
    re.IGNORECASE,
)
_PAGE_COMMAND = re.compile(
    r"(?:ignore (?:all |previous )?instructions|system prompt|tool[_ ]?call|"
    r"<\s*/?(?:system|assistant|tool)\b|앞선 지시|지시를 무시|도구를 실행|"
    r"권한을 (?:부여|승격)|비밀번호를 입력)",
    re.IGNORECASE,
)
_RAW_MARKUP = re.compile(r"(?:<!doctype\s+html|<html\b|<body\b|<script\b|<iframe\b)", re.IGNORECASE)
_EVIDENCE_REF = re.compile(r"^(?:object|s3|gs|blob)://[A-Za-z0-9][A-Za-z0-9._/@:+-]{2,500}$")
_ALLOWED_TEMPLATE_KEYS = frozenset({
    "area_key", "signature_version", "signature", "required_anchors", "required_states",
    "stable_names", "stable_states", "reuse_threshold", "dom_fallback", "page_type",
})


class SiteKnowledgeError(ValueError):
    """Stable validation failure safe to expose through the API."""


def normalize_origin(value: str) -> str:
    parsed = urlsplit(str(value or "").strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise SiteKnowledgeError("invalid_origin")
    try:
        port = f":{parsed.port}" if parsed.port else ""
    except ValueError as exc:
        raise SiteKnowledgeError("invalid_origin") from exc
    return urlunsplit((parsed.scheme.lower(), f"{parsed.hostname.lower()}{port}", "", "", ""))


def evidence_refs(value: Sequence[str] | None) -> list[str]:
    refs = [str(item).strip() for item in (value or [])]
    if not refs or len(refs) > 50 or any(not _EVIDENCE_REF.fullmatch(item) for item in refs):
        raise SiteKnowledgeError("object_evidence_refs_required")
    return sorted(set(refs))


def _contains_forbidden_text(value: str) -> bool:
    return bool(
        _SENSITIVE_WORD.search(value)
        or _SENSITIVE_VALUE.search(value)
        or _PAGE_COMMAND.search(value)
        or _RAW_MARKUP.search(value)
    )


def safe_semantic_text(value: str, *, field: str = "content") -> str:
    text = str(value or "").strip()
    if not text or len(text) > 12000:
        raise SiteKnowledgeError(f"invalid_{field}")
    if _contains_forbidden_text(text):
        raise SiteKnowledgeError("sensitive_or_page_command_content")
    return text


def safe_observation_value(value: Any, *, depth: int = 0) -> Any:
    """Validate a JSON value without ever treating it as an instruction."""
    if depth > 12:
        raise SiteKnowledgeError("observation_nesting_too_deep")
    if isinstance(value, Mapping):
        if len(value) > 200:
            raise SiteKnowledgeError("observation_too_large")
        result: dict[str, Any] = {}
        for key, item in value.items():
            safe_key = str(key)
            if _SENSITIVE_WORD.search(safe_key):
                raise SiteKnowledgeError("sensitive_or_page_command_content")
            result[safe_key] = safe_observation_value(item, depth=depth + 1)
        return result
    if isinstance(value, list):
        if len(value) > 1000:
            raise SiteKnowledgeError("observation_too_large")
        return [safe_observation_value(item, depth=depth + 1) for item in value]
    if isinstance(value, str):
        return safe_semantic_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    raise SiteKnowledgeError("unsupported_observation_value")


def safe_page_template(value: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(value)
    if not payload or set(payload) - _ALLOWED_TEMPLATE_KEYS:
        raise SiteKnowledgeError("invalid_page_template_contract")
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(encoded) > 100_000 or _contains_forbidden_text(encoded):
        raise SiteKnowledgeError("sensitive_or_page_command_content")
    # Persist a normalized structural contract, never arbitrary DOM/OCR fields.
    return safe_observation_value(payload)


def canonical_provenance(value: Mapping[str, Any] | None, *, origin: str, version: str) -> dict[str, str]:
    raw = dict(value or {})
    source_kind = str(raw.get("source_kind") or "operator_curated").strip().lower()
    if source_kind not in {"operator_curated", "verified_extraction", "migration"}:
        raise SiteKnowledgeError("invalid_provenance_source")
    return {"source_kind": source_kind, "origin": normalize_origin(origin), "version": safe_semantic_text(version, field="version")[:120]}


def _origin_list(value: Any) -> list[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            value = []
    return [str(item) for item in value] if isinstance(value, list) else []


async def _profile(*, tenant_id: str, profile_id: str) -> dict[str, Any]:
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            """SELECT id,base_origin,allowed_origins,knowledge_version
                 FROM authenticated_site_profiles
                WHERE tenant_id=$1::uuid AND id=$2::uuid AND enabled IS TRUE""",
            tenant_id, profile_id,
        )
    if not row:
        raise SiteKnowledgeError("site_profile_not_found")
    return dict(row)


async def create_page_template_candidate(
    *, tenant_id: str, site_profile_id: str, page_key: str, version: str,
    template: Mapping[str, Any], evidence: Sequence[str], expires_at: datetime,
    provenance: Mapping[str, Any] | None,
) -> dict[str, Any]:
    profile = await _profile(tenant_id=tenant_id, profile_id=site_profile_id)
    refs = evidence_refs(evidence)
    if expires_at.tzinfo is None or expires_at.astimezone(UTC) <= datetime.now(UTC):
        raise SiteKnowledgeError("page_template_ttl_required")
    safe_page_key = safe_semantic_text(page_key, field="page_key")[:300]
    safe_version = safe_semantic_text(version, field="version")[:120]
    payload = safe_page_template(template)
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    provenance_value = canonical_provenance(provenance, origin=str(profile["base_origin"]), version=safe_version)
    async with get_pool().acquire() as conn, conn.transaction():
        artifact = await conn.fetchrow(
            """INSERT INTO browser_learned_artifacts(tenant_id,artifact_type,artifact_key)
               VALUES($1::uuid,'page_template',$2)
               ON CONFLICT(tenant_id,artifact_type,artifact_key)
               DO UPDATE SET artifact_key=EXCLUDED.artifact_key RETURNING id""",
            tenant_id, f"site:{site_profile_id}:page:{safe_page_key}",
        )
        digest = await conn.fetchval(
            "SELECT 'sha256:' || encode(digest(convert_to(($1::jsonb)::text,'UTF8'),'sha256'),'hex')",
            encoded,
        )
        row = await conn.fetchrow(
            """INSERT INTO browser_learned_artifact_versions
               (artifact_id,version,status,payload,payload_sha256,provenance,evidence_refs,expires_at)
               VALUES($1,$2,'candidate',$3::jsonb,$4,$5::jsonb,$6::jsonb,$7)
               ON CONFLICT(artifact_id,version) DO NOTHING
               RETURNING id,version,status,expires_at""",
            artifact["id"], safe_version, encoded, digest,
            json.dumps(provenance_value), json.dumps(refs), expires_at,
        )
    if not row:
        raise SiteKnowledgeError("page_template_version_exists")
    return {**dict(row), "artifact_id": str(artifact["id"]), "evidence_refs": refs, "provenance": provenance_value}


async def attach_browser_recipe_provenance(
    *, tenant_id: str, site_profile_id: str, recipe_id: str, version: str,
    expires_at: datetime, provenance: Mapping[str, Any] | None,
) -> dict[str, Any]:
    profile = await _profile(tenant_id=tenant_id, profile_id=site_profile_id)
    if expires_at.tzinfo is None or expires_at.astimezone(UTC) <= datetime.now(UTC):
        raise SiteKnowledgeError("browser_recipe_ttl_required")
    safe_recipe_id = safe_semantic_text(recipe_id, field="recipe_id")[:200]
    safe_version = safe_semantic_text(version, field="version")[:80]
    provenance_value = canonical_provenance(provenance, origin=str(profile["base_origin"]), version=safe_version)
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            """UPDATE browser_recipes
                  SET site_profile_id=$3::uuid,provenance=$4::jsonb,expires_at=$5
                WHERE tenant_id=$1::uuid AND recipe_id=$2 AND version=$6
                RETURNING id,recipe_id,version,expires_at""",
            tenant_id, safe_recipe_id, site_profile_id, json.dumps(provenance_value), expires_at, safe_version,
        )
    if not row:
        raise SiteKnowledgeError("browser_recipe_not_found")
    return {**dict(row), "provenance": provenance_value}


async def record_semantic_memory(
    *, tenant_id: str, site_profile_id: str, project: str, subject: str, detail: str,
    category: str, evidence: Sequence[str], expires_at: datetime,
    provenance: Mapping[str, Any] | None,
) -> dict[str, Any]:
    profile = await _profile(tenant_id=tenant_id, profile_id=site_profile_id)
    if expires_at.tzinfo is None or expires_at.astimezone(UTC) <= datetime.now(UTC):
        raise SiteKnowledgeError("semantic_memory_ttl_required")
    refs = evidence_refs(evidence)
    subject = safe_semantic_text(subject, field="subject")[:300]
    detail = safe_semantic_text(detail)
    provenance_value = canonical_provenance(provenance, origin=str(profile["base_origin"]), version=str(profile["knowledge_version"]))
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO memory_facts
               (tenant_id,site_profile_id,project,category,subject,detail,context_snippet,
                provenance,evidence_refs,expires_at)
               VALUES($1::uuid,$2::uuid,$3,$4,$5,$6,'',$7::jsonb,$8::jsonb,$9)
               RETURNING id,subject,expires_at""",
            tenant_id, site_profile_id, project[:20], category[:30], subject, detail,
            json.dumps(provenance_value), json.dumps(refs), expires_at,
        )
    return {**dict(row), "evidence_refs": refs, "provenance": provenance_value}


async def attach_site_skill_provenance(
    *, tenant_id: str, site_profile_id: str, skill_id: str, version: str,
    evidence: Sequence[str], expires_at: datetime, provenance: Mapping[str, Any] | None,
) -> dict[str, Any]:
    profile = await _profile(tenant_id=tenant_id, profile_id=site_profile_id)
    if expires_at.tzinfo is None or expires_at.astimezone(UTC) <= datetime.now(UTC):
        raise SiteKnowledgeError("site_skill_ttl_required")
    refs = evidence_refs(evidence)
    safe_version = safe_semantic_text(version, field="version")[:120]
    provenance_value = canonical_provenance(provenance, origin=str(profile["base_origin"]), version=safe_version)
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            """UPDATE ops_skill_versions v
                  SET site_profile_id=$4::uuid,provenance=$5::jsonb,evidence_refs=$6::jsonb,expires_at=$7
                 FROM ops_skill_library l
                WHERE v.skill_id=l.id AND l.tenant_id=$1::uuid AND l.id=$2::uuid
                  AND v.version=$3
                RETURNING v.id,v.version,v.status,v.expires_at""",
            tenant_id, skill_id, safe_version, site_profile_id,
            json.dumps(provenance_value), json.dumps(refs), expires_at,
        )
    if not row:
        raise SiteKnowledgeError("site_skill_version_not_found")
    return {**dict(row), "evidence_refs": refs, "provenance": provenance_value}


async def record_live_observation(
    *, tenant_id: str, site_profile_id: str, source_url: str, fact_type: str,
    entity_key: str, variant_key: str, revalidator_key: str, observed_value: Any,
    observed_at: datetime, expires_at: datetime, evidence_id: str,
    evidence: Sequence[str], provenance: Mapping[str, Any] | None,
    account_context: str = "",
) -> dict[str, Any]:
    profile = await _profile(tenant_id=tenant_id, profile_id=site_profile_id)
    source_origin = normalize_origin(source_url)
    allowed = {normalize_origin(item) for item in _origin_list(profile["allowed_origins"])}
    if source_origin not in allowed:
        raise SiteKnowledgeError("source_origin_not_allowed")
    refs = evidence_refs(evidence)
    safe_value = safe_observation_value(observed_value)
    provenance_value = canonical_provenance(provenance, origin=source_origin, version=str(profile["knowledge_version"]))
    from app.services.live_fact_gate import hash_account_context, record_live_fact

    fact = await record_live_fact(
        tenant_id=tenant_id, session_id=None, task_id=None,
        fact_type=safe_semantic_text(fact_type, field="fact_type")[:80],
        entity_key=safe_semantic_text(entity_key, field="entity_key")[:300],
        variant_key=str(variant_key or "")[:300],
        account_context_hash=hash_account_context(account_context),
        source_url=source_url, source_kind="site_knowledge",
        revalidator_key=safe_semantic_text(revalidator_key, field="revalidator_key")[:120],
        observed_value=safe_value, observed_at=observed_at, expires_at=expires_at,
        evidence_id=safe_semantic_text(evidence_id, field="evidence_id")[:500],
        evidence={"object_evidence_refs": refs},
    )
    async with get_pool().acquire() as conn:
        await conn.execute(
            """UPDATE browser_live_facts SET site_profile_id=$1::uuid,provenance=$2::jsonb
                WHERE tenant_id=$3::uuid AND id=$4::uuid""",
            site_profile_id, json.dumps(provenance_value), tenant_id, fact["fact_id"],
        )
    return {**fact, "evidence_refs": refs, "provenance": provenance_value}
