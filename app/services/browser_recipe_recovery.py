"""Fail-closed recovery for browser recipe execution failures.

Recovery observations intentionally contain classification and structural evidence
only.  They never retain cookies, OTPs, credentials, page instructions, or raw
failure text.  The registry recipe remains canonical; selector discoveries are
stored as a G6-gated *patch candidate*, never written to ``browser_recipes``.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Mapping
from typing import Any

from app.core.db_pool import get_pool

MAX_RECOVERY_RETRIES = 2
_CREDENTIAL_MARKERS = re.compile(r"(?:cookie|token|otp|password|secret|authorization|bearer|session[ _-]?id)", re.I)
_PAGE_COMMAND_MARKERS = re.compile(r"(?:javascript:|<script|\b(?:click|type|submit|navigate|execute)\s*\()", re.I)
_SELECTOR = re.compile(r"""^[a-zA-Z0-9_#.[\]="'~*^$|:+> ()\-,]+$""")
_SCOPE_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$")


def classify_failure(*, error_code: str = "", aria_decision: str = "") -> str:
    """Classify only stable error codes; unknown input deliberately fails closed."""
    value = f"{error_code} {aria_decision}".lower()
    if aria_decision == "rediscover" and "selector" not in value:
        return "aria_signature_mismatch"
    if any(token in value for token in ("selector", "element_not_found", "locator", "not_found")):
        return "selector_changed"
    if any(token in value for token in ("login", "auth", "401", "403", "session_expired", "expired")):
        return "login_expired"
    if any(token in value for token in ("timeout", "network", "connection", "dns", "502", "503", "504")):
        return "network_transient"
    return "unknown"


def recovery_plan(*, failure_class: str, prior_attempts: int) -> dict[str, Any]:
    """Return an idempotent bounded action; credentials always require a human."""
    if failure_class in {"selector_changed", "aria_signature_mismatch"}:
        return {"action": "rediscover", "retry_attempt": prior_attempts, "human_guidance": "구조 재발견 후 shadow 검증 결과를 제출하세요."}
    if failure_class == "network_transient" and prior_attempts < MAX_RECOVERY_RETRIES:
        return {"action": "retry", "retry_attempt": prior_attempts + 1, "human_guidance": "일시 네트워크 오류입니다. 동일 작업 키로 제한된 재시도를 실행하세요."}
    if failure_class == "network_transient":
        return {"action": "human_gateway", "retry_attempt": prior_attempts, "human_guidance": "네트워크 재시도 한도에 도달했습니다. 연결 상태를 확인한 뒤 새 복구 요청을 시작하세요."}
    if failure_class == "login_expired":
        return {"action": "human_gateway", "retry_attempt": prior_attempts, "human_guidance": "로그인이 만료되었습니다. 사용자 환경에서 다시 로그인하고 OTP/인증서를 직접 완료한 뒤 재개하세요."}
    return {"action": "human_gateway", "retry_attempt": prior_attempts, "human_guidance": "자동 복구 대상이 아닙니다. 안전한 페이지 상태를 확인한 뒤 Human Gateway에서 재개하세요."}


def _safe_selector(value: Any) -> str | None:
    selector = str(value or "").strip()
    if not selector or len(selector) > 500 or not _SELECTOR.fullmatch(selector):
        return None
    if _CREDENTIAL_MARKERS.search(selector) or _PAGE_COMMAND_MARKERS.search(selector):
        return None
    return selector


def _safe_scope_key(value: str, *, field: str, allow_empty: bool = False) -> str:
    normalized = str(value or "").strip()
    if allow_empty and not normalized:
        return ""
    if not _SCOPE_KEY.fullmatch(normalized) or _CREDENTIAL_MARKERS.search(normalized):
        raise ValueError(f"invalid_{field}")
    return normalized


def sanitize_recovery_evidence(evidence: Mapping[str, Any] | None) -> dict[str, Any]:
    """Retain only bounded hashes/counts, excluding all raw page and auth data."""
    evidence = evidence if isinstance(evidence, Mapping) else {}
    result: dict[str, Any] = {}
    for key in ("aria_signature_hash", "previous_signature_hash", "screenshot_hash", "trace_hash"):
        value = str(evidence.get(key) or "").strip()
        if re.fullmatch(r"(?:sha256:)?[0-9a-fA-F]{64}", value):
            result[key] = value.lower()
    node_count = evidence.get("node_count")
    if isinstance(node_count, int) and 0 <= node_count <= 10000:
        result["node_count"] = node_count
    return result


def _candidate_payload(*, recipe_id: str, recipe_version: str, site_key: str, page_key: str, skill_key: str,
                       skill_version: str, selector: str, evidence: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "kind": "browser_recipe_selector_patch",
        "recipe_id": recipe_id,
        "source_recipe_version": recipe_version,
        "site_key": site_key,
        "page_key": page_key,
        "skill_key": skill_key,
        "skill_version": skill_version,
        "selector_patch": {"replacement": selector},
        "structural_evidence": dict(evidence),
    }


async def record_recipe_recovery(
    *, tenant_id: str, recipe_id: str, recipe_version: str, site_key: str, page_key: str,
    skill_key: str = "", skill_version: str = "", run_id: str | None = None,
    idempotency_key: str, error_code: str = "", aria_decision: str = "",
    evidence: Mapping[str, Any] | None = None, rediscovered_selector: str | None = None,
) -> dict[str, Any]:
    """Persist one recovery decision and optionally create a non-active G6 candidate."""
    recipe_id = _safe_scope_key(recipe_id, field="recipe_id")
    recipe_version = _safe_scope_key(recipe_version, field="recipe_version")
    site_key = _safe_scope_key(site_key, field="site_key")
    page_key = _safe_scope_key(page_key, field="page_key")
    skill_key = _safe_scope_key(skill_key, field="skill_key", allow_empty=True)
    skill_version = _safe_scope_key(skill_version, field="skill_version", allow_empty=True)
    if not _IDEMPOTENCY_KEY.fullmatch(str(idempotency_key or "")) or _CREDENTIAL_MARKERS.search(idempotency_key):
        raise ValueError("invalid_idempotency_key")
    if run_id:
        run_id = str(uuid.UUID(str(run_id)))
    cleaned_evidence = sanitize_recovery_evidence(evidence)
    failure_class = classify_failure(error_code=error_code, aria_decision=aria_decision)
    tenant = uuid.UUID(str(tenant_id))
    safe_selector = _safe_selector(rediscovered_selector)
    async with get_pool().acquire() as conn, conn.transaction():
        replay = await conn.fetchrow(
            """SELECT failure_class,recovery_action,retry_attempt,retry_limit,candidate_artifact_version_id,human_guidance
                 FROM browser_recipe_recovery_events WHERE tenant_id=$1::uuid AND idempotency_key=$2 FOR UPDATE""",
            str(tenant), idempotency_key,
        )
        if replay:
            return {"idempotent": True, **dict(replay)}
        prior_attempts = await conn.fetchval(
            """SELECT count(*) FROM browser_recipe_recovery_events
                 WHERE tenant_id=$1::uuid AND recipe_id=$2 AND recipe_version=$3 AND site_key=$4 AND page_key=$5
                   AND skill_key=$6 AND skill_version=$7 AND failure_class='network_transient' AND recovery_action='retry'""",
            str(tenant), recipe_id, recipe_version, site_key, page_key, skill_key, skill_version,
        )
        plan = recovery_plan(failure_class=failure_class, prior_attempts=int(prior_attempts or 0))
        candidate_version_id = None
        if plan["action"] == "rediscover" and safe_selector:
            artifact_key = f"recipe:{recipe_id}:site:{site_key}:page:{page_key}:skill:{skill_key or '-'}:{skill_version or '-'}"
            artifact = await conn.fetchrow(
                """INSERT INTO browser_learned_artifacts (tenant_id,artifact_type,artifact_key)
                   VALUES($1::uuid,'page_template',$2)
                   ON CONFLICT (tenant_id,artifact_type,artifact_key) DO UPDATE SET artifact_key=EXCLUDED.artifact_key
                   RETURNING id""",
                str(tenant), artifact_key,
            )
            payload = _candidate_payload(recipe_id=recipe_id, recipe_version=recipe_version, site_key=site_key,
                                         page_key=page_key, skill_key=skill_key, skill_version=skill_version,
                                         selector=safe_selector, evidence=cleaned_evidence)
            canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            digest = "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            version = "recovery-" + digest.removeprefix("sha256:")[:16]
            candidate = await conn.fetchrow(
                """INSERT INTO browser_learned_artifact_versions (artifact_id,version,status,payload,payload_sha256)
                   VALUES($1::uuid,$2,'candidate',$3::jsonb,$4)
                   ON CONFLICT (artifact_id,version) DO UPDATE SET version=EXCLUDED.version
                   RETURNING id""",
                str(artifact["id"]), version, canonical, digest,
            )
            candidate_version_id = candidate["id"]
        row = await conn.fetchrow(
            """INSERT INTO browser_recipe_recovery_events
               (tenant_id,recipe_id,recipe_version,site_key,page_key,skill_key,skill_version,run_id,idempotency_key,
                failure_class,recovery_action,retry_attempt,retry_limit,candidate_artifact_version_id,evidence,human_guidance)
               VALUES($1::uuid,$2,$3,$4,$5,$6,$7,$8::uuid,$9,$10,$11,$12,$13,$14::uuid,$15::jsonb,$16)
               RETURNING failure_class,recovery_action,retry_attempt,retry_limit,candidate_artifact_version_id,human_guidance""",
            str(tenant), recipe_id, recipe_version, site_key, page_key, skill_key, skill_version, run_id,
            idempotency_key, failure_class, plan["action"], plan["retry_attempt"], MAX_RECOVERY_RETRIES,
            str(candidate_version_id) if candidate_version_id else None, json.dumps(cleaned_evidence), plan["human_guidance"],
        )
    return {"idempotent": False, **dict(row)}
