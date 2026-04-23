from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import asyncpg

from app.core.db_pool import get_pool
from app.services.model_registry import normalize_provider

logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))
_PENDING_PIPELINE_STATUSES = {"queued", "pending", "created", "awaiting_approval"}

_PROVIDER_DISPLAY_NAMES = {
    "anthropic": "Anthropic",
    "codex": "Codex CLI",
    "deepseek": "DeepSeek",
    "gemini": "Gemini",
    "groq": "Groq",
    "kimi": "Kimi",
    "litellm": "LiteLLM Proxy",
    "minimax": "MiniMax",
    "openai": "OpenAI",
    "openrouter": "OpenRouter",
    "qwen": "Qwen / DashScope",
}


def _iso(dt: datetime | None) -> str | None:
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(KST).isoformat()


def _new_usage(scope: str) -> dict[str, Any]:
    return {
        "scope": scope,
        "calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cost_usd": 0.0,
        "error_runs": 0,
        "_models": set(),
    }


def _finalize_usage(usage: dict[str, Any]) -> dict[str, Any]:
    return {
        "scope": usage["scope"],
        "calls": int(usage["calls"]),
        "input_tokens": int(usage["input_tokens"]),
        "output_tokens": int(usage["output_tokens"]),
        "total_tokens": int(usage["total_tokens"]),
        "cost_usd": round(float(usage["cost_usd"]), 6),
        "error_runs": int(usage["error_runs"]),
        "models": sorted(usage["_models"]),
    }


def _append_exact_usage(usage: dict[str, Any], row: dict[str, Any]) -> None:
    input_tokens = int(row.get("input_tokens") or 0)
    output_tokens = int(row.get("output_tokens") or 0)
    usage["calls"] += 1
    usage["input_tokens"] += input_tokens
    usage["output_tokens"] += output_tokens
    usage["total_tokens"] += input_tokens + output_tokens
    usage["cost_usd"] += float(row.get("cost_usd") or 0.0)
    if row.get("model"):
        usage["_models"].add(str(row["model"]))


def _append_observed_usage(usage: dict[str, Any], model_ref: str, *, is_error: bool) -> None:
    usage["calls"] += 1
    if is_error:
        usage["error_runs"] += 1
    if model_ref:
        usage["_models"].add(model_ref)


def _display_name_for_provider(provider: str) -> str:
    normalized = normalize_provider(provider)
    return _PROVIDER_DISPLAY_NAMES.get(normalized, normalized or "Unknown")


def _classify_provider_from_model(model_name: str, provider_hint: str | None = None) -> str:
    hinted = normalize_provider(provider_hint or "")
    if hinted and hinted != "litellm":
        return hinted

    raw = (model_name or "").strip().lower()
    if not raw:
        return hinted

    if ":" in raw:
        prefix, suffix = raw.split(":", 1)
        normalized_prefix = normalize_provider(prefix)
        if normalized_prefix and normalized_prefix != "litellm":
            return normalized_prefix
        raw = suffix.strip()

    if raw.startswith(("claude", "anthropic")):
        return "anthropic"
    if raw.startswith("openrouter-") or raw.startswith("openrouter/"):
        return "openrouter"
    if raw.startswith("groq-") or raw.startswith("groq/"):
        return "groq"
    if raw.startswith("gemini") or raw.startswith("gemma"):
        return "gemini"
    if raw.startswith("deepseek"):
        return "deepseek"
    if raw.startswith(("qwen", "qwq", "dashscope-")):
        return "qwen"
    if raw.startswith("kimi"):
        return "kimi"
    if raw.startswith("minimax"):
        return "minimax"
    if raw in {"gpt-5.4", "gpt-5.4-mini", "gpt-5.3-codex"}:
        return "codex"
    if raw.startswith("codex"):
        return "codex"
    if raw.startswith("gpt-") or raw in {"o3", "o3-mini", "o3-pro"}:
        return "openai"

    return hinted or normalize_provider(raw.split("-", 1)[0])


def _key_status(*, is_active: bool, rate_limited_until: datetime | None, now: datetime) -> str:
    if not is_active:
        return "inactive"
    if rate_limited_until and rate_limited_until > now:
        return "rate_limited"
    return "active"


def _exact_pressure(
    *,
    status: str,
    recent_errors_24h: int,
    latest_ratelimit: dict[str, Any] | None,
) -> tuple[str, str]:
    if status == "rate_limited":
        return "exhausted", "Rate limited in DB"
    if status == "inactive":
        return "inactive", "Account inactive"

    if recent_errors_24h >= 3:
        return "critical", "Recent Anthropic API errors"

    if not latest_ratelimit:
        return "normal", "No recent exact rate-limit sample"

    limits = (
        ("rl_requests_limit", "rl_requests_remaining"),
        ("rl_tokens_limit", "rl_tokens_remaining"),
        ("rl_input_tokens_limit", "rl_input_tokens_remaining"),
        ("rl_output_tokens_limit", "rl_output_tokens_remaining"),
    )
    ratios: list[float] = []
    for limit_key, remaining_key in limits:
        remaining = latest_ratelimit.get(remaining_key)
        limit = latest_ratelimit.get(limit_key)
        if remaining is not None and int(remaining) <= 0:
            return "exhausted", f"{remaining_key} depleted"
        if limit is not None and int(limit) > 0 and remaining is not None:
            ratios.append(float(remaining) / float(limit))

    if ratios:
        min_ratio = min(ratios)
        if min_ratio <= 0.05:
            return "critical", "Remaining capacity under 5%"
        if min_ratio <= 0.20:
            return "elevated", "Remaining capacity under 20%"

    return "normal", "Healthy exact usage headroom"


def _observed_pressure(
    *,
    status: str,
    usage_5h: dict[str, Any],
    recent_errors_24h: int,
) -> tuple[str, str]:
    if status == "rate_limited":
        return "exhausted", "Rate limited in DB"
    if status == "inactive":
        return "inactive", "Account inactive"
    if recent_errors_24h >= 3:
        return "critical", "Recent provider-observed runner errors"
    if usage_5h["calls"] >= 6 or usage_5h["error_runs"] >= 1:
        return "elevated", "Observed provider activity is elevated"
    return "normal", "Key state only"


def _provider_pressure(summary: dict[str, Any]) -> tuple[str, str]:
    if summary["status"] == "rate_limited":
        return "exhausted", "All active accounts are rate limited"
    if summary["recent_errors_24h"] >= 3:
        return "critical", "Recent provider-observed runner errors"
    if summary["rate_limited_account_count"] > 0 or summary["observed_usage_5h"]["calls"] >= 6:
        return "elevated", "Observed usage or rate-limit pressure is elevated"
    if summary["status"] == "observed_only":
        return "observed", "Observed usage without managed accounts"
    if summary["status"] == "inactive":
        return "inactive", "No active managed accounts"
    return "normal", "Provider looks healthy"


def _clean_ratelimit_snapshot(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if not row:
        return None
    return {
        "requests_limit": row.get("rl_requests_limit"),
        "requests_remaining": row.get("rl_requests_remaining"),
        "requests_reset": _iso(row.get("rl_requests_reset")),
        "tokens_limit": row.get("rl_tokens_limit"),
        "tokens_remaining": row.get("rl_tokens_remaining"),
        "tokens_reset": _iso(row.get("rl_tokens_reset")),
        "input_tokens_limit": row.get("rl_input_tokens_limit"),
        "input_tokens_remaining": row.get("rl_input_tokens_remaining"),
        "input_tokens_reset": _iso(row.get("rl_input_tokens_reset")),
        "output_tokens_limit": row.get("rl_output_tokens_limit"),
        "output_tokens_remaining": row.get("rl_output_tokens_remaining"),
        "output_tokens_reset": _iso(row.get("rl_output_tokens_reset")),
        "captured_at": _iso(row.get("created_at")),
    }


def _merge_timestamp(current: datetime | None, candidate: datetime | None) -> datetime | None:
    if candidate is None:
        return current
    if current is None or candidate > current:
        return candidate
    return current


async def _fetch_pipeline_rows(conn: asyncpg.Connection) -> list[dict[str, Any]]:
    query = """
        SELECT actual_model,
               worker_model,
               status,
               COALESCE(started_at, created_at) AS observed_at
        FROM pipeline_jobs
        WHERE COALESCE(started_at, created_at) >= NOW() - INTERVAL '7 days'
          AND (
            NULLIF(actual_model, '') IS NOT NULL
            OR NULLIF(worker_model, '') IS NOT NULL
          )
    """
    try:
        rows = await conn.fetch(query)
    except asyncpg.UndefinedTableError:
        logger.warning("llm_account_usage.pipeline_jobs_missing")
        return []
    return [dict(row) for row in rows]


async def get_account_usage_snapshot() -> dict[str, Any]:
    from app.core.auth_provider import get_oauth_key_records_async

    now = datetime.now(timezone.utc)
    cutoff_5h = now - timedelta(hours=5)
    cutoff_24h = now - timedelta(hours=24)

    try:
        oauth_records = await get_oauth_key_records_async(include_rate_limited=True)
    except Exception:
        logger.exception("llm_account_usage.oauth_records_failed")
        oauth_records = []

    oauth_by_key_name = {str(record.get("key_name", "")): record for record in oauth_records}

    pool = get_pool()
    async with pool.acquire() as conn:
        key_rows = await conn.fetch(
            """
            SELECT id,
                   provider,
                   key_name,
                   label,
                   priority,
                   is_active,
                   rate_limited_until,
                   last_used_at,
                   last_verified_at
            FROM llm_api_keys
            ORDER BY provider, priority, id
            """
        )
        oauth_usage_rows = await conn.fetch(
            """
            SELECT account_slot,
                   model,
                   input_tokens,
                   output_tokens,
                   cost_usd,
                   error_code,
                   created_at
            FROM oauth_usage_log
            WHERE created_at >= NOW() - INTERVAL '7 days'
            ORDER BY created_at DESC
            """
        )
        oauth_ratelimit_rows = await conn.fetch(
            """
            SELECT DISTINCT ON (account_slot)
                   account_slot,
                   rl_requests_limit,
                   rl_requests_remaining,
                   rl_requests_reset,
                   rl_tokens_limit,
                   rl_tokens_remaining,
                   rl_tokens_reset,
                   rl_input_tokens_limit,
                   rl_input_tokens_remaining,
                   rl_input_tokens_reset,
                   rl_output_tokens_limit,
                   rl_output_tokens_remaining,
                   rl_output_tokens_reset,
                   created_at
            FROM oauth_usage_log
            WHERE rl_requests_remaining IS NOT NULL
               OR rl_tokens_remaining IS NOT NULL
               OR rl_input_tokens_remaining IS NOT NULL
               OR rl_output_tokens_remaining IS NOT NULL
            ORDER BY account_slot, created_at DESC
            """
        )
        pipeline_rows = await _fetch_pipeline_rows(conn)

    exact_usage: dict[str, dict[str, Any]] = {}
    for raw in oauth_usage_rows:
        row = dict(raw)
        slot = str(row.get("account_slot") or "").strip() or "unknown"
        bucket = exact_usage.setdefault(
            slot,
            {
                "window_5h": _new_usage("exact"),
                "window_7d": _new_usage("exact"),
                "recent_errors_24h": 0,
            },
        )
        created_at = row.get("created_at")
        if created_at and created_at >= cutoff_24h and row.get("error_code"):
            bucket["recent_errors_24h"] += 1
        if row.get("error_code"):
            continue
        _append_exact_usage(bucket["window_7d"], row)
        if created_at and created_at >= cutoff_5h:
            _append_exact_usage(bucket["window_5h"], row)

    latest_ratelimit_by_slot = {
        str(row["account_slot"]): dict(row)
        for row in oauth_ratelimit_rows
    }

    provider_observed: dict[str, dict[str, Any]] = {}
    for row in pipeline_rows:
        status = str(row.get("status") or "").strip().lower()
        actual_model = str(row.get("actual_model") or "").strip()
        worker_model = str(row.get("worker_model") or "").strip()
        model_ref = actual_model or (worker_model if status not in _PENDING_PIPELINE_STATUSES else "")
        if not model_ref:
            continue
        observed_at = row.get("observed_at") or now
        provider = _classify_provider_from_model(model_ref)
        bucket = provider_observed.setdefault(
            provider,
            {
                "provider": provider,
                "window_5h": _new_usage("provider_observed"),
                "window_7d": _new_usage("provider_observed"),
                "recent_errors_24h": 0,
                "last_observed_at": None,
            },
        )
        is_error = status == "error"
        _append_observed_usage(bucket["window_7d"], model_ref, is_error=is_error)
        if observed_at >= cutoff_5h:
            _append_observed_usage(bucket["window_5h"], model_ref, is_error=is_error)
        if observed_at >= cutoff_24h and is_error:
            bucket["recent_errors_24h"] += 1
        bucket["last_observed_at"] = _merge_timestamp(bucket["last_observed_at"], observed_at)

    accounts: list[dict[str, Any]] = []
    provider_summaries: dict[str, dict[str, Any]] = {}

    for raw in key_rows:
        row = dict(raw)
        provider = normalize_provider(row["provider"])
        provider_summary = provider_summaries.setdefault(
            provider,
            {
                "provider": provider,
                "display_name": _display_name_for_provider(provider),
                "account_count": 0,
                "active_account_count": 0,
                "rate_limited_account_count": 0,
                "inactive_account_count": 0,
                "last_used_at": None,
                "last_verified_at": None,
            },
        )

        status = _key_status(
            is_active=bool(row["is_active"]),
            rate_limited_until=row.get("rate_limited_until"),
            now=now,
        )
        provider_summary["account_count"] += 1
        provider_summary[f"{status}_account_count"] += 1
        provider_summary["last_used_at"] = _merge_timestamp(provider_summary["last_used_at"], row.get("last_used_at"))
        provider_summary["last_verified_at"] = _merge_timestamp(provider_summary["last_verified_at"], row.get("last_verified_at"))

        oauth_record = oauth_by_key_name.get(str(row["key_name"])) if provider == "anthropic" else None
        oauth_slot = str(oauth_record.get("slot") or "") if oauth_record else ""
        exact_bucket = exact_usage.get(oauth_slot) if oauth_slot else None
        provider_bucket = provider_observed.get(provider)

        if exact_bucket:
            usage_scope = "exact"
            usage_5h = _finalize_usage(exact_bucket["window_5h"])
            usage_7d = _finalize_usage(exact_bucket["window_7d"])
            recent_errors_24h = int(exact_bucket["recent_errors_24h"])
            latest_ratelimit = latest_ratelimit_by_slot.get(oauth_slot)
            pressure, pressure_reason = _exact_pressure(
                status=status,
                recent_errors_24h=recent_errors_24h,
                latest_ratelimit=latest_ratelimit,
            )
            measurement_note = "Anthropic exact per-account usage"
        elif provider_bucket:
            usage_scope = "provider_observed"
            usage_5h = _finalize_usage(provider_bucket["window_5h"])
            usage_7d = _finalize_usage(provider_bucket["window_7d"])
            recent_errors_24h = int(provider_bucket["recent_errors_24h"])
            latest_ratelimit = None
            pressure, pressure_reason = _observed_pressure(
                status=status,
                usage_5h=usage_5h,
                recent_errors_24h=recent_errors_24h,
            )
            measurement_note = "Provider-level observed usage only"
        else:
            usage_scope = "key_state_only"
            usage_5h = _finalize_usage(_new_usage("key_state_only"))
            usage_7d = _finalize_usage(_new_usage("key_state_only"))
            recent_errors_24h = 0
            latest_ratelimit = None
            pressure, pressure_reason = _observed_pressure(
                status=status,
                usage_5h=usage_5h,
                recent_errors_24h=recent_errors_24h,
            )
            measurement_note = "Key state only"

        label = str(row.get("label") or "").strip()
        if not label and oauth_record:
            label = str(oauth_record.get("label") or "").strip()

        accounts.append(
            {
                "id": row["id"],
                "provider": provider,
                "provider_display_name": _display_name_for_provider(provider),
                "key_name": row["key_name"],
                "label": label,
                "slot": oauth_slot or None,
                "priority": int(row.get("priority") or 0),
                "status": status,
                "pressure": pressure,
                "pressure_reason": pressure_reason,
                "usage_scope": usage_scope,
                "measurement_note": measurement_note,
                "rate_limited_until": _iso(row.get("rate_limited_until")),
                "last_used_at": _iso(row.get("last_used_at")),
                "last_verified_at": _iso(row.get("last_verified_at")),
                "usage_5h": usage_5h,
                "usage_7d": usage_7d,
                "recent_errors_24h": recent_errors_24h,
                "recent_errors_scope": "exact" if usage_scope == "exact" else "provider_observed" if usage_scope == "provider_observed" else "none",
                "latest_ratelimit": _clean_ratelimit_snapshot(latest_ratelimit),
            }
        )

    for provider, bucket in provider_observed.items():
        summary = provider_summaries.setdefault(
            provider,
            {
                "provider": provider,
                "display_name": _display_name_for_provider(provider),
                "account_count": 0,
                "active_account_count": 0,
                "rate_limited_account_count": 0,
                "inactive_account_count": 0,
                "last_used_at": None,
                "last_verified_at": None,
            },
        )
        summary["observed_usage_5h"] = _finalize_usage(bucket["window_5h"])
        summary["observed_usage_7d"] = _finalize_usage(bucket["window_7d"])
        summary["recent_errors_24h"] = int(bucket["recent_errors_24h"])
        summary["last_observed_at"] = _iso(bucket["last_observed_at"])

    for provider, summary in provider_summaries.items():
        summary.setdefault("observed_usage_5h", _finalize_usage(_new_usage("provider_observed")))
        summary.setdefault("observed_usage_7d", _finalize_usage(_new_usage("provider_observed")))
        summary.setdefault("recent_errors_24h", 0)
        summary.setdefault("last_observed_at", None)
        summary["usage_scope"] = (
            "exact_per_account"
            if provider == "anthropic"
            else "provider_observed"
            if summary["observed_usage_7d"]["calls"] > 0
            else "key_state_only"
        )
        if summary["rate_limited_account_count"] > 0 and summary["active_account_count"] == 0 and summary["account_count"] > 0:
            summary["status"] = "rate_limited"
        elif summary["active_account_count"] > 0:
            summary["status"] = "active"
        elif summary["account_count"] == 0 and summary["observed_usage_7d"]["calls"] > 0:
            summary["status"] = "observed_only"
        else:
            summary["status"] = "inactive"
        summary["pressure"], summary["pressure_reason"] = _provider_pressure(summary)
        summary["last_used_at"] = _iso(summary["last_used_at"])
        summary["last_verified_at"] = _iso(summary["last_verified_at"])

    accounts.sort(key=lambda item: (item["provider"], item["priority"], item["key_name"]))
    providers = sorted(provider_summaries.values(), key=lambda item: (item["provider"] != "anthropic", item["provider"]))

    total_accounts = len(accounts)
    active_accounts = sum(1 for account in accounts if account["status"] == "active")
    rate_limited_accounts = sum(1 for account in accounts if account["status"] == "rate_limited")
    inactive_accounts = sum(1 for account in accounts if account["status"] == "inactive")

    return {
        "generated_at": datetime.now(KST).isoformat(),
        "summary": {
            "total_accounts": total_accounts,
            "active_accounts": active_accounts,
            "rate_limited_accounts": rate_limited_accounts,
            "inactive_accounts": inactive_accounts,
            "provider_count": len(providers),
        },
        "providers": providers,
        "accounts": accounts,
    }
