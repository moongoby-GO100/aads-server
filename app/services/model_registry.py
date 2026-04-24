from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterable, Sequence

import asyncpg

from app.core.db_pool import get_pool

logger = logging.getLogger(__name__)

_CACHE_TTL_SECONDS = 30
_cache: dict[str, tuple[Any, float]] = {}

_PROVIDER_ALIASES = {
    "anthropic": "anthropic",
    "claude": "anthropic",
    "gemini": "gemini",
    "google": "gemini",
    "openai": "openai",
    "groq": "groq",
    "deepseek": "deepseek",
    "openrouter": "openrouter",
    "alibaba": "qwen",
    "dashscope": "qwen",
    "qwen": "qwen",
    "kimi": "kimi",
    "moonshot": "kimi",
    "minimax": "minimax",
    "codex": "codex",
    "litellm": "litellm",
}


@dataclass(frozen=True)
class ModelTemplate:
    provider: str
    model_id: str
    display_name: str
    family: str
    category: str
    supports_tools: bool
    supports_thinking: bool
    supports_vision: bool
    supports_coding: bool
    input_cost: Decimal | None
    output_cost: Decimal | None
    execution_backend: str | None
    execution_model_id: str | None
    execution_base_url: str | None


def normalize_provider(provider: str) -> str:
    return _PROVIDER_ALIASES.get((provider or "").strip().lower(), (provider or "").strip().lower())


def invalidate_registry_cache() -> None:
    _cache.clear()


def _cache_get(key: str) -> Any:
    cached = _cache.get(key)
    if not cached:
        return None
    value, expires_at = cached
    if expires_at <= time.time():
        _cache.pop(key, None)
        return None
    return value


def _cache_set(key: str, value: Any) -> Any:
    _cache[key] = (value, time.time() + _CACHE_TTL_SECONDS)
    return value


def _coerce_json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            logger.warning("model_registry.invalid_metadata_json")
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    if value is None:
        return {}
    try:
        return dict(value)
    except (TypeError, ValueError):
        logger.warning("model_registry.invalid_metadata_type: %s", type(value).__name__)
        return {}


def _decimal(value: float | str | None) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


_MODEL_COSTS: dict[str, tuple[Decimal, Decimal]] = {
    "claude-opus": (_decimal(5.0), _decimal(25.0)),
    "claude-opus-46": (_decimal(5.0), _decimal(25.0)),
    "claude-sonnet": (_decimal(3.0), _decimal(15.0)),
    "claude-haiku": (_decimal(1.0), _decimal(5.0)),
    "gemini-flash": (_decimal(0.075), _decimal(0.3)),
    "gemini-flash-lite": (_decimal(0.01), _decimal(0.04)),
    "gemini-pro": (_decimal(1.25), _decimal(5.0)),
    "gemini-3-flash-preview": (_decimal(0.5), _decimal(3.0)),
    "gemini-3.1-flash-lite-preview": (_decimal(0.25), _decimal(1.5)),
    "gemini-3.1-pro-preview": (_decimal(2.0), _decimal(12.0)),
    "gemini-2.5-flash": (_decimal(0.15), _decimal(0.6)),
    "gemini-2.5-flash-lite": (_decimal(0.04), _decimal(0.1)),
    "groq-qwen3-32b": (_decimal(0.0), _decimal(0.0)),
    "groq-kimi-k2": (_decimal(0.0), _decimal(0.0)),
    "groq-llama4-scout": (_decimal(0.0), _decimal(0.0)),
    "groq-llama-70b": (_decimal(0.0), _decimal(0.0)),
    "groq-llama-8b": (_decimal(0.0), _decimal(0.0)),
    "groq-gpt-oss-120b": (_decimal(0.0), _decimal(0.0)),
    "groq-compound": (_decimal(0.0), _decimal(0.0)),
    "gpt-4o": (_decimal(2.5), _decimal(10.0)),
    "gpt-4o-mini": (_decimal(0.15), _decimal(0.6)),
    "gpt-5": (_decimal(5.0), _decimal(15.0)),
    "gpt-5-mini": (_decimal(0.5), _decimal(2.0)),
    "o3": (_decimal(2.0), _decimal(8.0)),
    "o3-mini": (_decimal(1.1), _decimal(4.4)),
    "o3-pro": (_decimal(20.0), _decimal(80.0)),
    "gpt-5.4": (_decimal(2.5), _decimal(15.0)),
    "gpt-5.4-mini": (_decimal(0.75), _decimal(4.5)),
    "gpt-5.3-codex": (_decimal(1.75), _decimal(14.0)),
    "deepseek-chat": (_decimal(0.28), _decimal(0.42)),
    "deepseek-reasoner": (_decimal(0.55), _decimal(2.19)),
    "openrouter-grok-4-fast": (_decimal(0.2), _decimal(0.2)),
    "openrouter-deepseek-v3": (_decimal(0.26), _decimal(0.26)),
    "openrouter-mistral-small": (_decimal(0.15), _decimal(0.15)),
    "openrouter-nemotron-free": (_decimal(0.0), _decimal(0.0)),
    "openrouter-minimax-m2": (_decimal(0.3), _decimal(0.3)),
    "qwen3-235b": (_decimal(0.6), _decimal(2.4)),
    "qwen3-235b-instruct": (_decimal(0.6), _decimal(2.4)),
    "qwen3-235b-thinking": (_decimal(0.6), _decimal(2.4)),
    "qwen3-next-80b": (_decimal(0.3), _decimal(1.2)),
    "qwen3-max": (_decimal(0.4), _decimal(1.2)),
    "qwen3-32b": (_decimal(0.08), _decimal(0.32)),
    "qwen3-30b-a3b": (_decimal(0.07), _decimal(0.28)),
    "qwen3-14b": (_decimal(0.04), _decimal(0.16)),
    "qwen3-8b": (_decimal(0.02), _decimal(0.08)),
    "qwen3-coder-plus": (_decimal(0.35), _decimal(1.4)),
    "qwen3-coder-flash": (_decimal(0.07), _decimal(0.28)),
    "qwen3-coder-480b": (_decimal(1.2), _decimal(4.8)),
    "qwen3.5-plus": (_decimal(0.4), _decimal(1.2)),
    "qwen3.5-flash": (_decimal(0.07), _decimal(0.28)),
    "qwen-max": (_decimal(0.4), _decimal(1.2)),
    "qwen-max-latest": (_decimal(0.4), _decimal(1.2)),
    "qwen-plus": (_decimal(0.08), _decimal(0.32)),
    "qwen-plus-latest": (_decimal(0.08), _decimal(0.32)),
    "qwen-turbo": (_decimal(0.02), _decimal(0.06)),
    "qwen-turbo-latest": (_decimal(0.02), _decimal(0.06)),
    "qwen-flash": (_decimal(0.01), _decimal(0.03)),
    "qwen-coder-plus": (_decimal(0.35), _decimal(1.4)),
    "qwen2.5-72b-instruct": (_decimal(0.3), _decimal(0.9)),
    "qwq-plus": (_decimal(0.6), _decimal(2.4)),
    "qwen-vl-max": (_decimal(0.4), _decimal(1.2)),
    "qwen-vl-plus": (_decimal(0.08), _decimal(0.32)),
    "qwen3-vl-plus": (_decimal(0.35), _decimal(1.4)),
    "qwen3-vl-235b": (_decimal(0.6), _decimal(2.4)),
    "qwen-omni-turbo": (_decimal(0.02), _decimal(0.06)),
    "dashscope-deepseek-v3.2": (_decimal(0.28), _decimal(0.42)),
    "kimi-k2.5": (_decimal(0.6), _decimal(2.4)),
    "kimi-k2": (_decimal(0.6), _decimal(2.4)),
    "kimi-latest": (_decimal(0.02), _decimal(0.06)),
    "kimi-128k": (_decimal(0.06), _decimal(0.24)),
    "kimi-8k": (_decimal(0.02), _decimal(0.06)),
    "minimax-m2.7": (_decimal(0.5), _decimal(2.0)),
    "minimax-m2.5": (_decimal(0.3), _decimal(1.2)),
}

_THINKING_MODELS = {
    "gemini-pro",
    "gemini-flash",
    "gemini-3-flash-preview",
    "gemini-3-pro-preview",
    "gemini-3.1-flash-lite-preview",
    "gemini-3.1-pro-preview",
    "gemini-2.5-flash",
    "gemini-2.5-pro",
    "deepseek-reasoner",
    "qwen3-235b-thinking",
    "qwq-plus",
    "o3",
    "o3-mini",
    "o3-pro",
}

_VISION_MODELS = {
    "gpt-4o",
    "gpt-4o-mini",
    "gemini-2.5-flash-image",
    "qwen-vl-max",
    "qwen-vl-plus",
    "qwen3-vl-plus",
    "qwen3-vl-235b",
    "qwen-omni-turbo",
}

_CODING_MODELS = {
    "claude-opus",
    "claude-opus-46",
    "claude-sonnet",
    "claude-haiku",
    "gpt-5",
    "gpt-5-mini",
    "gpt-5.4",
    "gpt-5.4-mini",
    "gpt-5.3-codex",
    "o3",
    "o3-mini",
    "o3-pro",
    "qwen3-coder-plus",
    "qwen3-coder-flash",
    "qwen3-coder-480b",
    "qwen-coder-plus",
}

_DISPLAY_NAME_OVERRIDES = {
    "claude-opus": "Claude Opus",
    "claude-opus-46": "Claude Opus 4.6",
    "claude-sonnet": "Claude Sonnet",
    "claude-haiku": "Claude Haiku",
    "gpt-5.4": "GPT-5.4 (Codex CLI)",
    "gpt-5.4-mini": "GPT-5.4 Mini (Codex CLI)",
    "gpt-5.3-codex": "GPT-5.3 Codex (Codex CLI)",
    "openrouter-grok-4-fast": "OpenRouter Grok 4 Fast",
    "openrouter-deepseek-v3": "OpenRouter DeepSeek V3",
    "openrouter-mistral-small": "OpenRouter Mistral Small",
    "openrouter-nemotron-free": "OpenRouter Nemotron Free",
    "openrouter-minimax-m2": "OpenRouter MiniMax M2",
    "dashscope-deepseek-v3.2": "DashScope DeepSeek V3.2",
}

_PROVIDER_MODELS: dict[str, tuple[str, ...]] = {
    "anthropic": ("claude-opus", "claude-opus-46", "claude-sonnet", "claude-haiku"),
    "gemini": (
        "gemini-flash",
        "gemini-flash-lite",
        "gemini-pro",
        "gemini-3-flash-preview",
        "gemini-3-pro-preview",
        "gemini-3.1-flash-lite-preview",
        "gemini-3.1-pro-preview",
        "gemini-2.0-flash",
        "gemini-2.5-flash",
        "gemini-2.5-flash-lite",
        "gemini-2.5-pro",
        "gemini-2.5-flash-image",
        "gemma-3-27b-it",
    ),
    "groq": (
        "groq-qwen3-32b",
        "groq-kimi-k2",
        "groq-llama4-scout",
        "groq-llama-70b",
        "groq-llama-8b",
        "groq-gpt-oss-120b",
        "groq-compound",
    ),
    "openai": ("gpt-4o", "gpt-4o-mini", "gpt-5", "gpt-5-mini", "o3", "o3-mini", "o3-pro"),
    "codex": ("gpt-5.4", "gpt-5.4-mini", "gpt-5.3-codex"),
    "deepseek": ("deepseek-chat", "deepseek-reasoner"),
    "openrouter": (
        "openrouter-grok-4-fast",
        "openrouter-deepseek-v3",
        "openrouter-mistral-small",
        "openrouter-nemotron-free",
        "openrouter-minimax-m2",
    ),
    "qwen": (
        "qwen3-235b",
        "qwen3-235b-instruct",
        "qwen3-235b-thinking",
        "qwen3-next-80b",
        "qwen3-max",
        "qwen3-32b",
        "qwen3-30b-a3b",
        "qwen3-14b",
        "qwen3-8b",
        "qwen3-coder-plus",
        "qwen3-coder-flash",
        "qwen3-coder-480b",
        "qwen3.5-plus",
        "qwen3.5-flash",
        "qwen-max",
        "qwen-max-latest",
        "qwen-plus",
        "qwen-plus-latest",
        "qwen-turbo",
        "qwen-turbo-latest",
        "qwen-flash",
        "qwen-coder-plus",
        "qwen2.5-72b-instruct",
        "qwq-plus",
        "qwen-vl-max",
        "qwen-vl-plus",
        "qwen3-vl-plus",
        "qwen3-vl-235b",
        "qwen-omni-turbo",
        "dashscope-deepseek-v3.2",
    ),
    "kimi": ("kimi-k2.5", "kimi-k2", "kimi-latest", "kimi-128k", "kimi-8k"),
    "minimax": ("minimax-m2.7", "minimax-m2.5"),
}

_KEYLESS_PROVIDERS = {"codex"}

_PROVIDER_META = {
    "anthropic": {"display_name": "Anthropic", "manual_review": False},
    "gemini": {"display_name": "Gemini", "manual_review": False},
    "groq": {"display_name": "Groq", "manual_review": False},
    "openai": {"display_name": "OpenAI", "manual_review": False},
    "codex": {"display_name": "Codex CLI", "manual_review": False},
    "deepseek": {"display_name": "DeepSeek", "manual_review": False},
    "openrouter": {"display_name": "OpenRouter", "manual_review": False},
    "qwen": {"display_name": "Qwen / DashScope", "manual_review": False},
    "kimi": {"display_name": "Kimi", "manual_review": False},
    "minimax": {"display_name": "MiniMax", "manual_review": False},
}

_DIRECT_PROVIDER_BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "groq": "https://api.groq.com/openai/v1",
    "deepseek": "https://api.deepseek.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "qwen": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
    "kimi": "https://api.moonshot.ai/v1",
    "minimax": "https://api.minimax.chat/v1",
}


def _titleize(token: str) -> str:
    special = {
        "gpt": "GPT",
        "o3": "o3",
        "qwen": "Qwen",
        "kimi": "Kimi",
        "groq": "Groq",
        "claude": "Claude",
        "gemini": "Gemini",
        "deepseek": "DeepSeek",
        "openrouter": "OpenRouter",
        "minimax": "MiniMax",
        "vl": "VL",
    }
    return special.get(token.lower(), token.upper() if token.isalpha() and len(token) <= 3 else token.capitalize())


def _display_name_for(model_id: str) -> str:
    override = _DISPLAY_NAME_OVERRIDES.get(model_id)
    if override:
        return override
    parts = model_id.replace(".", " ").replace("-", " ").split()
    return " ".join(_titleize(part) for part in parts)


def _family_for(provider: str, model_id: str) -> str:
    if provider == "anthropic":
        return "claude"
    if provider == "openai":
        return "o-series" if model_id.startswith("o") else "gpt"
    if provider == "codex":
        return "codex"
    if provider == "qwen":
        return "qwen"
    return provider


def _category_for(model_id: str) -> str:
    lowered = model_id.lower()
    if model_id in _VISION_MODELS or "vl" in lowered or "image" in lowered or "omni" in lowered:
        return "vision"
    if model_id in _CODING_MODELS or "coder" in lowered or "codex" in lowered:
        return "coding"
    if model_id in _THINKING_MODELS or "reasoner" in lowered or "thinking" in lowered:
        return "reasoning"
    return "general"


def _supports_tools_for(provider: str) -> bool:
    return provider != "litellm"


def _build_template(provider: str, model_id: str) -> ModelTemplate:
    costs = _MODEL_COSTS.get(model_id, (None, None))
    category = _category_for(model_id)
    execution_backend = None
    execution_base_url = None
    execution_model_id = model_id
    if provider in _DIRECT_PROVIDER_BASE_URLS:
        execution_backend = "openai_compatible_direct"
        execution_base_url = _DIRECT_PROVIDER_BASE_URLS[provider]
    elif provider == "codex":
        execution_backend = "codex_cli"
    elif provider == "anthropic":
        execution_backend = "claude_cli_relay"
    elif provider == "gemini":
        execution_backend = "litellm_proxy"
    return ModelTemplate(
        provider=provider,
        model_id=model_id,
        display_name=_display_name_for(model_id),
        family=_family_for(provider, model_id),
        category=category,
        supports_tools=_supports_tools_for(provider),
        supports_thinking=model_id in _THINKING_MODELS or category == "reasoning",
        supports_vision=model_id in _VISION_MODELS or category == "vision",
        supports_coding=model_id in _CODING_MODELS or category == "coding" or provider in {"anthropic", "codex"},
        input_cost=costs[0],
        output_cost=costs[1],
        execution_backend=execution_backend,
        execution_model_id=execution_model_id,
        execution_base_url=execution_base_url,
    )


_PROVIDER_TEMPLATES: dict[str, tuple[ModelTemplate, ...]] = {
    provider: tuple(_build_template(provider, model_id) for model_id in model_ids)
    for provider, model_ids in _PROVIDER_MODELS.items()
}


def _iso(dt: datetime | None) -> str | None:
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _pick_linked_key(keys: list[dict[str, Any]]) -> str | None:
    if not keys:
        return None
    available = [key for key in keys if key["is_available"]]
    source = available or keys
    source.sort(key=lambda item: (item["priority"], item["id"]))
    return source[0]["key_name"]


def _build_key_state(rows: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    state: dict[str, dict[str, Any]] = {}
    now = datetime.now(timezone.utc)
    for raw in rows:
        provider = normalize_provider(raw["provider"])
        bucket = state.setdefault(
            provider,
            {
                "provider": provider,
                "raw_providers": set(),
                "keys": [],
                "active_key_count": 0,
                "available_key_count": 0,
                "rate_limited_key_count": 0,
                "verified_key_count": 0,
                "last_used_at": None,
                "last_verified_at": None,
            },
        )
        bucket["raw_providers"].add(raw["provider"])
        is_available = bool(raw["is_active"]) and (
            raw["rate_limited_until"] is None or raw["rate_limited_until"] <= now
        )
        key_row = {
            "id": raw["id"],
            "key_name": raw["key_name"],
            "priority": raw["priority"],
            "is_active": bool(raw["is_active"]),
            "is_available": is_available,
            "rate_limited_until": raw["rate_limited_until"],
            "last_used_at": raw["last_used_at"],
            "last_verified_at": raw["last_verified_at"],
        }
        bucket["keys"].append(key_row)
        if raw["is_active"]:
            bucket["active_key_count"] += 1
        if is_available:
            bucket["available_key_count"] += 1
        if raw["rate_limited_until"] and raw["rate_limited_until"] > now:
            bucket["rate_limited_key_count"] += 1
        if raw["last_verified_at"]:
            bucket["verified_key_count"] += 1
        if raw["last_used_at"] and (
            bucket["last_used_at"] is None or raw["last_used_at"] > bucket["last_used_at"]
        ):
            bucket["last_used_at"] = raw["last_used_at"]
        if raw["last_verified_at"] and (
            bucket["last_verified_at"] is None or raw["last_verified_at"] > bucket["last_verified_at"]
        ):
            bucket["last_verified_at"] = raw["last_verified_at"]
    return state


def build_registry_snapshots(key_rows: Iterable[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    key_state = _build_key_state(key_rows)
    model_rows: list[dict[str, Any]] = []
    provider_rows: list[dict[str, Any]] = []

    for provider, meta in _PROVIDER_META.items():
        state = key_state.get(provider, {})
        keys = list(state.get("keys", []))
        keyless = provider in _KEYLESS_PROVIDERS
        active_key_count = int(state.get("active_key_count", 0))
        available_key_count = int(state.get("available_key_count", 0))
        rate_limited_key_count = int(state.get("rate_limited_key_count", 0))
        verified_key_count = int(state.get("verified_key_count", 0))
        has_runtime_models = keyless or available_key_count > 0
        activation_source = "fallback" if keyless or active_key_count == 0 else "db"
        linked_key_name = None if keyless else _pick_linked_key(keys)

        templates = _PROVIDER_TEMPLATES.get(provider, ())
        for template in templates:
            metadata = {
                "template_provider": provider,
                "raw_provider_aliases": sorted(state.get("raw_providers", set())),
                "active_key_count": active_key_count,
                "available_key_count": available_key_count,
                "rate_limited_key_count": rate_limited_key_count,
                "verified_key_count": verified_key_count,
                "last_used_at": _iso(state.get("last_used_at")),
                "last_verified_at": _iso(state.get("last_verified_at")),
                "requires_admin_review": False,
                "execution_backend": template.execution_backend,
                "execution_model_id": template.execution_model_id,
                "execution_base_url": template.execution_base_url,
            }
            model_rows.append(
                {
                    "provider": provider,
                    "model_id": template.model_id,
                    "display_name": template.display_name,
                    "family": template.family,
                    "category": template.category,
                    "supports_tools": template.supports_tools,
                    "supports_thinking": template.supports_thinking,
                    "supports_vision": template.supports_vision,
                    "supports_coding": template.supports_coding,
                    "input_cost": template.input_cost,
                    "output_cost": template.output_cost,
                    "is_active": has_runtime_models,
                    "activation_source": activation_source,
                    "linked_key_name": linked_key_name,
                    "metadata": metadata,
                }
            )

        provider_rows.append(
            {
                "provider": provider,
                "display_name": meta["display_name"],
                "template_available": True,
                "requires_admin_review": False,
                "active_key_count": active_key_count,
                "available_key_count": available_key_count,
                "rate_limited_key_count": rate_limited_key_count,
                "verified_key_count": verified_key_count,
                "active_model_count": sum(1 for row in model_rows if row["provider"] == provider and row["is_active"]),
                "template_model_count": len(templates),
                "last_used_at": _iso(state.get("last_used_at")),
                "last_verified_at": _iso(state.get("last_verified_at")),
                "linked_key_name": linked_key_name,
                "status": (
                    "active"
                    if has_runtime_models
                    else "rate_limited"
                    if active_key_count > 0 and available_key_count == 0 and rate_limited_key_count > 0
                    else "inactive"
                ),
            }
        )

    for provider, state in sorted(key_state.items()):
        if provider in _PROVIDER_META:
            continue
        provider_rows.append(
            {
                "provider": provider,
                "display_name": provider or "unknown",
                "template_available": False,
                "requires_admin_review": True,
                "active_key_count": int(state.get("active_key_count", 0)),
                "available_key_count": int(state.get("available_key_count", 0)),
                "rate_limited_key_count": int(state.get("rate_limited_key_count", 0)),
                "verified_key_count": int(state.get("verified_key_count", 0)),
                "active_model_count": 0,
                "template_model_count": 0,
                "last_used_at": _iso(state.get("last_used_at")),
                "last_verified_at": _iso(state.get("last_verified_at")),
                "linked_key_name": _pick_linked_key(list(state.get("keys", []))),
                "status": "review_required",
            }
        )

    provider_rows.sort(key=lambda row: row["provider"])
    model_rows.sort(key=lambda row: (row["provider"], row["family"], row["model_id"]))
    return model_rows, provider_rows


async def append_key_audit_log(
    conn: asyncpg.Connection,
    *,
    key_id: int | None,
    provider: str,
    key_name: str,
    event_type: str,
    actor: str,
    details: dict[str, Any] | None = None,
) -> None:
    safe_details = dict(details or {})
    for forbidden in ("value", "raw_value", "encrypted_value", "token", "secret", "plaintext"):
        safe_details.pop(forbidden, None)
    try:
        await conn.execute(
            """
            INSERT INTO llm_key_audit_logs (key_id, provider, key_name, event_type, actor, details)
            VALUES ($1, $2, $3, $4, $5, $6::jsonb)
            """,
            key_id,
            normalize_provider(provider),
            key_name,
            event_type,
            actor,
            json.dumps(safe_details),
        )
    except asyncpg.UndefinedTableError:
        logger.warning("model_registry.audit_log_table_missing")


async def _fetch_key_rows(conn: asyncpg.Connection) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        """
        SELECT id, provider, key_name, priority, is_active,
               rate_limited_until, last_used_at, last_verified_at
        FROM llm_api_keys
        ORDER BY provider, priority, id
        """
    )
    return [dict(row) for row in rows]


async def _fetch_registry_rows(conn: asyncpg.Connection) -> list[dict[str, Any]] | None:
    try:
        rows = await conn.fetch(
            """
            SELECT provider, model_id, display_name, family, category,
                   supports_tools, supports_thinking, supports_vision, supports_coding,
                   input_cost, output_cost, is_active, activation_source,
                   linked_key_name, metadata, updated_at
            FROM llm_models
            ORDER BY provider, family, model_id
            """
        )
    except asyncpg.UndefinedTableError:
        return None
    return [dict(row) for row in rows]


async def list_registered_models(*, provider: str | None = None, active_only: bool = False) -> list[dict[str, Any]]:
    cache_key = f"models:{normalize_provider(provider or '')}:{int(active_only)}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    pool = get_pool()
    async with pool.acquire() as conn:
        key_rows = await _fetch_key_rows(conn)
        registry_rows = await _fetch_registry_rows(conn)
        if registry_rows is None or not registry_rows:
            registry_rows, _ = build_registry_snapshots(key_rows)

    filtered = []
    normalized_provider = normalize_provider(provider or "")
    for row in registry_rows:
        if normalized_provider and row["provider"] != normalized_provider:
            continue
        if active_only and not row["is_active"]:
            continue
        normalized_row = dict(row)
        normalized_row["metadata"] = _coerce_json_object(normalized_row.get("metadata"))
        filtered.append(normalized_row)
    return _cache_set(cache_key, filtered)


async def list_provider_summaries() -> list[dict[str, Any]]:
    cached = _cache_get("provider_summaries")
    if cached is not None:
        return cached

    pool = get_pool()
    async with pool.acquire() as conn:
        key_rows = await _fetch_key_rows(conn)
    _, provider_rows = build_registry_snapshots(key_rows)
    return _cache_set("provider_summaries", provider_rows)


async def get_executable_model_ids() -> set[str] | None:
    cached = _cache_get("executable_ids")
    if cached is not None:
        return cached
    try:
        rows = await list_registered_models(active_only=True)
    except Exception:
        logger.exception("model_registry.executable_ids_failed")
        return None
    executable = {row["model_id"] for row in rows if row.get("is_active")}
    return _cache_set("executable_ids", executable)


async def filter_executable_models(model_ids: Sequence[str]) -> list[str]:
    executable_ids = await get_executable_model_ids()
    if executable_ids is None or len(executable_ids) == 0:
        return list(model_ids)
    return [model_id for model_id in model_ids if model_id in executable_ids]


async def sync_model_registry(*, triggered_by: str = "system", reason: str = "") -> dict[str, Any]:
    sync_token = uuid.uuid4().hex
    pool = get_pool()
    async with pool.acquire() as conn:
        key_rows = await _fetch_key_rows(conn)
        model_rows, provider_rows = build_registry_snapshots(key_rows)

        try:
            async with conn.transaction():
                for row in model_rows:
                    metadata = _coerce_json_object(row.get("metadata"))
                    metadata["sync_token"] = sync_token
                    await conn.execute(
                        """
                        INSERT INTO llm_models (
                            provider, model_id, display_name, family, category,
                            supports_tools, supports_thinking, supports_vision, supports_coding,
                            input_cost, output_cost, is_active, activation_source,
                            linked_key_name, metadata, updated_at
                        )
                        VALUES (
                            $1, $2, $3, $4, $5,
                            $6, $7, $8, $9,
                            $10, $11, $12, $13,
                            $14, $15::jsonb, NOW()
                        )
                        ON CONFLICT (provider, model_id)
                        DO UPDATE SET
                            display_name = EXCLUDED.display_name,
                            family = EXCLUDED.family,
                            category = EXCLUDED.category,
                            supports_tools = EXCLUDED.supports_tools,
                            supports_thinking = EXCLUDED.supports_thinking,
                            supports_vision = EXCLUDED.supports_vision,
                            supports_coding = EXCLUDED.supports_coding,
                            input_cost = EXCLUDED.input_cost,
                            output_cost = EXCLUDED.output_cost,
                            is_active = EXCLUDED.is_active,
                            activation_source = EXCLUDED.activation_source,
                            linked_key_name = EXCLUDED.linked_key_name,
                            metadata = EXCLUDED.metadata,
                            updated_at = NOW()
                        """,
                        row["provider"],
                        row["model_id"],
                        row["display_name"],
                        row["family"],
                        row["category"],
                        row["supports_tools"],
                        row["supports_thinking"],
                        row["supports_vision"],
                        row["supports_coding"],
                        row["input_cost"],
                        row["output_cost"],
                        row["is_active"],
                        row["activation_source"],
                        row["linked_key_name"],
                        json.dumps(metadata),
                    )

                await conn.execute(
                    """
                    UPDATE llm_models
                    SET is_active = FALSE,
                        activation_source = CASE
                            WHEN activation_source = 'manual' THEN activation_source
                            ELSE 'fallback'
                        END,
                        metadata = COALESCE(metadata, '{}'::jsonb) || '{"retired": true}'::jsonb,
                        updated_at = NOW()
                    WHERE activation_source <> 'manual'
                      AND COALESCE(metadata->>'sync_token', '') <> $1
                    """,
                    sync_token,
                )

                await append_key_audit_log(
                    conn,
                    key_id=None,
                    provider="registry",
                    key_name="*",
                    event_type="registry_sync",
                    actor=triggered_by,
                    details={
                        "reason": reason,
                        "models_synced": len(model_rows),
                        "providers_seen": [row["provider"] for row in provider_rows],
                    },
                )
        except asyncpg.UndefinedTableError:
            logger.warning("model_registry.sync_missing_table")
            return {"ok": False, "error": "registry_tables_missing", "models_synced": 0, "providers": provider_rows}

    invalidate_registry_cache()
    return {"ok": True, "models_synced": len(model_rows), "providers": provider_rows}
