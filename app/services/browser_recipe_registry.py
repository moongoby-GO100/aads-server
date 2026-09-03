"""Browser recipe registry for API-less authenticated admin automation."""
from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any
from urllib.parse import urlparse

from app.core.db_pool import get_pool
from app.services.browser_permission_policy import classify_browser_action, mask_sensitive_value
from app.services.managed_browser import normalize_origin, normalize_work_key


ALLOWED_RUNTIMES = {"pc_agent", "self_hosted_playwright", "external_sandbox", "auto"}
SAAS_PROJECT_KEYS = {"AADS", "KIS", "GO100", "SF", "NTV2", "NAS", "CUSTOM"}
SITE_ENVIRONMENTS = {"webview2", "chrome_extension", "chrome_cdp", "playwright_server", "file_upload", "official_api", "manual_export"}
VERSION_STATUSES = {"draft", "active", "archived"}
ALLOWED_QUEUE_STRATEGIES = {"fifo", "priority", "latest_only", "reject_on_conflict"}
DEFAULT_CONCURRENCY_POLICY = {
    "max_parallel_runs": 1,
    "queue_strategy": "fifo",
    "conflict_keys": ["work_key", "origin"],
}
DEFAULT_RESOURCE_POLICY = {
    "runtime": "auto",
    "max_browser_contexts": 1,
    "max_memory_mb": 1024,
    "max_runtime_seconds": 900,
    "artifact_budget_mb": 256,
}
ACTIVE_RECIPE_RUN_STATUSES = ("queued", "running", "approval_required")
RECIPE_HASH_FIELDS = (
    "recipe_id",
    "version",
    "service",
    "allowed_origins",
    "work_key_template",
    "runtime_policy",
    "concurrency_policy",
    "resource_policy",
    "login_steps",
    "challenge_policy",
    "navigation_steps",
    "capture_rules",
    "parser_id",
    "upload_rules",
    "risk_actions",
    "verifier",
    "fallbacks",
    "project_key",
    "site_environment",
    "record_types",
    "normalization_schema",
    "fixture_cases",
    "version_status",
)


def _json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _int_between(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def normalize_concurrency_policy(policy: dict[str, Any] | None) -> dict[str, Any]:
    item = {**DEFAULT_CONCURRENCY_POLICY, **_json_dict(policy)}
    queue_strategy = str(item.get("queue_strategy") or "fifo").strip().lower()
    if queue_strategy not in ALLOWED_QUEUE_STRATEGIES:
        queue_strategy = "fifo"
    conflict_keys = [
        str(key).strip()
        for key in _json_list(item.get("conflict_keys"))
        if str(key).strip()
    ]
    return {
        "max_parallel_runs": _int_between(item.get("max_parallel_runs"), default=1, minimum=1, maximum=20),
        "queue_strategy": queue_strategy,
        "conflict_keys": conflict_keys[:20] or ["work_key", "origin"],
    }


def normalize_resource_policy(policy: dict[str, Any] | None) -> dict[str, Any]:
    item = {**DEFAULT_RESOURCE_POLICY, **_json_dict(policy)}
    runtime = str(item.get("runtime") or "auto").strip().lower()
    if runtime not in ALLOWED_RUNTIMES:
        runtime = "auto"
    return {
        "runtime": runtime,
        "max_browser_contexts": _int_between(item.get("max_browser_contexts"), default=1, minimum=1, maximum=50),
        "max_memory_mb": _int_between(item.get("max_memory_mb"), default=1024, minimum=256, maximum=32768),
        "max_runtime_seconds": _int_between(item.get("max_runtime_seconds"), default=900, minimum=30, maximum=86400),
        "artifact_budget_mb": _int_between(item.get("artifact_budget_mb"), default=256, minimum=1, maximum=10240),
    }


def normalize_allowed_origins(origins: list[Any]) -> list[str]:
    normalized: list[str] = []
    for origin in origins:
        try:
            value = normalize_origin(str(origin))
        except ValueError:
            value = ""
        if value and value not in normalized:
            normalized.append(value)
    return normalized


def normalize_recipe_payload(payload: dict[str, Any]) -> dict[str, Any]:
    recipe_id = str(payload.get("recipe_id") or "").strip()
    if not recipe_id:
        raise ValueError("recipe_id_required")
    version = str(payload.get("version") or "v1").strip()
    if not version:
        raise ValueError("version_required")
    work_key_template = normalize_work_key(str(payload.get("work_key_template") or recipe_id))
    allowed_origins = normalize_allowed_origins(_json_list(payload.get("allowed_origins")))
    if not allowed_origins:
        raise ValueError("allowed_origin_required")
    service = str(payload.get("service") or recipe_id.split(".")[0]).strip()
    runtime_policy = _json_dict(payload.get("runtime_policy"))
    resource_policy = normalize_resource_policy(payload.get("resource_policy") or runtime_policy)
    runtime_policy = {**runtime_policy, "runtime": resource_policy["runtime"]}
    project_key = str(payload.get("project_key") or "CUSTOM").strip().upper()
    site_environment = str(payload.get("site_environment") or "chrome_cdp").strip().lower()
    version_status = str(payload.get("version_status") or "draft").strip().lower()
    if project_key not in SAAS_PROJECT_KEYS:
        raise ValueError("unsupported_project_key")
    if site_environment not in SITE_ENVIRONMENTS:
        raise ValueError("unsupported_site_environment")
    if version_status not in VERSION_STATUSES:
        raise ValueError("unsupported_version_status")
    return {
        "recipe_id": recipe_id,
        "version": version,
        "title": str(payload.get("title") or recipe_id).strip()[:300],
        "service": service[:120],
        "allowed_origins": allowed_origins,
        "work_key_template": work_key_template,
        "runtime_policy": runtime_policy,
        "concurrency_policy": normalize_concurrency_policy(payload.get("concurrency_policy")),
        "resource_policy": resource_policy,
        "login_steps": _json_list(payload.get("login_steps")),
        "challenge_policy": _json_dict(payload.get("challenge_policy")),
        "navigation_steps": _json_list(payload.get("navigation_steps")),
        "capture_rules": _json_dict(payload.get("capture_rules")),
        "parser_id": str(payload.get("parser_id") or "").strip()[:200],
        "upload_rules": _json_dict(payload.get("upload_rules")),
        "risk_actions": _json_list(payload.get("risk_actions")),
        "verifier": _json_dict(payload.get("verifier")),
        "fallbacks": _json_dict(payload.get("fallbacks")),
        "enabled": bool(payload.get("enabled", True)),
        "project_key": project_key,
        "site_environment": site_environment,
        "record_types": _json_list(payload.get("record_types")),
        "normalization_schema": _json_dict(payload.get("normalization_schema")),
        "fixture_cases": _json_list(payload.get("fixture_cases")),
        "version_status": version_status,
    }


def compute_recipe_hash(recipe: dict[str, Any]) -> str:
    canonical = {key: recipe.get(key) for key in RECIPE_HASH_FIELDS}
    raw = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def build_recipe_dry_run_plan(recipe: dict[str, Any], *, target_url: str = "") -> dict[str, Any]:
    normalized = normalize_recipe_payload(recipe)
    version_hash = compute_recipe_hash(normalized)
    risk_actions = []
    for item in normalized["risk_actions"]:
        if not isinstance(item, dict):
            continue
        action_type = str(item.get("action_type") or item.get("type") or "").strip()
        summary = str(item.get("summary") or item.get("action_summary") or "")
        policy = classify_browser_action(action_type, summary, _json_dict(item.get("payload")))
        risk_actions.append(
            {
                "action_type": action_type,
                "summary": summary,
                "policy": policy.to_dict(),
                "approval_required": policy.decision == "ask",
            }
        )
    return {
        "recipe_id": normalized["recipe_id"],
        "version": normalized["version"],
        "version_hash": version_hash,
        "target_origin": normalize_origin(target_url) if target_url else normalized["allowed_origins"][0],
        "runtime": normalized["resource_policy"]["runtime"],
        "runtime_plan": build_runtime_execution_plan(normalized, target_url=target_url),
        "concurrency_policy": normalized["concurrency_policy"],
        "resource_policy": normalized["resource_policy"],
        "required_approvals": [item for item in risk_actions if item["approval_required"]],
        "blocked_actions": [item for item in risk_actions if item["policy"]["decision"] == "deny"],
        "artifact_capture": bool(normalized["capture_rules"]),
        "upload_enabled": bool(normalized["upload_rules"]),
    }


def build_recipe_concurrency_key(recipe: dict[str, Any], *, target_url: str = "", work_key: str = "") -> str:
    normalized = normalize_recipe_payload(recipe)
    policy = normalized["concurrency_policy"]
    origin = normalize_origin(target_url) if target_url else normalized["allowed_origins"][0]
    values = {
        "recipe_id": normalized["recipe_id"],
        "version": normalized["version"],
        "service": normalized["service"],
        "work_key": normalize_work_key(work_key or normalized["work_key_template"]),
        "origin": origin,
        "runtime": normalized["resource_policy"]["runtime"],
    }
    parts = [f"{key}:{values[key]}" for key in policy["conflict_keys"] if key in values and values[key]]
    if not parts:
        parts = [f"work_key:{values['work_key']}", f"origin:{values['origin']}"]
    return "|".join(parts)


def build_resource_claim(recipe: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_recipe_payload(recipe)
    resource_policy = normalized["resource_policy"]
    return {
        "runtime": resource_policy["runtime"],
        "browser_contexts": resource_policy["max_browser_contexts"],
        "memory_mb": resource_policy["max_memory_mb"],
        "runtime_seconds": resource_policy["max_runtime_seconds"],
        "artifact_budget_mb": resource_policy["artifact_budget_mb"],
    }


def _http_target_url(value: str) -> bool:
    parsed = urlparse(str(value or "").strip())
    return parsed.scheme.lower() in {"http", "https"} and bool(parsed.netloc)


def _recipe_requires_pc_agent(recipe: dict[str, Any]) -> tuple[bool, list[str]]:
    challenge_policy = _json_dict(recipe.get("challenge_policy"))
    runtime_policy = _json_dict(recipe.get("runtime_policy"))
    fallbacks = _json_dict(recipe.get("fallbacks"))
    reasons: list[str] = []
    if runtime_policy.get("requires_pc_agent") is True:
        reasons.append("runtime_policy_requires_pc_agent")
    for key in ("certificate", "local_certificate", "passkey", "desktop_only", "local_file_picker"):
        if challenge_policy.get(key) or runtime_policy.get(key) or fallbacks.get(key):
            reasons.append(f"{key}_requires_user_environment")
    return bool(reasons), reasons


def build_runtime_execution_plan(recipe: dict[str, Any], *, target_url: str = "") -> dict[str, Any]:
    normalized = normalize_recipe_payload(recipe)
    configured_runtime = normalized["resource_policy"]["runtime"]
    candidate_target = target_url or (normalized["allowed_origins"][0] if normalized["allowed_origins"] else "")
    self_hosted_eligible = _http_target_url(candidate_target)
    pc_agent_required, pc_agent_reasons = _recipe_requires_pc_agent(normalized)
    fallback_config = _json_dict(normalized.get("fallbacks"))
    configured_fallbacks = [
        str(value).strip()
        for value in fallback_config.values()
        if str(value).strip() in ALLOWED_RUNTIMES
    ]

    if configured_runtime != "auto":
        primary_runtime = configured_runtime
    elif pc_agent_required:
        primary_runtime = "pc_agent"
    elif self_hosted_eligible:
        primary_runtime = "self_hosted_playwright"
    else:
        primary_runtime = "pc_agent"

    fallback_runtimes: list[str] = []
    for runtime in [*configured_fallbacks, "pc_agent", "self_hosted_playwright", "external_sandbox"]:
        if runtime != primary_runtime and runtime not in fallback_runtimes:
            fallback_runtimes.append(runtime)

    return {
        "configured_runtime": configured_runtime,
        "primary_runtime": primary_runtime,
        "fallback_runtimes": fallback_runtimes[:4],
        "self_hosted_eligible": self_hosted_eligible,
        "pc_agent_required": pc_agent_required,
        "pc_agent_reasons": pc_agent_reasons,
        "access_probe_supported": primary_runtime in {"self_hosted_playwright", "auto"} or self_hosted_eligible,
        "notes": [
            "서버 Playwright 접근 실패는 access-check/live-frame diagnosis로 분류합니다.",
            "OTP/CAPTCHA/인증서는 승인 토큰 범위 안에서만 자동 입력 또는 모델 판독을 허용합니다.",
        ],
    }


def evaluate_recipe_run_admission(
    recipe: dict[str, Any],
    *,
    active_runs: int,
    target_url: str = "",
    work_key: str = "",
) -> dict[str, Any]:
    normalized = normalize_recipe_payload(recipe)
    policy = normalized["concurrency_policy"]
    max_parallel_runs = policy["max_parallel_runs"]
    queue_strategy = policy["queue_strategy"]
    concurrency_key = build_recipe_concurrency_key(normalized, target_url=target_url, work_key=work_key)
    can_start = active_runs < max_parallel_runs
    if can_start:
        decision = "start"
        reason = "capacity_available"
        status = "running"
    elif queue_strategy == "reject_on_conflict":
        decision = "reject"
        reason = "concurrency_conflict"
        status = "rejected"
    elif queue_strategy == "latest_only":
        decision = "queue_latest_only"
        reason = "supersede_queued_conflict"
        status = "queued"
    else:
        decision = "queue"
        reason = "capacity_exhausted"
        status = "queued"
    return {
        "decision": decision,
        "reason": reason,
        "status": status,
        "concurrency_key": concurrency_key,
        "active_runs": int(active_runs),
        "max_parallel_runs": max_parallel_runs,
        "queue_strategy": queue_strategy,
        "resource_claim": build_resource_claim(normalized),
        "runtime_plan": build_runtime_execution_plan(normalized, target_url=target_url),
    }


def _row_to_recipe(row: Any) -> dict[str, Any]:
    item = dict(row)
    for key in ("id", "tenant_id"):
        item[key] = str(item[key])
    for key in ("created_at", "updated_at"):
        if item.get(key):
            item[key] = item[key].isoformat()
    for key in (
        "allowed_origins",
        "runtime_policy",
        "concurrency_policy",
        "resource_policy",
        "login_steps",
        "challenge_policy",
        "navigation_steps",
        "capture_rules",
        "upload_rules",
        "risk_actions",
        "verifier",
        "fallbacks",
        "record_types",
        "normalization_schema",
        "fixture_cases",
    ):
        if key in item:
            item[key] = _json_dict(item[key]) if key.endswith("_policy") or key in {"challenge_policy", "capture_rules", "upload_rules", "verifier", "fallbacks", "normalization_schema"} else _json_list(item[key])
    return mask_sensitive_value(item)


async def upsert_browser_recipe(*, tenant_id: str, user_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    recipe = normalize_recipe_payload(payload)
    recipe["version_hash"] = compute_recipe_hash(recipe)
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO browser_recipes (
                tenant_id, recipe_id, version, title, service, allowed_origins, work_key_template,
                runtime_policy, concurrency_policy, resource_policy, login_steps, challenge_policy,
                navigation_steps, capture_rules, parser_id, upload_rules, risk_actions, verifier,
                fallbacks, enabled, version_hash, created_by, project_key, site_environment,
                record_types, normalization_schema, fixture_cases, version_status, updated_at
            )
            VALUES (
                $1, $2, $3, $4, $5, $6::jsonb, $7,
                $8::jsonb, $9::jsonb, $10::jsonb, $11::jsonb, $12::jsonb,
                $13::jsonb, $14::jsonb, $15, $16::jsonb, $17::jsonb, $18::jsonb,
                $19::jsonb, $20, $21, $22, $23, $24,
                $25::jsonb, $26::jsonb, $27::jsonb, $28, NOW()
            )
            ON CONFLICT (tenant_id, recipe_id, version) DO UPDATE
               SET title = EXCLUDED.title,
                   service = EXCLUDED.service,
                   allowed_origins = EXCLUDED.allowed_origins,
                   work_key_template = EXCLUDED.work_key_template,
                   runtime_policy = EXCLUDED.runtime_policy,
                   concurrency_policy = EXCLUDED.concurrency_policy,
                   resource_policy = EXCLUDED.resource_policy,
                   login_steps = EXCLUDED.login_steps,
                   challenge_policy = EXCLUDED.challenge_policy,
                   navigation_steps = EXCLUDED.navigation_steps,
                   capture_rules = EXCLUDED.capture_rules,
                   parser_id = EXCLUDED.parser_id,
                   upload_rules = EXCLUDED.upload_rules,
                   risk_actions = EXCLUDED.risk_actions,
                   verifier = EXCLUDED.verifier,
                   fallbacks = EXCLUDED.fallbacks,
                   enabled = EXCLUDED.enabled,
                   version_hash = EXCLUDED.version_hash,
                   project_key = EXCLUDED.project_key,
                   site_environment = EXCLUDED.site_environment,
                   record_types = EXCLUDED.record_types,
                   normalization_schema = EXCLUDED.normalization_schema,
                   fixture_cases = EXCLUDED.fixture_cases,
                   version_status = EXCLUDED.version_status,
                   updated_at = NOW()
            RETURNING *
            """,
            uuid.UUID(str(tenant_id)),
            recipe["recipe_id"],
            recipe["version"],
            recipe["title"],
            recipe["service"],
            json.dumps(recipe["allowed_origins"], ensure_ascii=False),
            recipe["work_key_template"],
            json.dumps(recipe["runtime_policy"], ensure_ascii=False),
            json.dumps(recipe["concurrency_policy"], ensure_ascii=False),
            json.dumps(recipe["resource_policy"], ensure_ascii=False),
            json.dumps(recipe["login_steps"], ensure_ascii=False),
            json.dumps(recipe["challenge_policy"], ensure_ascii=False),
            json.dumps(recipe["navigation_steps"], ensure_ascii=False),
            json.dumps(recipe["capture_rules"], ensure_ascii=False),
            recipe["parser_id"],
            json.dumps(recipe["upload_rules"], ensure_ascii=False),
            json.dumps(recipe["risk_actions"], ensure_ascii=False),
            json.dumps(recipe["verifier"], ensure_ascii=False),
            json.dumps(recipe["fallbacks"], ensure_ascii=False),
            recipe["enabled"],
            recipe["version_hash"],
            user_id,
            recipe["project_key"],
            recipe["site_environment"],
            json.dumps(recipe["record_types"], ensure_ascii=False),
            json.dumps(recipe["normalization_schema"], ensure_ascii=False),
            json.dumps(recipe["fixture_cases"], ensure_ascii=False),
            recipe["version_status"],
        )
    return _row_to_recipe(row)


async def list_browser_recipes(*, tenant_id: str, service: str | None = None, enabled: bool | None = None, project_key: str | None = None) -> list[dict[str, Any]]:
    args: list[Any] = [uuid.UUID(str(tenant_id))]
    where = ["tenant_id = $1"]
    if service:
        args.append(service)
        where.append(f"service = ${len(args)}")
    if enabled is not None:
        args.append(enabled)
        where.append(f"enabled = ${len(args)}")
    if project_key:
        args.append(project_key.upper())
        where.append(f"project_key = ${len(args)}")
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT *
              FROM browser_recipes
             WHERE {' AND '.join(where)}
             ORDER BY service, recipe_id, version DESC
            """,
            *args,
        )
    return [_row_to_recipe(row) for row in rows]


async def get_browser_recipe(*, tenant_id: str, recipe_id: str, version: str = "v1") -> dict[str, Any] | None:
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT *
              FROM browser_recipes
             WHERE tenant_id = $1
               AND recipe_id = $2
               AND version = $3
            """,
            uuid.UUID(str(tenant_id)),
            recipe_id,
            version,
        )
    return _row_to_recipe(row) if row else None


async def plan_browser_recipe_run(
    *,
    tenant_id: str,
    recipe_id: str,
    version: str = "v1",
    target_url: str = "",
    work_key: str = "",
) -> dict[str, Any] | None:
    recipe = await get_browser_recipe(tenant_id=tenant_id, recipe_id=recipe_id, version=version)
    if not recipe:
        return None
    concurrency_key = build_recipe_concurrency_key(recipe, target_url=target_url, work_key=work_key)
    async with get_pool().acquire() as conn:
        active_runs = await conn.fetchval(
            """
            SELECT count(*)
              FROM browser_recipe_runs
             WHERE tenant_id = $1
               AND concurrency_key = $2
               AND status = ANY($3::text[])
            """,
            uuid.UUID(str(tenant_id)),
            concurrency_key,
            list(ACTIVE_RECIPE_RUN_STATUSES),
        )
    dry_run = build_recipe_dry_run_plan(recipe, target_url=target_url)
    admission = evaluate_recipe_run_admission(
        recipe,
        active_runs=int(active_runs or 0),
        target_url=target_url,
        work_key=work_key,
    )
    return {**dry_run, "admission": admission}


async def create_browser_recipe_run(
    *,
    tenant_id: str,
    recipe_id: str,
    version: str = "v1",
    target_url: str = "",
    work_key: str = "",
) -> dict[str, Any] | None:
    recipe = await get_browser_recipe(tenant_id=tenant_id, recipe_id=recipe_id, version=version)
    if not recipe:
        return None
    plan = await plan_browser_recipe_run(
        tenant_id=tenant_id,
        recipe_id=recipe_id,
        version=version,
        target_url=target_url,
        work_key=work_key,
    )
    if not plan:
        return None
    admission = plan["admission"]
    if admission["decision"] == "reject":
        return {"status": "rejected", "plan": plan}
    origin = normalize_origin(target_url) if target_url else recipe["allowed_origins"][0]
    run_work_key = normalize_work_key(work_key or recipe["work_key_template"])
    async with get_pool().acquire() as conn:
        if admission["decision"] == "queue_latest_only":
            await conn.execute(
                """
                UPDATE browser_recipe_runs
                   SET status = 'superseded',
                       updated_at = NOW(),
                       completed_at = COALESCE(completed_at, NOW())
                 WHERE tenant_id = $1
                   AND concurrency_key = $2
                   AND status = 'queued'
                """,
                uuid.UUID(str(tenant_id)),
                admission["concurrency_key"],
            )
        row = await conn.fetchrow(
            """
            INSERT INTO browser_recipe_runs (
                tenant_id, recipe_id, recipe_version, recipe_hash, work_key, origin, runtime,
                status, concurrency_key, resource_claim, started_at, updated_at
            )
            VALUES (
                $1, $2, $3, $4, $5, $6, $7,
                $8, $9, $10::jsonb,
                CASE WHEN $8 = 'running' THEN NOW() ELSE NULL END,
                NOW()
            )
            RETURNING *
            """,
            uuid.UUID(str(tenant_id)),
            recipe["recipe_id"],
            recipe["version"],
            recipe["version_hash"],
            run_work_key,
            origin,
            admission["resource_claim"]["runtime"],
            admission["status"],
            admission["concurrency_key"],
            json.dumps(admission["resource_claim"], ensure_ascii=False),
        )
    item = dict(row)
    for key in ("id", "tenant_id", "task_id"):
        if item.get(key):
            item[key] = str(item[key])
    for key in ("started_at", "completed_at", "created_at", "updated_at"):
        if item.get(key):
            item[key] = item[key].isoformat()
    item["resource_claim"] = _json_dict(item.get("resource_claim"))
    item["result"] = _json_dict(item.get("result"))
    return {"status": "created", "run": mask_sensitive_value(item), "plan": plan}
