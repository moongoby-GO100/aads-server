"""Authenticated site collector SaaS control-plane helpers.

This module keeps the product-facing collector layer separate from the lower
level browser recipe and PC Agent queue primitives. It is intentionally safe to
import without a live database so pre-commit and unit tests can run offline.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.services import pc_agent_collection_queue as queue_module
from app.services.browser_recipe_registry import build_recipe_dry_run_plan
from app.services.managed_browser import normalize_origin, normalize_work_key

KST = timezone(timedelta(hours=9))
DATA_DIR = Path(os.getenv("AADS_AUTHENTICATED_SITE_COLLECTOR_DATA_DIR", "app/data/authenticated_site_collector"))
SITE_PROFILE_PATH = Path(
    os.getenv("AADS_AUTHENTICATED_SITE_PROFILES_PATH", str(DATA_DIR / "site_profiles.json"))
)

PROJECT_KEYS = {"AADS", "KIS", "GO100", "SF", "NTV2", "NAS", "CUSTOM"}
SITE_ENVIRONMENTS = {
    "webview2",
    "chrome_extension",
    "chrome_cdp",
    "playwright_server",
    "file_upload",
    "official_api",
    "manual_export",
}
LOGIN_MODES = {"user_session", "agent_vault", "manual_export", "official_api", "none"}
CHALLENGE_POLICIES = {"user_intervention", "manual_export", "deny", "none"}
VERSION_STATUSES = {"draft", "active", "archived"}
ACTIVE_JOB_STATUSES = {"queued", "running", "action_required"}

DEFAULT_SITE_PROFILES: list[dict[str, Any]] = [
    {
        "project_key": "AADS",
        "site_key": "aads.internal_ops",
        "display_name": "AADS internal operations portal",
        "base_origin": "https://aads.newtalk.kr",
        "allowed_origins": ["https://aads.newtalk.kr"],
        "runtime": "playwright_server",
        "data_categories": ["runner_status", "documents", "agent_health"],
        "login_mode": "agent_vault",
        "challenge_policy": "user_intervention",
        "retention_policy": {"days": 90, "artifact_scope": "internal_audit"},
        "account_count": 1,
        "connected_account_count": 1,
        "enabled": True,
        "metadata": {"sample": True, "collector_priority": "internal_first"},
    },
    {
        "project_key": "GO100",
        "site_key": "go100.research_portal",
        "display_name": "GO100 research and finance portal",
        "base_origin": "https://go100.newtalk.kr",
        "allowed_origins": ["https://go100.newtalk.kr"],
        "runtime": "official_api",
        "data_categories": ["market_data", "research", "portfolio"],
        "login_mode": "official_api",
        "challenge_policy": "manual_export",
        "retention_policy": {"days": 365, "artifact_scope": "financial_audit"},
        "account_count": 1,
        "connected_account_count": 0,
        "enabled": True,
        "metadata": {"sample": True, "risk": "financial_terms_high"},
    },
    {
        "project_key": "KIS",
        "site_key": "kis.secure_trading_portal",
        "display_name": "KIS secure trading portal",
        "base_origin": "https://securities.example",
        "allowed_origins": ["https://securities.example"],
        "runtime": "manual_export",
        "data_categories": ["orders", "fills", "statements"],
        "login_mode": "manual_export",
        "challenge_policy": "deny",
        "retention_policy": {"days": 365, "artifact_scope": "regulated_financial"},
        "account_count": 0,
        "connected_account_count": 0,
        "enabled": True,
        "metadata": {"sample": True, "risk": "no_captcha_bypass"},
    },
    {
        "project_key": "SF",
        "site_key": "sf.creator_platform",
        "display_name": "ShortFlow creator platform",
        "base_origin": "https://studio.example",
        "allowed_origins": ["https://studio.example"],
        "runtime": "chrome_extension",
        "data_categories": ["uploads", "ads", "analytics", "downloads"],
        "login_mode": "user_session",
        "challenge_policy": "user_intervention",
        "retention_policy": {"days": 180, "artifact_scope": "campaign_ops"},
        "account_count": 0,
        "connected_account_count": 0,
        "enabled": True,
        "metadata": {"sample": True, "requires_file_downloads": True},
    },
    {
        "project_key": "NTV2",
        "site_key": "ntv2.partner_settlement",
        "display_name": "NewTalk partner settlement portal",
        "base_origin": "https://newtalk.kr",
        "allowed_origins": ["https://newtalk.kr"],
        "runtime": "webview2",
        "data_categories": ["seller_onboarding", "settlement", "reviews"],
        "login_mode": "user_session",
        "challenge_policy": "user_intervention",
        "retention_policy": {"days": 365, "artifact_scope": "tenant_settlement"},
        "account_count": 0,
        "connected_account_count": 0,
        "enabled": True,
        "metadata": {"sample": True, "tenant_isolation": "required"},
    },
    {
        "project_key": "NAS",
        "site_key": "nas.file_processing",
        "display_name": "NAS file processing portal",
        "base_origin": "https://nas.example",
        "allowed_origins": ["https://nas.example"],
        "runtime": "file_upload",
        "data_categories": ["images", "file_hashes", "exports"],
        "login_mode": "manual_export",
        "challenge_policy": "manual_export",
        "retention_policy": {"days": 180, "artifact_scope": "file_processing"},
        "account_count": 0,
        "connected_account_count": 0,
        "enabled": True,
        "metadata": {"sample": True, "hash_verification": "required"},
    },
]


def _now_text() -> str:
    return datetime.now(KST).isoformat(timespec="seconds")


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


def _db_enabled() -> bool:
    return bool(os.getenv("DATABASE_URL") or os.getenv("YEOLJEONG_FINANCE_DATABASE_URL"))


def _clean_text(value: Any, *, default: str = "", max_length: int = 500) -> str:
    text = str(value if value is not None else default).strip()
    return text[:max_length]


def _normalize_enum(value: Any, allowed: set[str], default: str) -> str:
    text = _clean_text(value, default=default).lower()
    return text if text in allowed else default


def _normalize_project_key(value: Any) -> str:
    text = _clean_text(value, default="CUSTOM", max_length=40).upper()
    return text if text in PROJECT_KEYS else "CUSTOM"


def _normalize_site_key(value: Any, *, fallback: str) -> str:
    text = _clean_text(value, default=fallback, max_length=120).lower()
    cleaned = "".join(ch if ch.isalnum() or ch in "._:-" else "-" for ch in text).strip("-")
    return cleaned or fallback


def normalize_site_profile(payload: dict[str, Any]) -> dict[str, Any]:
    project_key = _normalize_project_key(payload.get("project_key"))
    fallback_key = f"{project_key.lower()}.custom"
    site_key = _normalize_site_key(payload.get("site_key"), fallback=fallback_key)
    raw_origins = [str(value) for value in _json_list(payload.get("allowed_origins"))]
    base_origin = normalize_origin(_clean_text(payload.get("base_origin"), max_length=2000))
    allowed_origins = []
    for origin in [base_origin, *raw_origins]:
        normalized = normalize_origin(origin) or origin.strip().rstrip("/")
        if normalized and normalized not in allowed_origins:
            allowed_origins.append(normalized)
    if not base_origin and allowed_origins:
        base_origin = allowed_origins[0]
    runtime = _normalize_enum(payload.get("runtime") or payload.get("site_environment"), SITE_ENVIRONMENTS, "webview2")
    return {
        "id": _clean_text(payload.get("id"), default=str(uuid.uuid4()), max_length=80),
        "project_key": project_key,
        "site_key": site_key,
        "display_name": _clean_text(payload.get("display_name"), default=site_key, max_length=200),
        "base_origin": base_origin,
        "allowed_origins": allowed_origins,
        "runtime": runtime,
        "site_environment": runtime,
        "data_categories": [
            _clean_text(value, max_length=80)
            for value in _json_list(payload.get("data_categories"))
            if _clean_text(value, max_length=80)
        ][:30],
        "login_mode": _normalize_enum(payload.get("login_mode"), LOGIN_MODES, "user_session"),
        "challenge_policy": _normalize_enum(
            payload.get("challenge_policy"), CHALLENGE_POLICIES, "user_intervention"
        ),
        "retention_policy": _json_dict(payload.get("retention_policy")),
        "account_count": int(payload.get("account_count") or 0),
        "connected_account_count": int(payload.get("connected_account_count") or 0),
        "last_collected_at": _clean_text(payload.get("last_collected_at"), max_length=80),
        "enabled": bool(payload.get("enabled", True)),
        "metadata": _json_dict(payload.get("metadata")),
        "created_at": _clean_text(payload.get("created_at"), default=_now_text(), max_length=80),
        "updated_at": _clean_text(payload.get("updated_at"), default=_now_text(), max_length=80),
    }


def normalize_recipe_extension(payload: dict[str, Any]) -> dict[str, Any]:
    project_key = _normalize_project_key(payload.get("project_key"))
    runtime = _normalize_enum(payload.get("site_environment") or payload.get("runtime"), SITE_ENVIRONMENTS, "webview2")
    version_status = _normalize_enum(payload.get("version_status"), VERSION_STATUSES, "draft")
    return {
        "project_key": project_key,
        "site_environment": runtime,
        "record_types": [
            _clean_text(value, max_length=80)
            for value in _json_list(payload.get("record_types"))
            if _clean_text(value, max_length=80)
        ][:50],
        "normalization_schema": _json_dict(payload.get("normalization_schema")),
        "fixture_cases": _json_list(payload.get("fixture_cases"))[:20],
        "version_status": version_status,
    }


def _read_file_profiles() -> list[dict[str, Any]]:
    if not SITE_PROFILE_PATH.exists():
        return []
    try:
        parsed = json.loads(SITE_PROFILE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [normalize_site_profile(item) for item in parsed if isinstance(item, dict)]


def _write_file_profiles(rows: list[dict[str, Any]]) -> None:
    SITE_PROFILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = SITE_PROFILE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(SITE_PROFILE_PATH)


def _profile_matches(profile: dict[str, Any], project_key: str | None) -> bool:
    return not project_key or profile["project_key"] == _normalize_project_key(project_key)


async def _fetch_db_profiles(tenant_id: str, project_key: str | None = None) -> list[dict[str, Any]] | None:
    if not _db_enabled():
        return None
    try:
        from app.core.db_pool import get_pool, init_pool

        try:
            pool = get_pool()
        except RuntimeError:
            pool = await init_pool()
        args: list[Any] = [uuid.UUID(str(tenant_id))]
        where = ["tenant_id = $1"]
        if project_key:
            args.append(_normalize_project_key(project_key))
            where.append(f"project_key = ${len(args)}")
        async with pool.acquire() as conn:
            exists = await conn.fetchval("SELECT to_regclass('public.authenticated_site_profiles')")
            if not exists:
                return None
            rows = await conn.fetch(
                f"""
                SELECT id::text, project_key, site_key, display_name, base_origin,
                       allowed_origins, runtime, data_categories, login_mode,
                       challenge_policy, retention_policy, account_count,
                       connected_account_count, last_collected_at, enabled,
                       metadata, created_at, updated_at
                  FROM authenticated_site_profiles
                 WHERE {' AND '.join(where)}
                 ORDER BY project_key, display_name, site_key
                """,
                *args,
            )
    except Exception:
        return None
    return [_row_to_profile(row) for row in rows]


def _row_to_profile(row: Any) -> dict[str, Any]:
    item = dict(row)
    for key in ("allowed_origins", "data_categories"):
        item[key] = _json_list(item.get(key))
    for key in ("retention_policy", "metadata"):
        item[key] = _json_dict(item.get(key))
    for key in ("last_collected_at", "created_at", "updated_at"):
        if item.get(key):
            item[key] = item[key].isoformat()
    return normalize_site_profile(item)


async def list_site_profiles(*, tenant_id: str, project_key: str | None = None) -> dict[str, Any]:
    db_profiles = await _fetch_db_profiles(tenant_id, project_key)
    if db_profiles is not None:
        return {"sites": db_profiles, "count": len(db_profiles), "demo": False}

    file_profiles = _read_file_profiles()
    if file_profiles:
        filtered = [profile for profile in file_profiles if _profile_matches(profile, project_key)]
        return {"sites": filtered, "count": len(filtered), "demo": False}

    fallback = [normalize_site_profile(item) for item in DEFAULT_SITE_PROFILES]
    filtered = [profile for profile in fallback if _profile_matches(profile, project_key)]
    return {"sites": filtered, "count": len(filtered), "demo": True}


async def upsert_site_profile(*, tenant_id: str, user_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    profile = normalize_site_profile(payload)
    db_profile = await _upsert_db_profile(tenant_id=tenant_id, user_id=user_id, profile=profile)
    if db_profile:
        return db_profile
    rows = _read_file_profiles()
    existing = next(
        (row for row in rows if row["project_key"] == profile["project_key"] and row["site_key"] == profile["site_key"]),
        None,
    )
    if existing:
        existing.update({**profile, "id": existing["id"], "created_at": existing["created_at"], "updated_at": _now_text()})
        saved = existing
    else:
        rows.append(profile)
        saved = profile
    _write_file_profiles(rows)
    return saved


async def _upsert_db_profile(*, tenant_id: str, user_id: str, profile: dict[str, Any]) -> dict[str, Any] | None:
    if not _db_enabled():
        return None
    try:
        from app.core.db_pool import get_pool, init_pool

        try:
            pool = get_pool()
        except RuntimeError:
            pool = await init_pool()
        async with pool.acquire() as conn:
            exists = await conn.fetchval("SELECT to_regclass('public.authenticated_site_profiles')")
            if not exists:
                return None
            row = await conn.fetchrow(
                """
                INSERT INTO authenticated_site_profiles (
                    tenant_id, project_key, site_key, display_name, base_origin,
                    allowed_origins, runtime, data_categories, login_mode,
                    challenge_policy, retention_policy, account_count,
                    connected_account_count, enabled, metadata, created_by,
                    updated_at
                )
                VALUES (
                    $1, $2, $3, $4, $5, $6::jsonb, $7, $8::jsonb, $9,
                    $10, $11::jsonb, $12, $13, $14, $15::jsonb, $16,
                    NOW()
                )
                ON CONFLICT (tenant_id, project_key, site_key) DO UPDATE
                   SET display_name = EXCLUDED.display_name,
                       base_origin = EXCLUDED.base_origin,
                       allowed_origins = EXCLUDED.allowed_origins,
                       runtime = EXCLUDED.runtime,
                       data_categories = EXCLUDED.data_categories,
                       login_mode = EXCLUDED.login_mode,
                       challenge_policy = EXCLUDED.challenge_policy,
                       retention_policy = EXCLUDED.retention_policy,
                       account_count = EXCLUDED.account_count,
                       connected_account_count = EXCLUDED.connected_account_count,
                       enabled = EXCLUDED.enabled,
                       metadata = EXCLUDED.metadata,
                       updated_at = NOW()
                RETURNING *
                """,
                uuid.UUID(str(tenant_id)),
                profile["project_key"],
                profile["site_key"],
                profile["display_name"],
                profile["base_origin"],
                json.dumps(profile["allowed_origins"], ensure_ascii=False),
                profile["runtime"],
                json.dumps(profile["data_categories"], ensure_ascii=False),
                profile["login_mode"],
                profile["challenge_policy"],
                json.dumps(profile["retention_policy"], ensure_ascii=False),
                profile["account_count"],
                profile["connected_account_count"],
                profile["enabled"],
                json.dumps(profile["metadata"], ensure_ascii=False),
                user_id,
            )
    except Exception:
        return None
    return _row_to_profile(row)


async def collector_overview(*, tenant_id: str) -> dict[str, Any]:
    profile_result = await list_site_profiles(tenant_id=tenant_id)
    sites = profile_result["sites"]
    jobs = list_jobs(project_key=None, status=None, limit=200)["jobs"]
    status_counts: dict[str, int] = {}
    for job in jobs:
        status = str(job.get("status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1

    projects: list[dict[str, Any]] = []
    for project_key in sorted({site["project_key"] for site in sites} | PROJECT_KEYS):
        project_sites = [site for site in sites if site["project_key"] == project_key]
        if not project_sites and project_key != "CUSTOM":
            continue
        projects.append(
            {
                "project_key": project_key,
                "site_count": len(project_sites),
                "active_account_count": sum(int(site.get("connected_account_count") or 0) for site in project_sites),
            }
        )
    return {
        "projects": projects,
        "totals": {
            "connected_sites": len([site for site in sites if site.get("enabled")]),
            "active_accounts": sum(int(site.get("connected_account_count") or 0) for site in sites),
            "running_jobs": status_counts.get("running", 0),
            "action_required_jobs": status_counts.get("action_required", 0),
            "failed_jobs": status_counts.get("failed", 0),
        },
        "job_statuses": status_counts,
        "demo": bool(profile_result.get("demo")),
    }


def _job_out(item: dict[str, Any]) -> dict[str, Any]:
    payload = _json_dict(item.get("payload"))
    return {
        "id": str(item.get("id") or ""),
        "status": str(item.get("status") or "queued"),
        "site_key": str(item.get("site_key") or ""),
        "work_key": str(item.get("work_key") or ""),
        "runtime": str(item.get("runtime") or "pc_agent"),
        "error_code": str(item.get("error_code") or ""),
        "message": str(item.get("message") or ""),
        "updated_at": str(item.get("updated_at") or _now_text()),
        "created_at": str(item.get("created_at") or ""),
        "payload": payload,
        "queue_type": str(item.get("queue_type") or ""),
        "same_work_key": True,
    }


def list_jobs(*, project_key: str | None = None, status: str | None = None, limit: int = 50) -> dict[str, Any]:
    project = _normalize_project_key(project_key) if project_key else None
    rows = queue_module.queue_snapshot(limit=max(1, min(int(limit), 200)))
    jobs = []
    for row in rows:
        if row.get("queue_type") != "browser_recipe":
            continue
        payload = _json_dict(row.get("payload"))
        if project and _normalize_project_key(payload.get("project_key")) != project:
            continue
        if status and row.get("status") != status:
            continue
        jobs.append(_job_out(row))
    return {"jobs": jobs, "count": len(jobs)}


async def create_collection_job(*, tenant_id: str, user_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    project_key = _normalize_project_key(payload.get("project_key"))
    site_key = _normalize_site_key(payload.get("site_key"), fallback=f"{project_key.lower()}.custom")
    profiles = await list_site_profiles(tenant_id=tenant_id, project_key=project_key)
    profile = next((item for item in profiles["sites"] if item["site_key"] == site_key), None)
    if not profile:
        raise ValueError("site_profile_not_found")
    recipe_id = _clean_text(payload.get("recipe_id"), default=f"{site_key}.collect", max_length=200)
    recipe_version = _clean_text(payload.get("recipe_version"), default="v1", max_length=80)
    work_key = normalize_work_key(
        _clean_text(payload.get("work_key"), default=f"{project_key.lower()}-{site_key}", max_length=120)
    )
    item = queue_module.enqueue_collection_item(
        {
            "tenant_id": tenant_id,
            "queue_type": "browser_recipe",
            "site_key": site_key,
            "service": site_key,
            "business_id": project_key,
            "branch": profile["display_name"],
            "work_key": work_key,
            "runtime": profile["runtime"],
            "priority": int(payload.get("priority") or 50),
            "latest_only": bool(payload.get("latest_only", True)),
            "payload": {
                "project_key": project_key,
                "recipe_id": recipe_id,
                "recipe_version": recipe_version,
                "site_environment": profile["runtime"],
                "allowed_origins": profile["allowed_origins"],
                "challenge_policy": profile["challenge_policy"],
                "created_by": user_id,
            },
            "created_by": user_id,
        }
    )
    return {"status": "created", "job": _job_out(item)}


def resume_collection_job(*, job_id: str, resolution: str, note: str = "") -> dict[str, Any] | None:
    item = queue_module.complete_collection_item(
        job_id,
        status="queued",
        result={"resolution": _clean_text(resolution, max_length=80), "note": _clean_text(note, max_length=1000)},
        error_code="",
        message="User intervention completed; queued for same work_key resume.",
        next_run_at=_now_text(),
    )
    if not item:
        return None
    return {"status": "queued", "same_work_key": True, "job": _job_out(item)}


def build_collector_recipe_dry_run(
    recipe: dict[str, Any],
    *,
    target_url: str = "",
    project_key: str | None = None,
    site_environment: str | None = None,
) -> dict[str, Any]:
    extension = normalize_recipe_extension(
        {**recipe, "project_key": project_key or recipe.get("project_key"), "site_environment": site_environment or recipe.get("site_environment")}
    )
    base_recipe = {
        **recipe,
        "resource_policy": {
            **_json_dict(recipe.get("resource_policy")),
            "runtime": "pc_agent" if extension["site_environment"] == "webview2" else extension["site_environment"],
        },
    }
    plan = build_recipe_dry_run_plan(base_recipe, target_url=target_url)
    return {**plan, "saas_extension": extension}
