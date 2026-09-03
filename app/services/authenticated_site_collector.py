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
from app.services.auth_challenge_orchestrator import make_resume_token
from app.services.browser_permission_policy import mask_sensitive_value
from app.services.browser_recipe_registry import build_recipe_dry_run_plan
from app.services.managed_browser import normalize_origin, normalize_work_key

KST = timezone(timedelta(hours=9))
DATA_DIR = Path(os.getenv("AADS_AUTHENTICATED_SITE_COLLECTOR_DATA_DIR", "app/data/authenticated_site_collector"))
SITE_PROFILE_PATH = Path(
    os.getenv("AADS_AUTHENTICATED_SITE_PROFILES_PATH", str(DATA_DIR / "site_profiles.json"))
)

PROJECT_KEYS = {
    "AADS",
    "KIS",
    "GO100",
    "SF",
    "NTV2",
    "NAS",
    "STORE_ASSISTANT",
    "MARKETING",
    "BANKING",
    "CUSTOM",
}
SITE_ENVIRONMENTS = {
    "webview2",
    "windows_collector",
    "chrome_extension",
    "chrome_cdp",
    "playwright_server",
    "file_upload",
    "official_api",
    "manual_export",
}
LOGIN_MODES = {"user_session", "agent_vault", "manual_export", "official_api", "none"}
CHALLENGE_POLICIES = {"user_intervention", "manual_export", "deny", "none"}
CHALLENGE_KINDS = {"captcha", "otp", "identity_check", "certificate", "terms", "permission", "login"}
RESUME_RESOLUTIONS = {
    "user_completed",
    "user_input_completed",
    "user_approved_automation",
    "manual_export_uploaded",
    "approved_same_session",
    "skip_optional_step",
    "completed",
}
VERSION_STATUSES = {"draft", "active", "archived"}
ACTIVE_JOB_STATUSES = {"queued", "running", "action_required"}
LOCAL_WINDOWS_SITE_ENVIRONMENTS = {"webview2", "windows_collector"}
CHALLENGE_HOLD_UNTIL = "2099-01-01T00:00:00+09:00"
PHYSICAL_INPUT_CHALLENGE_KINDS = {"otp", "identity_check", "certificate"}
FINANCIAL_PROJECT_KEYS = {"BANKING"}
FINANCIAL_DATA_CATEGORIES = {"transactions", "balances", "statements", "card_usage", "approvals"}
FINANCIAL_SITE_MARKERS = ("bank", "banking", "card", "shinhan")
WINDOWS_COLLECTOR_CONTRACT_VERSION = "windows_collector_v1"
FINANCIAL_EXCLUSIVE_JOB_TYPE = "financial_exclusive"

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
        "project_key": "STORE_ASSISTANT",
        "site_key": "baemin.owner",
        "display_name": "Baemin owner portal",
        "base_origin": "https://self.baemin.com/login",
        "allowed_origins": ["https://self.baemin.com"],
        "runtime": "webview2",
        "data_categories": ["orders", "reviews", "settlements", "ads"],
        "login_mode": "user_session",
        "challenge_policy": "user_intervention",
        "retention_policy": {"days": 365, "artifact_scope": "store_assistant_ops"},
        "account_count": 0,
        "connected_account_count": 0,
        "enabled": True,
        "metadata": {"sample": True, "runtime_contract": "windows_collector_v1"},
    },
    {
        "project_key": "MARKETING",
        "site_key": "meta.business",
        "display_name": "Meta Business",
        "base_origin": "https://business.facebook.com",
        "allowed_origins": ["https://business.facebook.com", "https://www.facebook.com"],
        "runtime": "webview2",
        "data_categories": ["ads", "campaigns", "insights"],
        "login_mode": "user_session",
        "challenge_policy": "user_intervention",
        "retention_policy": {"days": 365, "artifact_scope": "marketing_ops"},
        "account_count": 0,
        "connected_account_count": 0,
        "enabled": True,
        "metadata": {"sample": True, "runtime_contract": "windows_collector_v1"},
    },
    {
        "project_key": "BANKING",
        "site_key": "shinhan.easyview",
        "display_name": "Shinhan easy inquiry",
        "base_origin": "https://bizbank.shinhan.com",
        "allowed_origins": ["https://bizbank.shinhan.com", "https://bank.shinhan.com"],
        "runtime": "windows_collector",
        "data_categories": ["transactions", "balances", "statements"],
        "login_mode": "user_session",
        "challenge_policy": "user_intervention",
        "retention_policy": {"days": 1825, "artifact_scope": "banking_audit"},
        "account_count": 0,
        "connected_account_count": 0,
        "enabled": True,
        "metadata": {
            "sample": True,
            "runtime_contract": "windows_collector_v1",
            "requires_local_security_programs": True,
        },
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


def _normalize_challenge_policy(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        policy = {str(key)[:80]: val for key, val in value.items() if str(key).strip()}
        mode = _normalize_enum(policy.get("mode"), CHALLENGE_POLICIES, "user_intervention")
        return _safe_challenge_policy({**policy, "mode": mode})
    mode = _normalize_enum(value, CHALLENGE_POLICIES, "user_intervention")
    return _safe_challenge_policy({"mode": mode})


def _safe_challenge_policy(policy: dict[str, Any]) -> dict[str, Any]:
    """Collector-level safety contract. Challenge values are never persisted."""
    mode = _normalize_enum(policy.get("mode"), CHALLENGE_POLICIES, "user_intervention")
    challenge_kinds = [
        _normalize_enum(value, CHALLENGE_KINDS, "captcha")
        for value in _json_list(policy.get("challenge_kinds"))
    ]
    allowed_resolutions = [
        _normalize_enum(value, RESUME_RESOLUTIONS, "user_completed")
        for value in _json_list(policy.get("allowed_resolutions"))
    ]
    safe_policy = {
        **mask_sensitive_value(policy),
        "mode": mode,
        "auto_bypass_allowed": False,
        "user_approved_automation_allowed": bool(policy.get("user_approved_automation_allowed", True))
        and mode == "user_intervention",
        "responsibility_acceptance_required": True,
        "resume_strategy": "same_work_key_after_user_intervention",
        "challenge_kinds": sorted(set(challenge_kinds or CHALLENGE_KINDS)),
        "allowed_resolutions": sorted(set(allowed_resolutions or RESUME_RESOLUTIONS)),
        "stores_challenge_values": False,
    }
    safe_policy.pop("approved_input", None)
    return safe_policy


def _normalize_project_key(value: Any) -> str:
    text = _clean_text(value, default="CUSTOM", max_length=40).upper()
    return text if text in PROJECT_KEYS else "CUSTOM"


def resolve_collector_execution_runtime(site_environment: Any) -> str:
    runtime = _normalize_enum(site_environment, SITE_ENVIRONMENTS, "webview2")
    return "pc_agent" if runtime in LOCAL_WINDOWS_SITE_ENVIRONMENTS else runtime


def _is_financial_profile(profile: dict[str, Any]) -> bool:
    project_key = _normalize_project_key(profile.get("project_key"))
    site_key = str(profile.get("site_key") or "").strip().lower()
    categories = {
        str(value or "").strip().lower()
        for value in _json_list(profile.get("data_categories") or profile.get("record_types"))
        if str(value or "").strip()
    }
    return (
        project_key in FINANCIAL_PROJECT_KEYS
        or bool(categories & FINANCIAL_DATA_CATEGORIES)
        or any(marker in site_key for marker in FINANCIAL_SITE_MARKERS)
    )


def collector_runtime_contract_for_profile(profile: dict[str, Any]) -> dict[str, Any]:
    runtime = _normalize_enum(profile.get("runtime") or profile.get("site_environment"), SITE_ENVIRONMENTS, "webview2")
    execution_runtime = resolve_collector_execution_runtime(runtime)
    financial = _is_financial_profile(profile)
    if financial:
        return {
            "contract_version": WINDOWS_COLLECTOR_CONTRACT_VERSION,
            "execution_runtime": execution_runtime,
            "local_runtime": runtime,
            "job_type": FINANCIAL_EXCLUSIVE_JOB_TYPE,
            "lease_policy": {
                "scope": "pc_agent_interactive_browser_lane",
                "exclusive": True,
                "max_concurrency_per_agent": 1,
                "queue_if_busy": True,
                "wait_for_turn": True,
                "reason": "financial_sites_share_security_modules_and_physical_input",
            },
            "throughput_policy": {
                "site_parallelism": 1,
                "recommended_pc_agent_pool": "one_windows_collector_per_busy_financial_site_group",
                "poll_interval_seconds": 60,
                "stale_session_recovery": "close_certificate_or_stale_tabs_then_resume_same_work_key",
            },
            "success_contract": {
                "record_artifact_required": True,
                "minimum_imported_rows": 1,
                "allow_no_records_terminal_state": True,
                "required_stage_logs": [
                    "security_program_check",
                    "login_page_open",
                    "id_password_login",
                    "transaction_page_open",
                    "transaction_capture",
                    "ledger_persist",
                ],
            },
        }
    return {
        "contract_version": WINDOWS_COLLECTOR_CONTRACT_VERSION if runtime in LOCAL_WINDOWS_SITE_ENVIRONMENTS else "server_collector_v1",
        "execution_runtime": execution_runtime,
        "local_runtime": runtime,
        "job_type": "browser_bridge",
        "lease_policy": {
            "scope": "pc_agent_interactive_browser_lane",
            "exclusive": False,
            "max_concurrency_per_agent": 1,
            "queue_if_busy": True,
            "wait_for_turn": True,
        },
        "throughput_policy": {
            "site_parallelism": 1,
            "recommended_pc_agent_pool": "scale_by_additional_windows_collector_pc_agents",
            "poll_interval_seconds": 120,
        },
        "success_contract": {
            "record_artifact_required": True,
            "minimum_imported_rows": 0,
            "allow_no_records_terminal_state": True,
        },
    }


def _challenge_resolution_contract(kind: str, policy: dict[str, Any]) -> dict[str, Any]:
    requires_physical_input = kind in PHYSICAL_INPUT_CHALLENGE_KINDS
    user_approved_automation_allowed = bool(policy.get("user_approved_automation_allowed")) and not requires_physical_input
    return {
        "auto_bypass_allowed": False,
        "user_approved_automation_allowed": user_approved_automation_allowed,
        "requires_user_approval": True,
        "responsibility_acceptance_required": user_approved_automation_allowed,
        "requires_user_physical_input": requires_physical_input,
        "challenge_values_persisted": False,
        "same_work_key_required": True,
        "resume_strategy": "same_work_key_after_user_intervention",
        "automation_resolution": "user_approved_automation" if user_approved_automation_allowed else "",
        "physical_input_resolution": "user_input_completed" if requires_physical_input else "",
    }


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
    metadata = _json_dict(payload.get("metadata"))
    if runtime in LOCAL_WINDOWS_SITE_ENVIRONMENTS and "runtime_contract" not in metadata:
        metadata["runtime_contract"] = WINDOWS_COLLECTOR_CONTRACT_VERSION
    profile = {
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
        "challenge_policy": _normalize_challenge_policy(payload.get("challenge_policy")),
        "retention_policy": _json_dict(payload.get("retention_policy")),
        "account_count": int(payload.get("account_count") or 0),
        "connected_account_count": int(payload.get("connected_account_count") or 0),
        "last_collected_at": _clean_text(payload.get("last_collected_at"), max_length=80),
        "enabled": bool(payload.get("enabled", True)),
        "metadata": metadata,
        "created_at": _clean_text(payload.get("created_at"), default=_now_text(), max_length=80),
        "updated_at": _clean_text(payload.get("updated_at"), default=_now_text(), max_length=80),
    }
    if "runtime_contract_detail" not in metadata:
        metadata["runtime_contract_detail"] = collector_runtime_contract_for_profile(profile)
    return profile


def normalize_recipe_extension(payload: dict[str, Any]) -> dict[str, Any]:
    project_key = _normalize_project_key(payload.get("project_key"))
    runtime = _normalize_enum(payload.get("site_environment") or payload.get("runtime"), SITE_ENVIRONMENTS, "webview2")
    execution_runtime = resolve_collector_execution_runtime(runtime)
    version_status = _normalize_enum(payload.get("version_status"), VERSION_STATUSES, "draft")
    profile_like = {
        "project_key": project_key,
        "site_key": payload.get("site_key") or payload.get("service") or payload.get("recipe_id") or "",
        "runtime": runtime,
        "data_categories": payload.get("record_types") or payload.get("data_categories") or [],
    }
    runtime_contract = collector_runtime_contract_for_profile(profile_like)
    return {
        "project_key": project_key,
        "site_environment": runtime,
        "execution_runtime": execution_runtime,
        "runtime_contract": runtime_contract,
        "lease_policy": runtime_contract["lease_policy"],
        "throughput_policy": runtime_contract["throughput_policy"],
        "success_contract": runtime_contract["success_contract"],
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
        where = ["p.tenant_id = $1"]
        if project_key:
            args.append(_normalize_project_key(project_key))
            where.append(f"p.project_key = ${len(args)}")
        async with pool.acquire() as conn:
            exists = await conn.fetchval("SELECT to_regclass('public.authenticated_site_profiles')")
            if not exists:
                return None
            rows = await conn.fetch(
                f"""
                SELECT p.id::text, p.project_key, p.site_key, p.display_name, p.base_origin,
                       p.allowed_origins, p.runtime, p.data_categories, p.login_mode,
                       p.challenge_policy, p.retention_policy,
                       count(a.id) FILTER (WHERE a.enabled) AS account_count,
                       count(a.id) FILTER (WHERE a.enabled AND a.login_status = 'connected') AS connected_account_count,
                       max(a.last_collected_at) AS last_collected_at,
                       p.enabled, p.metadata, p.created_at, p.updated_at
                  FROM authenticated_site_profiles p
                  LEFT JOIN authenticated_site_accounts a
                    ON a.site_profile_id = p.id AND a.tenant_id = p.tenant_id
                 WHERE {' AND '.join(where)}
                 GROUP BY p.id
                 ORDER BY p.project_key, p.display_name, p.site_key
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
    for key in ("challenge_policy", "retention_policy", "metadata"):
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
                    challenge_policy, retention_policy, enabled, metadata, created_by,
                    updated_at
                )
                VALUES (
                    $1, $2, $3, $4, $5, $6::jsonb, $7, $8::jsonb, $9,
                    $10::jsonb, $11::jsonb, $12, $13::jsonb, $14,
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
                json.dumps(profile["challenge_policy"], ensure_ascii=False),
                json.dumps(profile["retention_policy"], ensure_ascii=False),
                profile["enabled"],
                json.dumps(profile["metadata"], ensure_ascii=False),
                user_id,
            )
            audit_exists = await conn.fetchval("SELECT to_regclass('public.authenticated_collector_audit_log')")
            if audit_exists:
                await conn.execute(
                    """
                    INSERT INTO authenticated_collector_audit_log
                        (tenant_id, actor_user_id, action, resource_type, resource_id, details)
                    VALUES ($1, $2, 'site_profile.upsert', 'site_profile', $3, $4::jsonb)
                    """,
                    uuid.UUID(str(tenant_id)),
                    user_id,
                    str(row["id"]),
                    json.dumps(
                        {"project_key": profile["project_key"], "site_key": profile["site_key"]},
                        ensure_ascii=False,
                    ),
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
        "runtime_contracts": {
            "windows_collector": {
                "contract_version": WINDOWS_COLLECTOR_CONTRACT_VERSION,
                "execution_runtime": "pc_agent",
                "supported_projects": sorted(FINANCIAL_PROJECT_KEYS | {"STORE_ASSISTANT", "MARKETING"}),
                "financial_job_type": FINANCIAL_EXCLUSIVE_JOB_TYPE,
                "financial_max_concurrency_per_pc": 1,
                "general_site_parallelism_per_pc": 1,
            },
            "financial_realtime_strategy": {
                "primary_runtime": "windows_collector",
                "lease_policy": "financial_sites_run_on_exclusive_interactive_browser_lane",
                "scaling_unit": "additional_windows_collector_pc_agent",
                "completion_condition": "ledger_created_and_imported_rows_gt_0_or_verified_no_records",
            },
        },
        "demo": bool(profile_result.get("demo")),
    }


def _job_out(item: dict[str, Any]) -> dict[str, Any]:
    payload = _json_dict(item.get("payload"))
    result = _json_dict(item.get("result"))
    runtime_contract = _json_dict(payload.get("runtime_contract"))
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
        "result": mask_sensitive_value(result),
        "challenge": mask_sensitive_value(_json_dict(result.get("challenge_gate"))),
        "runtime_contract": runtime_contract,
        "lease_policy": _json_dict(payload.get("lease_policy") or runtime_contract.get("lease_policy")),
        "throughput_policy": _json_dict(payload.get("throughput_policy") or runtime_contract.get("throughput_policy")),
        "success_contract": _json_dict(payload.get("success_contract") or runtime_contract.get("success_contract")),
        "queue_type": str(item.get("queue_type") or ""),
        "same_work_key": True,
    }


def _find_job(job_id: str) -> dict[str, Any] | None:
    return next((row for row in queue_module.queue_snapshot(limit=200) if str(row.get("id")) == str(job_id)), None)


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
    raw_work_key = _clean_text(payload.get("work_key"), max_length=120)
    work_key = normalize_work_key(raw_work_key or f"{project_key.lower()}-{site_key}")
    runtime_contract = collector_runtime_contract_for_profile(profile)
    execution_runtime = runtime_contract["execution_runtime"]
    item = queue_module.enqueue_collection_item(
        {
            "tenant_id": tenant_id,
            "queue_type": "browser_recipe",
            "site_key": site_key,
            "service": site_key,
            "business_id": project_key,
            "branch": profile["display_name"],
            "work_key": work_key,
            "runtime": execution_runtime,
            "priority": int(payload.get("priority") or 50),
            "latest_only": bool(payload.get("latest_only", True)),
            "payload": {
                "project_key": project_key,
                "site_key": site_key,
                "recipe_id": recipe_id,
                "recipe_version": recipe_version,
                "site_environment": profile["runtime"],
                "execution_runtime": execution_runtime,
                "allowed_origins": profile["allowed_origins"],
                "challenge_policy": profile["challenge_policy"],
                "runtime_contract": runtime_contract,
                "lease_policy": runtime_contract["lease_policy"],
                "throughput_policy": runtime_contract["throughput_policy"],
                "success_contract": runtime_contract["success_contract"],
                "job_type": runtime_contract["job_type"],
                "record_types": profile["data_categories"],
                "intervention_contract": {
                    "auto_bypass_allowed": False,
                    "user_approved_automation_allowed": profile["challenge_policy"].get(
                        "user_approved_automation_allowed",
                        False,
                    ),
                    "responsibility_acceptance_required": True,
                    "challenge_values_persisted": False,
                    "requires_user_approval": True,
                    "resume_strategy": "same_work_key_after_user_intervention",
                    "allowed_challenge_kinds": profile["challenge_policy"]["challenge_kinds"],
                },
                "created_by": user_id,
            },
            "created_by": user_id,
        }
    )
    return {"status": "created", "job": _job_out(item)}


def mark_collection_job_action_required(
    *,
    job_id: str,
    challenge_kind: str,
    page_url: str = "",
    message: str = "",
    evidence: list[str] | None = None,
    approval_scope: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    existing = _find_job(job_id)
    if not existing:
        return None
    kind = _normalize_enum(challenge_kind, CHALLENGE_KINDS, "captcha")
    payload = _json_dict(existing.get("payload"))
    policy = _normalize_challenge_policy(payload.get("challenge_policy"))
    if policy["mode"] == "deny":
        item = queue_module.complete_collection_item(
            job_id,
            status="failed",
            result={
                "challenge_gate": {
                    "kind": kind,
                    "auto_bypass_allowed": False,
                    "requires_user_intervention": True,
                    "blocked_by_policy": True,
                }
            },
            error_code="COLLECTOR_CHALLENGE_BLOCKED_BY_POLICY",
            message="Challenge automation is blocked by this site profile policy.",
        )
        return {"status": "failed", "same_work_key": True, "job": _job_out(item)} if item else None

    gate = {
        "kind": kind,
        "page_url": _clean_text(page_url, max_length=2000),
        "evidence": [_clean_text(value, max_length=120) for value in (evidence or [])][:5],
        "approval_scope": mask_sensitive_value(approval_scope or {}),
        "resume_token": make_resume_token(
            str(existing.get("work_key") or ""),
            str(existing.get("id") or ""),
            str(existing.get("attempt_count") or "0"),
        ),
        "auto_bypass_allowed": False,
        "challenge_values_persisted": False,
        "requires_user_intervention": True,
        "allowed_resolutions": policy["allowed_resolutions"],
        **_challenge_resolution_contract(kind, policy),
    }
    item = queue_module.complete_collection_item(
        job_id,
        status="action_required",
        result={"challenge_gate": gate},
        error_code=f"COLLECTOR_{kind.upper()}_USER_ACTION_REQUIRED",
        message=_clean_text(
            message,
            default="User must complete the portal challenge in the active session before automation resumes.",
            max_length=1000,
        ),
        next_run_at=CHALLENGE_HOLD_UNTIL,
    )
    if not item:
        return None
    return {"status": "action_required", "same_work_key": True, "job": _job_out(item)}


def resume_collection_job(
    *,
    job_id: str,
    resolution: str,
    note: str = "",
    responsibility_accepted: bool = False,
    physical_input_completed: bool = False,
) -> dict[str, Any] | None:
    existing = _find_job(job_id)
    if not existing:
        return None
    if existing.get("status") != "action_required":
        raise ValueError("collector_job_not_action_required")
    normalized_resolution = _normalize_enum(resolution, RESUME_RESOLUTIONS, "user_completed")
    previous_result = _json_dict(existing.get("result"))
    challenge_gate = _json_dict(previous_result.get("challenge_gate"))
    kind = _normalize_enum(challenge_gate.get("kind"), CHALLENGE_KINDS, "captcha")
    if normalized_resolution == "user_approved_automation":
        if kind in PHYSICAL_INPUT_CHALLENGE_KINDS or not challenge_gate.get("user_approved_automation_allowed"):
            raise ValueError("collector_user_approved_automation_not_allowed_for_challenge")
        if not responsibility_accepted:
            raise ValueError("collector_responsibility_acceptance_required")
    elif kind in PHYSICAL_INPUT_CHALLENGE_KINDS and not physical_input_completed:
        raise ValueError("collector_physical_input_completion_required")

    resumed_by_user = normalized_resolution != "user_approved_automation"
    item = queue_module.complete_collection_item(
        job_id,
        status="queued",
        result=mask_sensitive_value(
            {
                "resolution": normalized_resolution,
                "note": _clean_text(note, max_length=1000),
                "responsibility_accepted": bool(responsibility_accepted),
                "physical_input_completed": bool(physical_input_completed),
                "challenge_gate": {
                    **challenge_gate,
                    "resolved_by_user": resumed_by_user,
                    "approved_automation_requested": normalized_resolution == "user_approved_automation",
                    "responsibility_accepted": bool(responsibility_accepted),
                    "physical_input_completed": bool(physical_input_completed),
                    "resolved_at": _now_text(),
                    "challenge_values_persisted": False,
                    "auto_bypass_allowed": False,
                },
            }
        ),
        error_code="",
        message=(
            "User approved responsible automation; queued for same work_key resume without storing challenge values."
            if normalized_resolution == "user_approved_automation"
            else "User physical intervention completed; queued for same work_key resume without storing OTP/CAPTCHA values."
        ),
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
            "runtime": extension["execution_runtime"],
            "job_type": extension["runtime_contract"]["job_type"],
            "lease_policy": extension["lease_policy"],
        },
        "concurrency_policy": {
            **_json_dict(recipe.get("concurrency_policy")),
            **extension["throughput_policy"],
        },
        "verifier": {
            **_json_dict(recipe.get("verifier")),
            "success_contract": extension["success_contract"],
        },
    }
    plan = build_recipe_dry_run_plan(base_recipe, target_url=target_url)
    return {**plan, "saas_extension": extension}
