"""Tenant-scoped persistence for the Authenticated Site Collector SaaS."""
from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any
from urllib.parse import urlparse

from app.core.db_pool import get_pool

PROJECT_KEYS = {"AADS", "KIS", "GO100", "SF", "NTV2", "NAS", "CUSTOM"}
SITE_RUNTIMES = {
    "webview2", "chrome_extension", "chrome_cdp", "playwright_server",
    "file_upload", "official_api", "manual_export",
}
JOB_STATUSES = {"queued", "running", "action_required", "succeeded", "failed", "superseded", "cancelled"}
CHALLENGE_TYPES = {"otp", "captcha", "terms_consent", "permission_required", "session_expired", "login_required"}


def normalize_origin(value: str) -> str:
    parsed = urlparse(str(value or "").strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError("valid_http_origin_required")
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"


def normalize_site_profile(payload: dict[str, Any]) -> dict[str, Any]:
    project_key = str(payload.get("project_key") or "").strip().upper()
    runtime = str(payload.get("runtime") or "").strip().lower()
    site_key = str(payload.get("site_key") or "").strip().lower()
    if project_key not in PROJECT_KEYS:
        raise ValueError("unsupported_project_key")
    if runtime not in SITE_RUNTIMES:
        raise ValueError("unsupported_site_runtime")
    if not site_key or len(site_key) > 120:
        raise ValueError("site_key_required")
    base_origin = normalize_origin(str(payload.get("base_origin") or ""))
    origins = [normalize_origin(str(value)) for value in payload.get("allowed_origins", [])]
    if base_origin not in origins:
        origins.insert(0, base_origin)
    return {
        "project_key": project_key,
        "site_key": site_key,
        "display_name": str(payload.get("display_name") or site_key).strip()[:200],
        "base_origin": base_origin,
        "allowed_origins": list(dict.fromkeys(origins)),
        "runtime": runtime,
        "data_categories": [str(v).strip()[:80] for v in payload.get("data_categories", []) if str(v).strip()],
        "login_mode": str(payload.get("login_mode") or "user_session").strip()[:80],
        "challenge_policy": dict(payload.get("challenge_policy") or {}),
        "retention_policy": dict(payload.get("retention_policy") or {}),
        "enabled": bool(payload.get("enabled", True)),
        "metadata": dict(payload.get("metadata") or {}),
    }


def _row(row: Any) -> dict[str, Any]:
    item = dict(row)
    for key in ("id", "tenant_id", "site_profile_id"):
        if item.get(key) is not None:
            item[key] = str(item[key])
    for key in ("created_at", "updated_at", "last_authenticated_at", "last_collected_at", "next_run_at", "started_at", "finished_at"):
        if item.get(key):
            item[key] = item[key].isoformat()
    for key in ("allowed_origins", "data_categories", "challenge_policy", "retention_policy", "metadata", "payload", "result"):
        if isinstance(item.get(key), str):
            try:
                item[key] = json.loads(item[key])
            except json.JSONDecodeError:
                item[key] = {} if key in {"challenge_policy", "retention_policy", "metadata", "payload", "result"} else []
    return item


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


async def _audit(conn: Any, tenant_id: str, user_id: str, action: str, resource_type: str, resource_id: str, details: dict[str, Any]) -> None:
    # Callers pass operational metadata only. Secrets and challenge answers are never accepted.
    await conn.execute(
        """INSERT INTO authenticated_collector_audit_log
           (tenant_id, actor_user_id, action, resource_type, resource_id, details)
           VALUES ($1, $2, $3, $4, $5, $6::jsonb)""",
        uuid.UUID(tenant_id), user_id, action, resource_type, resource_id,
        json.dumps(details, ensure_ascii=False),
    )


async def list_site_profiles(tenant_id: str, project_key: str | None = None, enabled: bool | None = None) -> list[dict[str, Any]]:
    args: list[Any] = [uuid.UUID(tenant_id)]
    where = ["p.tenant_id = $1"]
    if project_key:
        args.append(project_key.upper())
        where.append(f"p.project_key = ${len(args)}")
    if enabled is not None:
        args.append(enabled)
        where.append(f"p.enabled = ${len(args)}")
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            f"""SELECT p.*,
                count(a.id) FILTER (WHERE a.enabled) AS account_count,
                count(a.id) FILTER (WHERE a.enabled AND a.login_status = 'connected') AS connected_account_count,
                max(a.last_collected_at) AS last_collected_at
                FROM authenticated_site_profiles p
                LEFT JOIN authenticated_site_accounts a ON a.site_profile_id = p.id AND a.tenant_id = p.tenant_id
                WHERE {' AND '.join(where)} GROUP BY p.id
                ORDER BY p.project_key, p.display_name""", *args,
        )
    return [_row(row) for row in rows]


async def upsert_site_profile(tenant_id: str, user_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    site = normalize_site_profile(payload)
    async with get_pool().acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            """INSERT INTO authenticated_site_profiles
               (tenant_id, project_key, site_key, display_name, base_origin, allowed_origins, runtime,
                data_categories, login_mode, challenge_policy, retention_policy, enabled, metadata, created_by)
               VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7,$8::jsonb,$9,$10::jsonb,$11::jsonb,$12,$13::jsonb,$14)
               ON CONFLICT (tenant_id, project_key, site_key) DO UPDATE SET
                 display_name=EXCLUDED.display_name, base_origin=EXCLUDED.base_origin,
                 allowed_origins=EXCLUDED.allowed_origins, runtime=EXCLUDED.runtime,
                 data_categories=EXCLUDED.data_categories, login_mode=EXCLUDED.login_mode,
                 challenge_policy=EXCLUDED.challenge_policy, retention_policy=EXCLUDED.retention_policy,
                 enabled=EXCLUDED.enabled, metadata=EXCLUDED.metadata, updated_at=NOW()
               RETURNING *""",
            uuid.UUID(tenant_id), site["project_key"], site["site_key"], site["display_name"], site["base_origin"],
            json.dumps(site["allowed_origins"]), site["runtime"], json.dumps(site["data_categories"]), site["login_mode"],
            json.dumps(site["challenge_policy"]), json.dumps(site["retention_policy"]), site["enabled"],
            json.dumps(site["metadata"]), user_id,
        )
        await _audit(conn, tenant_id, user_id, "site_profile.upsert", "site_profile", str(row["id"]), {"project_key": site["project_key"], "site_key": site["site_key"]})
    return _row(row)


async def overview(tenant_id: str) -> dict[str, Any]:
    async with get_pool().acquire() as conn:
        projects = await conn.fetch(
            """SELECT p.project_key, count(DISTINCT p.id) AS site_count,
               count(DISTINCT a.id) FILTER (WHERE a.enabled AND a.login_status='connected') AS active_account_count
               FROM authenticated_site_profiles p LEFT JOIN authenticated_site_accounts a
               ON a.site_profile_id=p.id AND a.tenant_id=p.tenant_id
               WHERE p.tenant_id=$1 GROUP BY p.project_key ORDER BY p.project_key""", uuid.UUID(tenant_id),
        )
        statuses = await conn.fetch(
            """SELECT status, count(*) AS count FROM pc_agent_collection_queue
               WHERE tenant_id=$1 AND queue_type='browser_recipe' GROUP BY status""", uuid.UUID(tenant_id),
        )
    by_status = {str(row["status"]): int(row["count"]) for row in statuses}
    project_rows = [{**_row(row), "site_count": int(row["site_count"]), "active_account_count": int(row["active_account_count"])} for row in projects]
    return {
        "projects": project_rows,
        "totals": {
            "connected_sites": sum(row["site_count"] for row in project_rows),
            "active_accounts": sum(row["active_account_count"] for row in project_rows),
            "running_jobs": by_status.get("running", 0),
            "action_required_jobs": by_status.get("action_required", 0),
            "failed_jobs": by_status.get("failed", 0),
        },
        "job_statuses": by_status,
        "demo": False,
    }


async def create_job(tenant_id: str, user_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    project_key = str(payload.get("project_key") or "").upper()
    recipe_id = str(payload.get("recipe_id") or "").strip()
    work_key = str(payload.get("work_key") or f"collector-{project_key.lower()}-{payload.get('site_key', 'site')}").strip()[:120]
    if project_key not in PROJECT_KEYS or not recipe_id:
        raise ValueError("project_key_and_recipe_id_required")
    site_key = str(payload.get("site_key") or "").strip().lower()
    async with get_pool().acquire() as conn, conn.transaction():
        site = await conn.fetchrow(
            """SELECT * FROM authenticated_site_profiles
               WHERE tenant_id=$1 AND project_key=$2 AND site_key=$3 AND enabled=TRUE""",
            uuid.UUID(tenant_id), project_key, site_key,
        )
        recipe = await conn.fetchrow(
            """SELECT * FROM browser_recipes WHERE tenant_id=$1 AND recipe_id=$2
               AND version=$3 AND project_key=$4 AND service=$5 AND enabled=TRUE AND version_status='active'""",
            uuid.UUID(tenant_id), recipe_id, str(payload.get("recipe_version") or "v1"), project_key, site_key,
        )
        if not site:
            raise LookupError("site_profile_not_found")
        if not recipe:
            raise LookupError("active_recipe_not_found")
        raw_key = f"{tenant_id}|browser_recipe|{project_key}|{site_key}|{work_key}|{uuid.uuid4()}"
        job_key = hashlib.sha256(raw_key.encode()).hexdigest()
        safe_payload = {
            "project_key": project_key, "site_profile_id": str(site["id"]), "recipe_id": recipe_id,
            "recipe_version": str(recipe["version"]), "record_types": _json_list(recipe["record_types"]),
            "target_origin": str(site["base_origin"]), "challenge_mode": "user_intervention_only",
        }
        row = await conn.fetchrow(
            """INSERT INTO pc_agent_collection_queue
               (tenant_id, job_key, queue_type, site_key, service, business_id, work_key, resource_key,
                runtime, priority, min_interval_seconds, latest_only, payload, created_by)
               VALUES ($1,$2,'browser_recipe',$3,$3,$4,$5,$6,$7,$8,0,$9,$10::jsonb,$11) RETURNING *""",
            uuid.UUID(tenant_id), job_key, site_key, project_key, work_key,
            f"{site['runtime']}|{tenant_id}|{project_key}|{site_key}|{work_key}", str(site["runtime"]),
            int(payload.get("priority") or 50), bool(payload.get("latest_only", False)), json.dumps(safe_payload), user_id,
        )
        await _audit(conn, tenant_id, user_id, "job.create", "collection_job", str(row["id"]), {"project_key": project_key, "site_key": site_key, "recipe_id": recipe_id})
    return _row(row)


async def list_jobs(tenant_id: str, project_key: str | None = None, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    args: list[Any] = [uuid.UUID(tenant_id)]
    where = ["tenant_id=$1", "queue_type='browser_recipe'"]
    if project_key:
        args.append(project_key.upper()); where.append(f"payload->>'project_key'=${len(args)}")
    if status:
        if status not in JOB_STATUSES: raise ValueError("unsupported_job_status")
        args.append(status); where.append(f"status=${len(args)}")
    args.append(max(1, min(limit, 200)))
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(f"SELECT * FROM pc_agent_collection_queue WHERE {' AND '.join(where)} ORDER BY updated_at DESC LIMIT ${len(args)}", *args)
    return [_row(row) for row in rows]


async def resume_job(tenant_id: str, user_id: str, job_id: str, resolution: str, note: str = "") -> dict[str, Any] | None:
    if resolution not in {"completed", "relogin_completed", "terms_accepted", "permission_granted", "manual_fallback"}:
        raise ValueError("unsupported_resolution")
    async with get_pool().acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            """UPDATE pc_agent_collection_queue SET status='queued', lease_agent_id='', error_code='',
               message='User action completed; resuming same work session', next_run_at=NOW(), finished_at=NULL,
               payload=payload || $3::jsonb, updated_at=NOW()
               WHERE id=$1 AND tenant_id=$2 AND queue_type='browser_recipe' AND status='action_required'
               RETURNING *""", uuid.UUID(job_id), uuid.UUID(tenant_id),
            json.dumps({"resume_resolution": resolution, "resume_note": note[:500], "resume_same_work_key": True}),
        )
        if row:
            await _audit(conn, tenant_id, user_id, "job.resume", "collection_job", job_id, {"resolution": resolution, "same_work_key": True})
    return _row(row) if row else None
