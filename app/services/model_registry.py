from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Awaitable, Callable, Iterable, Mapping, NamedTuple, Sequence

import asyncpg
import httpx

from app.core.db_pool import get_pool
from app.services import cli_model_autoreg

logger = logging.getLogger(__name__)

_CACHE_TTL_SECONDS = 30
_DISCOVERY_TIMEOUT_SECONDS = float(os.getenv("LLM_MODEL_DISCOVERY_TIMEOUT_SECONDS", "8"))
_AUTO_REFRESH_MIN_INTERVAL_SECONDS = max(15, int(os.getenv("LLM_MODEL_REGISTRY_AUTO_REFRESH_SECONDS", "15")))
_cache: dict[str, tuple[Any, float]] = {}
_auto_refresh_attempts: dict[str, float] = {}
_GOOGLE_ROUTE_ENV = "AADS_ALLOW_GOOGLE_COMMERCIAL_ROUTES"
_DEEPSEEK_MODELS_URL = (os.getenv("DEEPSEEK_MODELS_URL") or "https://api.deepseek.com/models").strip()
_DISCOVERY_STALE_AFTER_SECONDS = max(
    3600.0, float(os.getenv("LLM_MODEL_DISCOVERY_STALE_HOURS", "72")) * 3600.0
)

_GLOBAL_DISCOVERY_PROVIDER = "all"
_DISCOVERY_PROVIDERS = (
    "openai", "anthropic", "gemini", "litellm", "kimi", "deepseek", "codex",
    "groq", "mistral", "openrouter", "minimax", "together", "cerebras", "sambanova",
)

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
    "mistral": "mistral",
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


def _json_default(value: Any) -> str:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


_MODEL_COSTS: dict[str, tuple[Decimal, Decimal]] = {
    "claude-opus": (_decimal(4.0), _decimal(20.0)),
    "claude-opus-5-5": (_decimal(4.0), _decimal(20.0)),
    "claude-opus-5": (_decimal(5.0), _decimal(25.0)),
    "claude-opus-46": (_decimal(5.0), _decimal(25.0)),
    "claude-sonnet": (_decimal(3.0), _decimal(15.0)),
    "claude-haiku": (_decimal(1.0), _decimal(5.0)),
    "claude-sonnet-5": (_decimal(2.0), _decimal(10.0)),
    "claude-sonnet-5-5": (_decimal(2.0), _decimal(10.0)),
    "claude-fable-5": (_decimal(10.0), _decimal(50.0)),
    "claude-fable-5-1": (_decimal(10.0), _decimal(50.0)),
    "gemini-flash": (_decimal(0.075), _decimal(0.3)),
    "gemini-flash-lite": (_decimal(0.01), _decimal(0.04)),
    "gemini-pro": (_decimal(1.25), _decimal(5.0)),
    "gemini-3-flash-preview": (_decimal(0.5), _decimal(3.0)),
    "gemini-3.1-flash-lite-preview": (_decimal(0.25), _decimal(1.5)),
    "gemini-3.1-pro-preview": (_decimal(2.0), _decimal(12.0)),
    "gemini-2.5-flash": (_decimal(0.15), _decimal(0.6)),
    "gemini-2.5-flash-lite": (_decimal(0.04), _decimal(0.1)),
    "groq-gpt-oss-120b": (_decimal(0.15), _decimal(0.60)),
    "groq-gpt-oss-20b": (_decimal(0.075), _decimal(0.30)),
    "groq-qwen3.8-27b": (_decimal(0.80), _decimal(4.0)),
    "gpt-4o": (_decimal(2.5), _decimal(10.0)),
    "gpt-4o-mini": (_decimal(0.15), _decimal(0.6)),
    "gpt-5": (_decimal(5.0), _decimal(15.0)),
    "gpt-5-mini": (_decimal(0.5), _decimal(2.0)),
    "o3": (_decimal(2.0), _decimal(8.0)),
    "o3-mini": (_decimal(1.1), _decimal(4.4)),
    "o3-pro": (_decimal(20.0), _decimal(80.0)),
    "gpt-6-astra": (_decimal(10.0), _decimal(50.0)),
    "gpt-6-sol": (_decimal(2.0), _decimal(10.0)),
    "gpt-6.1-sol": (_decimal(2.0), _decimal(10.0)),
    "gpt-6.1-terra": (_decimal(1.0), _decimal(5.0)),
    "gpt-6-luna": (_decimal(0.1), _decimal(0.5)),
    "gpt-5.6-sol": (_decimal(4.0), _decimal(20.0)),
    "gpt-5.6-tela": (_decimal(3.0), _decimal(15.0)),
    "gpt-5.6-terra": (_decimal(2.0), _decimal(12.0)),
    "gpt-5.6-luna": (_decimal(0.2), _decimal(1.2)),
    "gpt-5.5": (_decimal(2.0), _decimal(12.0)),
    "gpt-5.4": (_decimal(2.5), _decimal(15.0)),
    "gpt-5.4-mini": (_decimal(0.75), _decimal(4.5)),
    "gpt-5.3-codex": (_decimal(1.75), _decimal(14.0)),
    "deepseek-flash": (_decimal(0.3), _decimal(1.2)),
    "deepseek-v4-pro": (_decimal(1.32), _decimal(3.96)),
    "openrouter-grok-4-fast": (_decimal(0.2), _decimal(0.2)),
    "openrouter-deepseek-v3": (_decimal(0.26), _decimal(0.26)),
    "openrouter-mistral-small": (_decimal(0.15), _decimal(0.15)),
    "openrouter-nemotron-free": (_decimal(0.0), _decimal(0.0)),
    "openrouter-minimax-m2": (_decimal(0.3), _decimal(0.3)),
    "openrouter-grok-4.7": (_decimal(1.6), _decimal(4.8)),
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
    "kimi-k2.6": (_decimal(0.6), _decimal(2.4)),
    "kimi-k2.5": (_decimal(0.6), _decimal(2.4)),
    "kimi-k2": (_decimal(0.6), _decimal(2.4)),
    "kimi-latest": (_decimal(0.02), _decimal(0.06)),
    "kimi-128k": (_decimal(0.06), _decimal(0.24)),
    "kimi-8k": (_decimal(0.02), _decimal(0.06)),
    "minimax-m2.7": (_decimal(0.5), _decimal(2.0)),
    "minimax-m2.5": (_decimal(0.3), _decimal(1.2)),
}

_THINKING_MODELS = {
    "claude-opus-5-5",
    "claude-sonnet-5-5",
    "claude-sonnet-5",
    "claude-fable-5",
    "claude-fable-5-1",
    "gemini-pro",
    "gemini-flash",
    "gemini-3-flash-preview",
    "gemini-3-pro-preview",
    "gemini-3.1-flash-lite-preview",
    "gemini-3.1-pro-preview",
    "gemini-3.8-flash",
    "gemini-3.8-live-extended-thinking",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-2.5-flash",
    "gemini-2.5-pro",
    "deepseek-v4-pro",
    "deepseek-flash",
    "deepseek-reasoner",
    "qwen3-235b-thinking",
    "qwen3.7-plus",
    "qwen3.7-max",
    "qwen3.8-max",
    "qwq-plus",
    "gpt-5.6-sol",
    "gpt-5.6-tela",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
    "gpt-6-astra",
    "gpt-6-sol",
    "gpt-6.1-sol",
    "gpt-6.1-terra",
    "o3",
    "o3-mini",
    "o3-pro",
}

_VISION_MODELS = {
    "claude-opus-5-5",
    "claude-sonnet-5-5",
    "claude-sonnet-5",
    "claude-fable-5",
    "claude-fable-5-1",
    "gpt-4o",
    "gpt-4o-mini",
    "gpt-6-astra",
    "gpt-6-sol",
    "gpt-6.1-sol",
    "gpt-6.1-terra",
    "gpt-6-luna",
    "gemini-2.5-flash-image",
    "gemini-3.8-flash",
    "kimi-k3",
    "qwen-vl-max",
    "qwen-vl-plus",
    "qwen3-vl-plus",
    "qwen3-vl-235b",
    "qwen-omni-turbo",
}

_CODING_MODELS = {
    "claude-opus",
    "claude-opus-5-5",
    "claude-opus-5",
    "claude-opus-46",
    "claude-sonnet",
    "claude-haiku",
    "claude-sonnet-5",
    "claude-sonnet-5-5",
    "claude-fable-5",
    "claude-fable-5-1",
    "gpt-5",
    "gpt-5-mini",
    "gpt-5.4",
    "gpt-5.4-mini",
    "gpt-5.3-codex",
    "gpt-6-astra",
    "gpt-6-sol",
    "gpt-6.1-sol",
    "gpt-6.1-terra",
    "gpt-6-luna",
    "gpt-5.6-sol",
    "gpt-5.6-tela",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
    "gpt-5.5",
    "o3",
    "o3-mini",
    "o3-pro",
    "qwen3-coder-plus",
    "qwen3-coder-flash",
    "qwen3-coder-480b",
    "qwen3.6-plus",
    "qwen3.7-plus",
    "qwen3.7-max",
    "qwen3.8-max",
    "qwen-coder-plus",
}

_DISPLAY_NAME_OVERRIDES = {
    "claude-opus": "Claude Opus 5.5 (latest, Claude CLI)",
    "claude-opus-5-5": "Claude Opus 5.5 (Claude CLI)",
    "claude-opus-5": "Claude Opus 5 (Claude CLI)",
    "claude-opus-46": "Claude Opus 4.6",
    "claude-sonnet": "Claude Sonnet",
    "claude-haiku": "Claude Haiku",
    "claude-sonnet-5": "Claude Sonnet 5",
    "claude-sonnet-5-5": "Claude Sonnet 5.5",
    "claude-fable-5": "Claude Fable 5",
    "claude-fable-5-1": "Claude Fable 5.1",
    "gpt-6-astra": "GPT-6 Astra",
    "gpt-6-sol": "GPT-6 Sol",
    "gpt-6.1-sol": "GPT-6.1 Sol",
    "gpt-6.1-terra": "GPT-6.1 Terra",
    "gpt-6-luna": "GPT-6 Luna",
    "gpt-5.4": "GPT-5.4 (Codex CLI)",
    "gpt-5.4-mini": "GPT-5.4 Mini (Codex CLI)",
    "gpt-5.3-codex": "GPT-5.3 Codex (Codex CLI)",
    "gpt-5.6-sol": "GPT-5.6 Sol (Codex CLI)",
    "gpt-5.6-terra": "GPT-5.6 Terra (Codex CLI)",
    "gpt-5.6-luna": "GPT-5.6 Luna (Codex CLI)",
    "gpt-5.5": "GPT-5.5 (Codex CLI)",
    "deepseek-flash": "DeepSeek V4.1 Flash",
    "deepseek-v4-pro": "DeepSeek V4 Pro",
    "deepseek-chat": "DeepSeek Chat (compat alias -> V4 Flash)",
    "deepseek-reasoner": "DeepSeek Reasoner (compat alias -> V4 Pro)",
    "openrouter-grok-4-fast": "OpenRouter Grok 4 Fast",
    "openrouter-deepseek-v3": "OpenRouter DeepSeek V3",
    "openrouter-mistral-small": "OpenRouter Mistral Small",
    "openrouter-nemotron-free": "OpenRouter Nemotron Free",
    "openrouter-minimax-m2": "OpenRouter MiniMax M2",
    "openrouter-grok-4.7": "OpenRouter Grok 4.7",
    "openrouter-kimi-k3": "OpenRouter Kimi K3",
    "openrouter-minimax-m3": "OpenRouter MiniMax M3",
    "openrouter-qwen3.8-max": "OpenRouter Qwen 3.8 Max",
    "mistral-medium-latest": "Mistral Medium 3.5",
    "mistral-small-latest": "Mistral Small 4",
    "mistral-large-latest": "Mistral Large 3",
    "dashscope-deepseek-v3.2": "DashScope DeepSeek V3.2",
}

_PROVIDER_MODELS: dict[str, tuple[str, ...]] = {
    "anthropic": (
        "claude-fable-5",
        "claude-fable-5-1",
        "claude-opus",
        "claude-opus-5-5",
        "claude-opus-5",
        "claude-opus-46",
        "claude-sonnet",
        "claude-sonnet-5",
        "claude-sonnet-5-5",
        "claude-haiku",
    ),
    "gemini": (
        "gemini-flash",
        "gemini-flash-lite",
        "gemini-pro",
        "gemini-3-flash-preview",
        "gemini-3.8-flash",
        "gemini-3.8-live",
        "gemini-3.8-live-extended-thinking",
        "gemini-3.7-flash",
        "gemini-3.6-flash",
        "gemini-3.5-flash",
        "gemini-3.5-flash-lite",
        "gemini-3.1-flash-lite",
        "gemini-3.1-pro-preview",
        "gemini-2.5-flash",
        "gemini-2.5-flash-lite",
        "gemini-2.5-pro",
        "gemini-2.5-flash-image",
        "gemma-3-27b-it",
    ),
    "groq": (
        "groq-gpt-oss-120b",
        "groq-gpt-oss-20b",
        "groq-qwen3.8-27b",
    ),
    "openai": (
        "gpt-6-astra",
        "gpt-6-sol",
        "gpt-6.1-sol",
        "gpt-6.1-terra",
        "gpt-6-luna",
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
        "gpt-4o",
        "gpt-4o-mini",
        "gpt-5",
        "gpt-5-mini",
        "o3",
        "o3-mini",
        "o3-pro",
    ),
    "codex": ("gpt-6-astra", "gpt-6-sol", "gpt-6-luna", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5", "gpt-5.4", "gpt-5.4-mini", "gpt-5.3-codex"),
    "deepseek": ("deepseek-flash", "deepseek-v4-pro"),
    "openrouter": (
        "openrouter-grok-4-fast",
        "openrouter-deepseek-v3",
        "openrouter-mistral-small",
        "openrouter-nemotron-free",
        "openrouter-minimax-m2",
        "openrouter-grok-4.7",
        "openrouter-kimi-k3",
        "openrouter-minimax-m3",
        "openrouter-qwen3.8-max",
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
        "qwen3.6-plus",
        "qwen3.7-plus",
        "qwen3.7-max",
        "qwen3.8-max",
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
    "kimi": ("kimi-k3", "kimi-k2.6", "kimi-k2.5", "kimi-k2", "kimi-latest", "kimi-128k", "kimi-8k"),
    "minimax": ("minimax-m3", "minimax-m2.7", "minimax-m2.5"),
    "mistral": ("mistral-medium-latest", "mistral-small-latest", "mistral-large-latest"),
}

_KEYLESS_PROVIDERS = {"codex", "antigravity"}  # relay 서버 경유, 별도 API key 불필요

_DEEPSEEK_ALIAS_DEPRECATION_DATE = "2026-07-24"
_DEEPSEEK_COMPATIBILITY_ALIASES = {
    "deepseek-chat": "deepseek-flash",
    "deepseek-v4-flash": "deepseek-flash",
    "deepseek-reasoner": "deepseek-v4-pro",
}
_DEEPSEEK_LITELLM_RUNTIME_ALIASES = {
    "deepseek-flash": "deepseek-flash",
    "deepseek-v4-pro": "deepseek-v4-pro",
}

_OPENROUTER_RUNTIME_MODEL_IDS = {
    "openrouter-grok-4.7": "x-ai/grok-4.7",
    "openrouter-kimi-k3": "moonshotai/kimi-k3",
    "openrouter-minimax-m3": "minimax/minimax-m3",
    "openrouter-qwen3.8-max": "qwen/qwen3.8-max-0902",
}

_GROQ_RUNTIME_MODEL_IDS = {
    "groq-gpt-oss-120b": "openai/gpt-oss-120b",
    "groq-gpt-oss-20b": "openai/gpt-oss-20b",
    "groq-qwen3.8-27b": "qwen/qwen3.8-27b",
}

# Officially shut-down endpoints must not be revived by provider discovery.
_FORCED_RETIRED_MODELS = {
    ("anthropic", "claude-opus-4-1-20250805"),
    ("anthropic", "claude-opus-4-20250514"),
    ("anthropic", "claude-sonnet-4-20250514"),
    ("anthropic", "claude-3-7-sonnet-20250219"),
    ("anthropic", "claude-3-5-haiku-20241022"),
    ("anthropic", "claude-3-haiku-20240307"),
    ("deepseek", "deepseek-chat"),
    ("deepseek", "deepseek-reasoner"),
    ("deepseek", "deepseek-v4-flash"),
    ("gemini", "gemini-2.0-flash"),
    ("gemini", "gemini-2.0-flash-lite"),
    ("gemini", "gemini-3.1-flash-lite-preview"),
    ("gemini", "gemini-3-pro-preview"),
    ("groq", "groq-compound"),
    ("groq", "groq-kimi-k2"),
    ("groq", "groq-llama-70b"),
    ("groq", "groq-llama-8b"),
    ("groq", "groq-llama4-scout"),
    ("groq", "groq-qwen3-32b"),
    ("openai", "gpt-5.2-chat-latest"),
    ("openai", "gpt-5.3-chat-latest"),
}
from scripts.claude_model_contract import AADS_MODEL_IDS  # noqa: E402

_ANTHROPIC_RUNTIME_MODEL_IDS = dict(AADS_MODEL_IDS)
_MODEL_ACCEPTED_ALIASES: dict[str, tuple[str, ...]] = {
    "claude-sonnet-5-5": ("claude-sonnet-5-5",),
    "claude-fable-5-1": (
        "claude-fable-5.1",
        "claude-fable-latest",
    ),
    "claude-fable-5": (
        "claude-fable-5",
    ),
    "claude-sonnet-5": (
        "claude-sonnet-5",
    ),
    "claude-sonnet": (
        "claude-sonnet-4-6",
    ),
    "claude-opus-46": ("claude-opus-4-6",),
    "claude-haiku": (
        "claude-haiku-4-5",
        "claude-haiku-4-5-20251001",
    ),
}
_MODEL_CANONICAL_ALIASES: dict[str, str] = {
    alias: canonical
    for canonical, aliases in _MODEL_ACCEPTED_ALIASES.items()
    for alias in aliases
    if alias != canonical
}

_DISCOVERY_REQUIREMENTS = {
    "anthropic": "x-api-key required for Models API; OAuth auth token supports runtime only",
    "openai": "Bearer provider key required",
    "gemini": "Google Generative Language API key required",
    "litellm": "LiteLLM master key and /model/info endpoint required",
    "kimi": "Moonshot provider key required for GET /v1/models",
    "deepseek": "DeepSeek provider key (central key store) required for official GET /models",
    "groq": "Groq provider key required for GET /openai/v1/models",
    "mistral": "Mistral provider key required for GET /v1/models",
    "openrouter": "OpenRouter provider key required for GET /api/v1/models",
    "minimax": "MiniMax provider key required for GET /v1/models",
    "together": "Together provider key required for GET /v1/models",
    "cerebras": "Cerebras provider key required for GET /v1/models",
    "sambanova": "SambaNova provider key required for GET /v1/models",
}

_PROVIDER_META = {
    "anthropic": {"display_name": "Anthropic", "manual_review": False},
    "gemini": {"display_name": "Gemini", "manual_review": False},
    "groq": {"display_name": "Groq", "manual_review": False},
    "openai": {"display_name": "OpenAI", "manual_review": False},
    "codex": {"display_name": "Codex CLI", "manual_review": False},
    "antigravity": {"display_name": "Antigravity CLI", "manual_review": False},
    "deepseek": {"display_name": "DeepSeek", "manual_review": False},
    "openrouter": {"display_name": "OpenRouter", "manual_review": False},
    "qwen": {"display_name": "Qwen / DashScope", "manual_review": False},
    "kimi": {"display_name": "Kimi", "manual_review": False},
    "minimax": {"display_name": "MiniMax", "manual_review": False},
    "mistral": {"display_name": "Mistral", "manual_review": False},
}

_DIRECT_PROVIDER_BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "groq": "https://api.groq.com/openai/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "qwen": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
    "kimi": "https://api.moonshot.ai/v1",
    "minimax": "https://api.minimax.chat/v1",
    "mistral": "https://api.mistral.ai/v1",
}

# OpenAI 호환 GET <base>/models 로 목록을 받는 회사 — 회사마다 함수를 복사하지 않고 이 표 하나로 수집한다.
# 실행 경로(_DIRECT_PROVIDER_BASE_URLS)가 있는 회사는 같은 값을 쓴다. 공식 문서로 확인한 값만 둔다(2026-11-05).
# 미확인이라 아직 넣지 않은 회사: qwen(DashScope /models 문서 없음), nvidia(공식 문서에 /v1/models 없음).
_OPENAI_COMPAT_DISCOVERY_BASE_URLS: dict[str, str] = {
    "groq": _DIRECT_PROVIDER_BASE_URLS["groq"],
    "mistral": _DIRECT_PROVIDER_BASE_URLS["mistral"],
    "openrouter": _DIRECT_PROVIDER_BASE_URLS["openrouter"],
    # 공식 List Models 는 api.minimax.io 이고 실행 경로(api.minimax.chat)와 다르다 — 실행 경로는 이 작업에서 바꾸지 않는다.
    "minimax": "https://api.minimax.io/v1",
    "together": "https://api.together.ai/v1",
    "cerebras": "https://api.cerebras.ai/v1",
    "sambanova": "https://api.sambanova.ai/v1",
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


def _display_name_for_provider(provider: str, model_id: str) -> str:
    if provider == "codex" and model_id in {"gpt-6-astra", "gpt-6-sol"}:
        return f"{_display_name_for(model_id)} (Codex CLI)"
    override = _DISPLAY_NAME_OVERRIDES.get(model_id)
    if override:
        return override
    if provider == "codex":
        return _display_name_for(model_id)
    if provider == "openai" and model_id.startswith("gpt-"):
        return f"GPT-{model_id[4:].replace('-', ' ')}"
    return _display_name_for(model_id)


def _canonical_model_id(model_id: str) -> str:
    return _MODEL_CANONICAL_ALIASES.get(
        model_id,
        _DEEPSEEK_COMPATIBILITY_ALIASES.get(model_id, model_id),
    )


def _runtime_model_id(provider: str, model_id: str) -> str:
    if provider == "deepseek":
        return _DEEPSEEK_LITELLM_RUNTIME_ALIASES.get(model_id, model_id)
    if provider == "openrouter":
        return _OPENROUTER_RUNTIME_MODEL_IDS.get(model_id, model_id)
    if provider == "groq":
        return _GROQ_RUNTIME_MODEL_IDS.get(model_id, model_id)
    return _canonical_model_id(model_id)


def _commercial_metadata(provider: str, model_id: str) -> dict[str, Any]:
    if provider == "codex":
        return {
            "billing_mode": "chatgpt_subscription_oauth",
            "subscription_plans": ["Plus", "Pro", "Business", "Enterprise"],
            "api_billing_separate": True,
        }
    if provider == "anthropic":
        return {
            "billing_mode": "claude_subscription_oauth",
            "subscription_plans": ["Pro", "Max", "Team", "Enterprise"],
            "api_billing_separate": True,
        }
    if provider == "gemini":
        return {
            "billing_mode": "api_key",
            "consumer_subscription_plans": ["Google AI Plus", "Google AI Pro", "Google AI Ultra"],
            "api_billing_separate": True,
        }
    if provider == "qwen":
        return {
            "billing_mode": "api_key",
            "coding_plan_available": model_id in {"qwen3.7-plus", "qwen3.6-plus", "qwen3.5-plus", "qwen3-coder-plus"},
            "api_billing_separate": True,
        }
    if provider == "kimi":
        return {
            "billing_mode": "api_key",
            "kimi_membership_available": model_id in {"kimi-k3", "kimi-k2.6"},
            "api_billing_separate": True,
        }
    if provider == "minimax":
        return {
            "billing_mode": "api_key",
            "token_plan_available": model_id in {"minimax-m3", "minimax-m2.7", "minimax-m2.5"},
            "api_billing_separate": True,
        }
    if provider == "openai":
        return {"billing_mode": "api_key", "api_billing_separate_from_chatgpt": True}
    return {"billing_mode": "api_key"}


def _is_retired_catalog_row(row: dict[str, Any]) -> bool:
    if (str(row.get("provider") or ""), str(row.get("model_id") or "")) in _FORCED_RETIRED_MODELS:
        return True
    raw = _coerce_json_object(_coerce_json_object(row.get("metadata")).get("raw"))
    shutdown_date = str(raw.get("shutdown_date") or "").strip()
    if not shutdown_date:
        return False
    try:
        return date.fromisoformat(shutdown_date) <= datetime.now(timezone.utc).date()
    except ValueError:
        return False


def _compatibility_alias_metadata(model_id: str) -> dict[str, Any]:
    canonical_model = _DEEPSEEK_COMPATIBILITY_ALIASES.get(model_id)
    if not canonical_model:
        return {}
    return {
        "canonical_model": canonical_model,
        "deprecation_date": _DEEPSEEK_ALIAS_DEPRECATION_DATE,
        "compatibility_alias": True,
    }


def _accepted_alias_metadata(provider: str, model_id: str) -> dict[str, Any]:
    aliases: list[str] = []
    if provider == "anthropic":
        aliases.extend(_MODEL_ACCEPTED_ALIASES.get(model_id, ()))
    if not aliases:
        return {}
    return {"accepted_aliases": aliases}


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
    execution_model_id = _runtime_model_id(provider, model_id)
    if provider == "deepseek":
        execution_backend = "litellm_proxy"
    elif provider in _DIRECT_PROVIDER_BASE_URLS:
        execution_backend = "openai_compatible_direct"
        execution_base_url = _DIRECT_PROVIDER_BASE_URLS[provider]
    elif provider == "codex":
        execution_backend = "codex_cli"
    elif provider == "anthropic":
        execution_backend = "claude_cli_relay"
        execution_model_id = _ANTHROPIC_RUNTIME_MODEL_IDS.get(model_id, execution_model_id)
    elif provider == "gemini":
        execution_backend = "litellm_proxy"
    return ModelTemplate(
        provider=provider,
        model_id=model_id,
        display_name=_display_name_for_provider(provider, model_id),
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


def _model_capabilities(provider: str, model_id: str, raw: dict[str, Any] | None = None) -> dict[str, Any]:
    capabilities = {
        "tools": _supports_tools_for(provider),
        "thinking": model_id in _THINKING_MODELS or _category_for(model_id) == "reasoning",
        "vision": model_id in _VISION_MODELS or _category_for(model_id) == "vision",
        "coding": model_id in _CODING_MODELS or _category_for(model_id) == "coding" or provider in {"anthropic", "codex"},
        "raw_generation_methods": (raw or {}).get("supportedGenerationMethods", []),
    }
    if provider in {"openai", "codex"} and model_id == "gpt-6-astra":
        capabilities.update(
            {
                "computer_use": True,
                "file_search": True,
                "web_search": True,
                "reasoning_effort": ["low", "medium", "high", "xhigh", "max"],
                "max_output_tokens": 128000,
                "context_window": 1050000,
                "knowledge_cutoff": "2026-04-30",
                "supports_custom_temperature": False,
                "supports_custom_top_p": False,
                "supports_logprobs": False,
            }
        )
        if provider == "openai":
            capabilities["responses_api_required_for_tools"] = True
        if provider == "codex":
            capabilities.update({"codex_cli": True, "chatgpt_credits": True})
    return capabilities


def _pricing_for(model_id: str) -> dict[str, str]:
    costs = _MODEL_COSTS.get(model_id)
    if not costs:
        return {}
    return {
        "input_cost": str(costs[0]) if costs[0] is not None else "",
        "output_cost": str(costs[1]) if costs[1] is not None else "",
        "unit": "usd_per_1m_tokens",
    }


def _anthropic_key_supports_models_api(key_name: str) -> bool:
    """호환용. 예전에는 OAuth 키 이름을 제외했지만 지금은 OAuth/문서 수집이 있어 키 이름이 있으면 수집 가능이다."""
    return _provider_auto_discovery_supported(
        "anthropic", {"available_key_count": 1 if str(key_name or "").strip() else 0}
    )


def _provider_auto_discovery_supported(provider: str, provider_state: dict[str, Any]) -> bool:
    supported = {"openai", "anthropic", "gemini", "litellm", "kimi", "deepseek", *_OPENAI_COMPAT_DISCOVERY_BASE_URLS}
    return provider in supported and int(provider_state.get("available_key_count", 0)) > 0


def _provider_discovery_requirement(provider: str) -> str:
    return _DISCOVERY_REQUIREMENTS.get(provider, "Provider catalog API key required")


def _provider_discovery_mode(
    provider: str,
    *,
    runtime_executable: bool,
    auto_discovery_supported: bool,
) -> str:
    if provider not in _DISCOVERY_REQUIREMENTS:
        return "template"
    if auto_discovery_supported:
        return "discovery"
    if runtime_executable:
        return "template_runtime_only"
    return "template"


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


def _collect_provider_normalizations(key_state: dict[str, dict[str, Any]]) -> dict[str, int]:
    """원본 provider 별칭이 정규화된 경우 최근 sync 메타데이터로 남긴다."""
    normalized_counts: dict[str, int] = {}
    for provider, state in key_state.items():
        for raw_provider in state.get("raw_providers", set()):
            raw = str(raw_provider or "").strip()
            if not raw:
                continue
            normalized = normalize_provider(raw)
            if not normalized or normalized == raw:
                continue
            rule = f"{raw}->{provider or normalized}"
            normalized_counts[rule] = normalized_counts.get(rule, 0) + 1
    return normalized_counts


_NON_TEXT_MODEL_TOKENS = (
    "audio",
    "dall-e",
    "embedding",
    "image",
    "moderation",
    "realtime",
    "rerank",
    "search",
    "transcribe",
    "tts",
    "whisper",
)
_OPENAI_TEXT_PREFIXES = ("gpt-", "chatgpt-", "o1", "o3", "o4", "o5")


def _is_text_model_candidate(provider: str, model_id: str, raw: dict[str, Any] | None = None) -> bool:
    """자동 활성화 심사 대상(텍스트 생성 모델)인가. 활성화 여부는 이 함수가 아니라 호출 검증이 정한다."""
    lowered = model_id.lower()
    if not lowered or any(token in lowered for token in _NON_TEXT_MODEL_TOKENS):
        return False
    if provider == "anthropic":
        return lowered.startswith("claude-")
    if provider == "openai":
        return lowered.startswith(_OPENAI_TEXT_PREFIXES)
    if provider == "gemini":
        methods = set((raw or {}).get("supportedGenerationMethods") or [])
        return lowered.startswith("gemini-") and (not methods or "generateContent" in methods)
    return True


def _is_auto_executable_discovered(provider: str, model_id: str, raw: dict[str, Any] | None = None) -> bool:
    """호환용. 'image' 등 비텍스트 id 제외 동작은 유지하고 판정은 _is_text_model_candidate 에 위임한다.

    발견만으로 실행 가능이 되지는 않는다(호출 검증 apply_activation_probes 가 켠다).
    """
    return _is_text_model_candidate(provider, model_id, raw)


def _discovered_model_row(
    *,
    provider: str,
    model_id: str,
    display_name: str,
    key_state: dict[str, dict[str, Any]],
    raw: dict[str, Any] | None = None,
    source: str,
) -> dict[str, Any]:
    provider_state = key_state.get(provider, {})
    available_key_count = int(provider_state.get("available_key_count", 0))
    active_key_count = int(provider_state.get("active_key_count", 0))
    keyless = provider in _KEYLESS_PROVIDERS
    has_runtime_models = keyless or available_key_count > 0
    auto_discovery_supported = _provider_auto_discovery_supported(provider, provider_state)
    # 발견만으로는 활성화하지 않는다. 호출 검증(apply_activation_probes)을 통과해야 켜진다.
    executable = False
    activation_candidate = has_runtime_models and _is_text_model_candidate(provider, model_id, raw)
    template = _build_template(provider, model_id)
    metadata = {
        "template_provider": provider,
        "model_source": "discovery",
        "discovered": True,
        "discovery_source": source,
        "runtime_executable": has_runtime_models,
        "auto_discovery_supported": auto_discovery_supported,
        "discovery_requirement": _provider_discovery_requirement(provider),
        "discovery_mode": _provider_discovery_mode(
            provider,
            runtime_executable=has_runtime_models,
            auto_discovery_supported=auto_discovery_supported,
        ),
        "raw_provider_aliases": sorted(provider_state.get("raw_providers", set())),
        "active_key_count": active_key_count,
        "available_key_count": available_key_count,
        "requires_admin_review": True,
        "activation_candidate": activation_candidate,
        "execution_backend": template.execution_backend,
        "execution_model_id": template.execution_model_id,
        "execution_base_url": template.execution_base_url,
        "raw": raw or {},
    }
    metadata.update(_reviewed_release_fields(provider, model_id))
    metadata.update(_compatibility_alias_metadata(model_id))
    metadata.update(_accepted_alias_metadata(provider, model_id))
    metadata.update(_commercial_metadata(provider, model_id))
    return {
        "provider": provider,
        "model_id": model_id,
        "display_name": display_name or _display_name_for_provider(provider, model_id),
        "family": _family_for(provider, model_id),
        "category": _category_for(model_id),
        "supports_tools": template.supports_tools,
        "supports_thinking": template.supports_thinking,
        "supports_vision": template.supports_vision,
        "supports_coding": template.supports_coding,
        "input_cost": template.input_cost,
        "output_cost": template.output_cost,
        "is_active": executable,
        "activation_source": "db" if executable else "review_required" if active_key_count else "fallback",
        "linked_key_name": None if keyless else _pick_linked_key(list(provider_state.get("keys", []))),
        "metadata": metadata,
        "execution_model_id": template.execution_model_id,
        "discovery_source": source,
        "verification_status": "discovered" if executable else "review_required",
        "last_verified_at": provider_state.get("last_verified_at"),
        "capabilities": _model_capabilities(provider, model_id, raw),
        "pricing": _pricing_for(model_id),
        "is_selectable": executable,
        "is_executable": executable,
    }


async def _get_first_provider_key(
    provider: str,
    *,
    excluded_key_name_prefixes: tuple[str, ...] = (),
    required_value_prefixes: tuple[str, ...] = (),
) -> str:
    try:
        from app.core.llm_key_provider import get_provider_key_records

        key_records = await get_provider_key_records(provider, include_rate_limited=False)
    except Exception:
        logger.exception("model_registry.discovery_key_failed", extra={"provider": provider})
        return ""
    excluded_prefixes = tuple(prefix.upper() for prefix in excluded_key_name_prefixes)
    for record in key_records:
        key_name = str(record.get("key_name") or "").upper()
        value = str(record.get("value") or "").strip()
        if not value:
            continue
        if excluded_prefixes and key_name.startswith(excluded_prefixes):
            continue
        if required_value_prefixes and not value.startswith(required_value_prefixes):
            continue
        return value
    return ""


async def _get_anthropic_models_api_key_and_runtime_state() -> tuple[str, bool]:
    try:
        from app.core.llm_key_provider import get_provider_key_records

        key_records = await get_provider_key_records("anthropic", include_rate_limited=False)
    except Exception:
        logger.exception("model_registry.discovery_key_failed", extra={"provider": "anthropic"})
        return "", False

    models_api_key = ""
    oauth_runtime_executable = False
    for record in key_records:
        key_name = str(record.get("key_name") or "").upper()
        value = str(record.get("value") or "").strip()
        if not value:
            continue
        if key_name.startswith("ANTHROPIC_AUTH_TOKEN") or value.startswith("sk-ant-oat"):
            oauth_runtime_executable = True
            continue
        if value.startswith("sk-ant-api") and not models_api_key:
            models_api_key = value
    return models_api_key, oauth_runtime_executable


async def _fetch_openai_models() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    api_key = await _get_first_provider_key("openai")
    key_source = "db"
    if not api_key:
        api_key = (os.getenv("OPENAI_API_KEY") or "").strip()
        key_source = "env"
    if not api_key:
        return [], {"status": "skipped", "error": "missing_key"}
    async with httpx.AsyncClient(timeout=_DISCOVERY_TIMEOUT_SECONDS) as client:
        response = await client.get(
            "https://api.openai.com/v1/models",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        response.raise_for_status()
        payload = response.json()
    rows = []
    for item in payload.get("data", []):
        model_id = str(item.get("id") or "").strip()
        if model_id:
            rows.append({"model_id": model_id, "display_name": _display_name_for_provider("openai", model_id), "raw": item})
    return rows, {"status": "ok", "count": len(rows), "key_source": key_source}


_ANTHROPIC_DOCS_URL = (
    os.getenv("ANTHROPIC_MODELS_DOCS_URL") or "https://docs.anthropic.com/en/docs/about-claude/models/overview"
).strip()
_ANTHROPIC_DOCS_CACHE_SECONDS = 24 * 3600.0
_ANTHROPIC_DOCS_MAX_IDS = 60
_ANTHROPIC_OAUTH_BETA = "oauth-2025-04-20"
_ANTHROPIC_DOC_ID_RE = re.compile(r"(?<![\w.@/:-])claude-[a-z0-9]+(?:-[a-z0-9.]+)*(?![\w@:/])")
_anthropic_docs_cache: dict[str, Any] = {"at": 0.0, "ids": []}


def extract_claude_model_ids(text: str) -> list[str]:
    """공식 모델 문서 본문에서 claude-* 모델 id 후보를 뽑는다. 숫자가 없는 토큰(claude-code 등)과 Bedrock/Vertex 표기는 버린다."""
    found: list[str] = []
    for match in _ANTHROPIC_DOC_ID_RE.finditer(str(text or "").lower()):
        candidate = match.group(0).rstrip(".-")
        if not any(ch.isdigit() for ch in candidate) or candidate in found:
            continue
        found.append(candidate)
    return sorted(found)[:_ANTHROPIC_DOCS_MAX_IDS]


async def _list_anthropic_models_via_oauth() -> list[dict[str, Any]]:
    """OAuth 토큰(R-AUTH: anthropic_client/auth_provider 경유)으로 GET /v1/models. 거부되면 예외를 올린다."""
    from app.core.auth_provider import create_anthropic_client, get_oauth_tokens_async

    tokens = await get_oauth_tokens_async()
    if not tokens:
        raise RuntimeError("no_oauth_token")
    client = create_anthropic_client(token=tokens[0]).with_options(
        default_headers={"anthropic-beta": _ANTHROPIC_OAUTH_BETA},
        timeout=_DISCOVERY_TIMEOUT_SECONDS,
        max_retries=0,
    )
    page = await client.models.list(limit=100)
    rows: list[dict[str, Any]] = []
    for item in getattr(page, "data", None) or []:
        model_id = str(getattr(item, "id", "") or "").strip()
        if model_id:
            rows.append({
                "model_id": model_id,
                "display_name": getattr(item, "display_name", None) or _display_name_for(model_id),
                "raw": {"id": model_id, "created_at": str(getattr(item, "created_at", "") or "")},
            })
    return rows


async def _read_anthropic_docs_model_ids() -> tuple[list[str], float]:
    """공식 모델 문서를 하루 1회 읽는다(프로세스 캐시). (id 목록, 읽은 시각 epoch)."""
    now_ts = time.time()
    cached_ids = _anthropic_docs_cache.get("ids") or []
    if cached_ids and now_ts - float(_anthropic_docs_cache.get("at") or 0.0) < _ANTHROPIC_DOCS_CACHE_SECONDS:
        return list(cached_ids), float(_anthropic_docs_cache["at"])
    async with httpx.AsyncClient(timeout=_DISCOVERY_TIMEOUT_SECONDS, follow_redirects=True) as client:
        response = await client.get(_ANTHROPIC_DOCS_URL)
        response.raise_for_status()
        ids = extract_claude_model_ids(response.text)
    if not ids:
        raise ValueError("docs_no_model_ids")
    _anthropic_docs_cache.update({"at": now_ts, "ids": ids})
    return ids, now_ts


async def _fetch_anthropic_models() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """수집 순서: ① API 키 /v1/models ② OAuth /v1/models ③ 공식 모델 문서(claude-* id 후보).

    ③ 의 후보는 발견일 뿐이다. 활성화는 호출 검증(apply_activation_probes)을 통과해야 한다.
    """
    api_key, oauth_runtime_executable = await _get_anthropic_models_api_key_and_runtime_state()
    base_meta = {
        "runtime_executable": oauth_runtime_executable or bool(api_key),
        "auto_discovery_supported": True,
        "discovery_requirement": _provider_discovery_requirement("anthropic"),
    }
    if api_key:
        async with httpx.AsyncClient(timeout=_DISCOVERY_TIMEOUT_SECONDS) as client:
            response = await client.get(
                "https://api.anthropic.com/v1/models?limit=100",
                headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
            )
            response.raise_for_status()
            payload = response.json()
        rows = []
        for item in payload.get("data", []):
            model_id = str(item.get("id") or "").strip()
            if model_id:
                rows.append({"model_id": model_id, "display_name": item.get("display_name") or _display_name_for(model_id), "raw": item})
        return rows, {"status": "ok", "count": len(rows), "model_source": "discovery", "discovery_source": "anthropic_api", **base_meta}
    if not oauth_runtime_executable:
        return [], {
            "status": "skipped",
            "error": "missing_key",
            "runtime_executable": False,
            "auto_discovery_supported": False,
            "discovery_requirement": _provider_discovery_requirement("anthropic"),
            "model_source": "template",
        }
    oauth_models_error = ""
    try:
        rows = await _list_anthropic_models_via_oauth()
        if rows:
            return rows, {
                "status": "ok", "count": len(rows), "model_source": "discovery",
                "discovery_source": "anthropic_oauth_api", **base_meta,
            }
        oauth_models_error = "empty_list"
    except Exception as exc:
        oauth_models_error = getattr(exc, "status_code", None) and f"http_{exc.status_code}" or type(exc).__name__
    try:
        ids, fetched_ts = await _read_anthropic_docs_model_ids()
    except Exception as exc:
        return [], {
            "status": "skipped",
            "error": "oauth_runtime_only_models_api_unavailable",
            "docs_error": _discovery_error_code(exc, 120),
            "oauth_models_api": oauth_models_error,
            **{**base_meta, "auto_discovery_supported": False},
            "model_source": "template",
        }
    rows = [
        {"model_id": model_id, "display_name": _display_name_for(model_id), "raw": {"id": model_id, "source": "anthropic_models_docs"}}
        for model_id in ids
    ]
    return rows, {
        "status": "ok",
        "count": len(rows),
        "model_source": "discovery",
        "discovery_source": "anthropic_docs",
        "fetched_at": datetime.fromtimestamp(fetched_ts, tz=timezone.utc).isoformat(),
        "oauth_models_api": oauth_models_error,
        **base_meta,
    }


async def _fetch_gemini_models() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not await _google_commercial_routes_enabled():
        return [], {
            "status": "skipped",
            "error": "google_routes_disabled",
            "count": 0,
            "auto_discovery_supported": False,
            "discovery_requirement": "Google/Gemini routes disabled by DB policy",
            "model_source": "template",
        }
    api_key = await _get_first_provider_key("gemini")
    if not api_key:
        return [], {"status": "skipped", "error": "missing_key"}
    async with httpx.AsyncClient(timeout=_DISCOVERY_TIMEOUT_SECONDS) as client:
        response = await client.get("https://generativelanguage.googleapis.com/v1beta/models", params={"key": api_key})
        response.raise_for_status()
        payload = response.json()
    rows = []
    for item in payload.get("models", []):
        raw_name = str(item.get("name") or "").strip()
        model_id = raw_name.removeprefix("models/")
        if model_id:
            rows.append({"model_id": model_id, "display_name": item.get("displayName") or _display_name_for_provider("gemini", model_id), "raw": item})
    return rows, {"status": "ok", "count": len(rows)}


async def _google_commercial_routes_enabled() -> bool:
    """Gate Gemini/Google catalog calls behind the DB routing policy."""
    if os.getenv(_GOOGLE_ROUTE_ENV, "").strip().lower() in {"1", "true", "yes"}:
        return True
    try:
        pool = get_pool()
        async with pool.acquire() as conn:
            enabled = await conn.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM model_routing_preferences
                    WHERE provider IN ('google', 'gemini')
                      AND is_enabled = TRUE
                )
                """
            )
        return bool(enabled)
    except Exception as exc:
        logger.warning("model_registry.google_route_policy_check_failed: %s", str(exc)[:160])
        return False


async def _fetch_litellm_models() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    api_key = await _get_first_provider_key("litellm")
    if not api_key:
        return [], {"status": "skipped", "error": "missing_key"}
    base_url = (os.getenv("LITELLM_BASE_URL") or "http://aads-litellm:4000").rstrip("/")
    async with httpx.AsyncClient(timeout=_DISCOVERY_TIMEOUT_SECONDS) as client:
        response = await client.get(f"{base_url}/model/info", headers={"Authorization": f"Bearer {api_key}"})
        response.raise_for_status()
        payload = response.json()
    source_rows = payload.get("data") if isinstance(payload, dict) else payload
    rows = []
    for item in source_rows or []:
        model_id = str(item.get("model_name") or item.get("model_id") or item.get("id") or "").strip()
        if model_id:
            rows.append({"model_id": model_id, "display_name": _display_name_for_provider("litellm", model_id), "raw": item})
    return rows, {"status": "ok", "count": len(rows)}


async def _fetch_kimi_models() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    key = await _get_first_provider_key("kimi")
    if not key:
        return [], {"status": "skipped", "error": "no_kimi_key", "count": 0}
    try:
        async with httpx.AsyncClient(timeout=_DISCOVERY_TIMEOUT_SECONDS) as client:
            resp = await client.get(
                "https://api.moonshot.ai/v1/models",
                headers={"Authorization": f"Bearer {key}"},
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        return [], {"status": "failed", "error": _discovery_error_code(exc, 200), "count": 0}
    rows = [
        {"model_id": item["id"], "display_name": item.get("id", ""), "raw": item}
        for item in data.get("data", [])
        if isinstance(item, dict) and item.get("id")
    ]
    return rows, {"status": "ok", "count": len(rows), "model_source": "discovery"}


async def _fetch_codex_cli_models() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """codex CLI 가 ChatGPT 백엔드에서 받아 둔 카탈로그. 발견만 한다 — 실행가능 판정은 probe 몫."""
    from app.services.cli_model_autoreg import read_codex_models_cache

    return await asyncio.to_thread(read_codex_models_cache)


def _discovery_error_code(exc: BaseException, limit: int = 500) -> str:
    """HTTP 오류는 공급자와 무관한 코드(http_<status>)로 정규화한다. 예외 문자열에는 요청 URL(?key=...)이 실릴 수 있다."""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"http_{exc.response.status_code}"
    if isinstance(exc, httpx.HTTPError):
        return f"fetch_error:{type(exc).__name__}"
    return str(exc)[:limit]


def classify_deepseek_catalog(listed_ids: Iterable[str]) -> dict[str, Any]:
    """공식 목록을 canonical / alias / 미검증 신규로 나눈다. 목록에 없는 canonical 은 폐기가 아니라 '일시 미확인' 이다."""
    listed = sorted({str(item or "").strip() for item in listed_ids if str(item or "").strip()})
    canonical_ids = set(_PROVIDER_MODELS.get("deepseek", ()))
    alias_ids = sorted(item for item in listed if item in _DEEPSEEK_COMPATIBILITY_ALIASES)
    canonical_listed = sorted(item for item in listed if item in canonical_ids)
    served_via_alias = {_DEEPSEEK_COMPATIBILITY_ALIASES[item] for item in alias_ids}
    return {
        "canonical_ids": canonical_listed,
        "alias_ids": alias_ids,
        "unverified_new_ids": sorted(
            item for item in listed if item not in canonical_ids and item not in _DEEPSEEK_COMPATIBILITY_ALIASES
        ),
        "missing_canonical_ids": sorted(
            item for item in canonical_ids if item not in canonical_listed and item not in served_via_alias
        ),
        "absence_policy": "missing_is_transient; retirement only by forced list or official shutdown_date",
    }


async def _fetch_deepseek_models() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """DeepSeek 공식 GET /models. 키는 중앙 키 저장소에서만 읽고 어디에도 기록하지 않는다."""
    api_key = await _get_first_provider_key("deepseek")
    if not api_key:
        return [], {
            "status": "skipped",
            "error": "missing_key",
            "count": 0,
            "discovery_source": "deepseek_api",
            "auto_discovery_supported": False,
            "model_source": "template",
        }
    try:
        async with httpx.AsyncClient(timeout=_DISCOVERY_TIMEOUT_SECONDS) as client:
            response = await client.get(_DEEPSEEK_MODELS_URL, headers={"Authorization": f"Bearer {api_key}"})
            response.raise_for_status()
            payload = response.json()
    except Exception as exc:
        return [], {
            "status": "failed",
            "error": _discovery_error_code(exc),
            "count": 0,
            "discovery_source": "deepseek_api",
            "model_source": "template",
        }
    items = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(items, list) or payload.get("error"):
        # HTTP 200 이어도 목록 구조가 아니면 '빈 목록 성공' 이 아니라 실패다.
        return [], {
            "status": "failed",
            "error": "schema_invalid",
            "count": 0,
            "discovery_source": "deepseek_api",
            "model_source": "template",
        }
    rows = [
        {"model_id": str(item["id"]).strip(), "display_name": _display_name_for_provider("deepseek", str(item["id"]).strip()), "raw": item}
        for item in items
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    ]
    return rows, {
        "status": "ok",
        "count": len(rows),
        "discovery_source": "deepseek_api",
        "auto_discovery_supported": True,
        "model_source": "discovery",
        **classify_deepseek_catalog(row["model_id"] for row in rows),
    }


async def _fetch_openai_compatible_models(provider: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """표(_OPENAI_COMPAT_DISCOVERY_BASE_URLS)의 회사 공통 수집기. 키는 중앙 키 조회로만 읽고 결과·로그에 남기지 않는다."""
    source = f"{provider}_api"
    api_key = await _get_first_provider_key(provider)
    if not api_key:
        return [], {
            "status": "skipped",
            "error": "missing_key",
            "count": 0,
            "discovery_source": source,
            "auto_discovery_supported": False,
            "model_source": "template",
        }
    url = f"{_OPENAI_COMPAT_DISCOVERY_BASE_URLS[provider]}/models"
    failed = {"status": "failed", "count": 0, "discovery_source": source, "model_source": "template"}
    try:
        async with httpx.AsyncClient(timeout=_DISCOVERY_TIMEOUT_SECONDS) as client:
            response = await client.get(url, headers={"Authorization": f"Bearer {api_key}"})
            response.raise_for_status()
            payload = response.json()
    except httpx.HTTPError as exc:
        return [], {**failed, "error": _discovery_error_code(exc)}
    except ValueError:
        return [], {**failed, "error": "schema_invalid"}
    # Together 는 최상위 배열, 나머지는 {"data": [...]} 로 응답한다.
    items = payload if isinstance(payload, list) else payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(items, list) or (isinstance(payload, dict) and payload.get("error")):
        return [], {**failed, "error": "schema_invalid"}
    rows_by_id: dict[str, dict[str, Any]] = {}
    for item in items:
        model_id = str(item.get("id") or "").strip() if isinstance(item, dict) else ""
        if model_id and model_id not in rows_by_id:
            rows_by_id[model_id] = {
                "model_id": model_id,
                "display_name": _display_name_for_provider(provider, model_id),
                "raw": item,
            }
    rows = list(rows_by_id.values())
    return rows, {
        "status": "ok",
        "count": len(rows),
        "discovery_source": source,
        "auto_discovery_supported": True,
        "model_source": "discovery",
    }


def _parse_timestamp(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)):
        try:
            parsed = datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    else:
        try:
            parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _discovery_reason_class(status: str, error: str) -> str:
    if error == "oauth_runtime_only_models_api_unavailable":
        return "oauth_runtime_only"
    if error == "google_routes_disabled":
        return "route_policy_disabled"
    if error == "disabled":
        return "discovery_disabled"
    if error in {"missing_key", "no_kimi_key"}:
        return "missing_key"
    if error in {"http_401", "http_403"}:
        return "auth_rejected"
    if error == "http_429":
        return "rate_limited"
    if error.startswith("http_"):
        return "fetch_failed"
    if status == "unavailable":
        return "source_unavailable"
    if status == "ok":
        return ""
    return "other" if status == "skipped" else "fetch_failed"


def classify_discovery_run(
    run: dict[str, Any],
    *,
    now: datetime | None = None,
    prior_success_at: Any = None,
    stale_after_seconds: float | None = None,
) -> dict[str, Any]:
    """회사별 발견 1회를 ok/skipped/error/stale 로 판정한다. ok 만 '최신 확인됨' 이다."""
    now = now or datetime.now(timezone.utc)
    stale_after = _DISCOVERY_STALE_AFTER_SECONDS if stale_after_seconds is None else stale_after_seconds
    status = str(run.get("status") or "").strip().lower()
    error = str(run.get("error") or "").strip()
    if status == "ok":
        state = "ok"
    elif status == "skipped":
        state = "skipped"
    else:
        state = "error"
    observed = _parse_timestamp(run.get("observed_at"))
    raw_fetched = run.get("fetched_at")
    source_fetched = _parse_timestamp(raw_fetched)
    is_cache_source = bool(run.get("source_path")) or str(run.get("discovery_source") or "") == cli_model_autoreg.DISCOVERY_SOURCE
    reason_override = ""
    reference = source_fetched or observed
    if state == "ok" and is_cache_source and source_fetched is None:
        # 캐시 출처는 읽은 시각(observed_at)이 아니라 원본 fetched_at 만 신선도의 근거다.
        state = "stale"
        reason_override = "cache_timestamp_missing" if raw_fetched in (None, "") else "cache_timestamp_invalid"
    elif state == "ok" and reference is not None and (now - reference).total_seconds() > stale_after:
        state = "stale"
    reason_class = reason_override or _discovery_reason_class(status, error)
    succeeded = state in {"ok", "stale"}
    prior = _parse_timestamp(prior_success_at)
    last_success = observed if succeeded else prior
    return {
        "state": state,
        "latest_confirmed": state == "ok",
        "reason_class": reason_class,
        "manual_registration_required": reason_class == "oauth_runtime_only",
        "last_attempt_at": observed.isoformat() if observed else None,
        "last_success_at": last_success.isoformat() if last_success else None,
        "source_fetched_at": run.get("fetched_at"),
    }


def summarize_discovery_states(runs: Iterable[dict[str, Any]]) -> dict[str, Any]:
    buckets: dict[str, list[str]] = {"ok": [], "skipped": [], "error": [], "stale": []}
    runs = list(runs)
    for run in runs:
        buckets.setdefault(str(run.get("state") or "error"), []).append(str(run.get("provider") or "unknown"))
    complete = bool(runs) and all(not buckets[name] for name in ("skipped", "error", "stale"))
    return {
        **buckets,
        "discovery_complete": complete,
        "status": "ok" if complete else "partial" if buckets["ok"] else "error",
    }


async def discover_provider_model_rows(
    key_rows: Iterable[dict[str, Any]],
    *,
    enabled: bool | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if enabled is None:
        enabled = (os.getenv("LLM_MODEL_DISCOVERY_ENABLED", "1").strip().lower() not in {"0", "false", "no"})
    if not enabled:
        # 가상 공급자 'all' 대신 실제 공급자마다 기록해야 회사별 최신 확인 판정이 비활성화를 반영한다.
        observed_dt = datetime.now(timezone.utc)
        disabled_runs = []
        for provider in _DISCOVERY_PROVIDERS:
            run = {"provider": provider, "status": "skipped", "error": "disabled", "count": 0,
                   "observed_at": observed_dt.isoformat()}
            run.update(classify_discovery_run(run, now=observed_dt))
            disabled_runs.append(run)
        return [], disabled_runs

    key_state = _build_key_state(key_rows)
    fetchers = {
        "openai": _fetch_openai_models,
        "anthropic": _fetch_anthropic_models,
        "gemini": _fetch_gemini_models,
        "litellm": _fetch_litellm_models,
        "kimi": _fetch_kimi_models,
        "deepseek": _fetch_deepseek_models,
        "codex": _fetch_codex_cli_models,
        **{
            name: functools.partial(_fetch_openai_compatible_models, name)
            for name in _OPENAI_COMPAT_DISCOVERY_BASE_URLS
        },
    }
    all_rows: list[dict[str, Any]] = []
    run_results: list[dict[str, Any]] = []
    for provider, fetcher in fetchers.items():
        try:
            raw_rows, result = await fetcher()
            provider_state = key_state.get(provider, {})
            runtime_executable = bool(
                result.get(
                    "runtime_executable",
                    provider in _KEYLESS_PROVIDERS or int(provider_state.get("available_key_count", 0)) > 0,
                )
            )
            auto_discovery_supported = bool(
                result.get(
                    "auto_discovery_supported",
                    _provider_auto_discovery_supported(provider, provider_state),
                )
            )
            discovered = [
                _discovered_model_row(
                    provider=provider,
                    model_id=item["model_id"],
                    display_name=item.get("display_name") or item["model_id"],
                    key_state=key_state,
                    raw=item.get("raw") or {},
                    source=result.get("discovery_source") or f"{provider}_api",
                )
                for item in raw_rows
            ]
            all_rows.extend(discovered)
            run_results.append({
                "provider": provider,
                "status": result.get("status", "ok"),
                "count": len(discovered),
                "active_count": sum(1 for row in discovered if row.get("is_active")),
                "error": result.get("error", ""),
                "runtime_executable": runtime_executable,
                "auto_discovery_supported": auto_discovery_supported,
                "discovery_requirement": result.get("discovery_requirement") or _provider_discovery_requirement(provider),
                "template_model_count": len(_PROVIDER_TEMPLATES.get(provider, ())),
                "discovered_model_count": len(discovered),
                "model_source": result.get("model_source") or ("discovery" if discovered else "template"),
                **{
                    key: result[key]
                    for key in (
                        "discovery_source", "source_path", "fetched_at", "hidden_count",
                        "oauth_models_api", "docs_error", "canonical_ids", "alias_ids", "unverified_new_ids", "missing_canonical_ids", "absence_policy",
                    )
                    if key in result
                },
            })
        except Exception as exc:
            logger.warning("model_registry.discovery_failed: %s: %s", provider, str(exc)[:200])
            provider_state = key_state.get(provider, {})
            run_results.append({
                "provider": provider,
                "status": "failed",
                "count": 0,
                "active_count": 0,
                "error": _discovery_error_code(exc),
                "runtime_executable": provider in _KEYLESS_PROVIDERS or int(provider_state.get("available_key_count", 0)) > 0,
                "auto_discovery_supported": _provider_auto_discovery_supported(provider, provider_state),
                "discovery_requirement": _provider_discovery_requirement(provider),
                "template_model_count": len(_PROVIDER_TEMPLATES.get(provider, ())),
                "discovered_model_count": 0,
                "model_source": "template",
            })
    observed_dt = datetime.now(timezone.utc)
    observed_at = observed_dt.isoformat()
    for run in run_results:
        run["observed_at"] = observed_at
        run.update(classify_discovery_run(run, now=observed_dt))
    # 공식 목록에서 이번에 실제로 확인된 행에만 확인 시각을 찍는다. llm_models.last_seen_at 은 템플릿 재저장으로도 갱신되므로 최신 확인의 근거가 아니다.
    confirmed_providers = {run["provider"] for run in run_results if run.get("latest_confirmed")}
    for row in all_rows:
        if row["provider"] in confirmed_providers:
            row["metadata"]["catalog_confirmed_at"] = observed_at
    return all_rows, run_results


# ── 발견 모델 자동 활성화: 실행 가능성 판정 ─────────────────────────────────
# provider 목록으로 켜고 끄지 않는다. 키가 있거나 keyless 인 텍스트 모델이 실제 호출(max_tokens<=16) 1회를
# 통과하면 켠다. 판정 결과는 llm_models.metadata.activation_probe 에 남기고, 이미 판정한 모델은 다시 부르지 않는다.
_ACTIVATION_ENABLED_ENV = "LLM_MODEL_AUTO_ACTIVATE"
_ACTIVATION_MAX_PER_PROVIDER = max(0, int(os.getenv("LLM_MODEL_AUTO_ACTIVATE_MAX_PER_PROVIDER", "5")))
_ACTIVATION_RETRY_AFTER = timedelta(hours=24)
_ACTIVATION_TRANSIENT_RETRY_AFTER = timedelta(hours=1)
_ACTIVATION_PROBE_TIMEOUT_SECONDS = 20.0
_ACTIVATION_PROBE_MAX_TOKENS = 16
_ACTIVATION_PROBE_PROMPT = "Reply with exactly: OK"
_NON_DISCOVERY_SOURCES = frozenset({"", "template", "accepted_alias", "manual_seed"})
_PROBE_ERROR_CODE_RE = re.compile(r"[A-Za-z0-9_.-]{1,60}")


class ProbeOutcome(NamedTuple):
    ok: bool
    error: str = ""
    transient: bool = False


Prober = Callable[[Mapping[str, Any]], Awaitable[ProbeOutcome]]


def _probe_outcome_from_chat_response(response: httpx.Response) -> ProbeOutcome:
    """OpenAI 호환 chat/completions 응답을 판정한다. 오류 본문 원문은 남기지 않는다(키·URL 유출 방지)."""
    status = response.status_code
    if status == 200:
        try:
            choices = response.json().get("choices")
        except Exception:
            return ProbeOutcome(False, "schema_invalid", False)
        if isinstance(choices, list) and choices:
            return ProbeOutcome(True)
        return ProbeOutcome(False, "schema_invalid", False)
    code = ""
    try:
        body = response.json()
        err = body.get("error") if isinstance(body, dict) else None
        if isinstance(err, dict):
            code = str(err.get("code") or err.get("type") or "")
    except Exception:
        pass
    label = f"http_{status}" + (f":{code}" if _PROBE_ERROR_CODE_RE.fullmatch(code) else "")
    return ProbeOutcome(False, label, status in {408, 409, 425, 429} or status >= 500)


async def _probe_chat_completions(url: str, api_key: str, body: dict[str, Any]) -> ProbeOutcome:
    try:
        async with httpx.AsyncClient(timeout=_ACTIVATION_PROBE_TIMEOUT_SECONDS) as client:
            response = await client.post(url, json=body, headers={"Authorization": f"Bearer {api_key}"})
    except httpx.HTTPError as exc:
        return ProbeOutcome(False, f"fetch_error:{type(exc).__name__}", True)
    return _probe_outcome_from_chat_response(response)


async def _probe_anthropic_oauth(model: str) -> ProbeOutcome:
    """Claude 는 OAuth 토큰(R-AUTH)으로 messages 1회. anthropic_client 폴백 체인(타 모델로 성공 위장)을 타지 않는다."""
    import anthropic

    from app.core.auth_provider import create_anthropic_client, get_oauth_tokens_async

    tokens = await get_oauth_tokens_async()
    if not tokens:
        return ProbeOutcome(False, "no_oauth_token", False)
    client = create_anthropic_client(token=tokens[0]).with_options(
        timeout=_ACTIVATION_PROBE_TIMEOUT_SECONDS, max_retries=0
    )
    try:
        await client.messages.create(
            model=model,
            max_tokens=_ACTIVATION_PROBE_MAX_TOKENS,
            messages=[{"role": "user", "content": _ACTIVATION_PROBE_PROMPT}],
        )
    except anthropic.APIStatusError as exc:
        status = int(getattr(exc, "status_code", 0) or 0)
        return ProbeOutcome(False, f"http_{status}", status in {408, 409, 425, 429} or status >= 500)
    except anthropic.APIConnectionError as exc:
        return ProbeOutcome(False, f"fetch_error:{type(exc).__name__}", True)
    return ProbeOutcome(True)


def _is_google_route(row: Mapping[str, Any]) -> bool:
    """Google/Gemini 로 나가는 행인가. litellm 별칭은 litellm_params.model 로도 본다."""
    provider = str(row.get("provider") or "")
    if provider == "antigravity":
        return False  # AGY 는 Google API 가 아니라 CLI 경로다. model_id 가 gemini-* 여도 API 정책 게이트 대상이 아니다.
    if provider == "gemini":
        return True
    meta = _coerce_json_object(row.get("metadata"))
    raw = _coerce_json_object(meta.get("raw"))
    upstream = str(_coerce_json_object(raw.get("litellm_params")).get("model") or "").lower()
    model_id = str(row.get("model_id") or "").lower()
    return (
        model_id.startswith(("gemini", "google"))
        or upstream.startswith(("gemini", "google", "vertex_ai"))
        or "gemini" in upstream
    )


async def probe_model_execution(row: Mapping[str, Any]) -> ProbeOutcome:
    """기존 실행 경로(anthropic OAuth / openai 호환 직접 / LiteLLM 프록시)로 짧은 호출 1회."""
    provider = str(row.get("provider") or "")
    meta = _coerce_json_object(row.get("metadata"))
    backend = str(meta.get("execution_backend") or "")
    model = str(row.get("execution_model_id") or row.get("model_id") or "")
    body = {
        "model": model,
        "messages": [{"role": "user", "content": _ACTIVATION_PROBE_PROMPT}],
    }
    if provider == "anthropic":
        return await _probe_anthropic_oauth(model)
    if backend == "openai_compatible_direct":
        base_url = str(meta.get("execution_base_url") or _DIRECT_PROVIDER_BASE_URLS.get(provider) or "")
        api_key = await _get_first_provider_key(provider)
        if not api_key and provider == "openai":
            api_key = (os.getenv("OPENAI_API_KEY") or "").strip()
        if not base_url or not api_key:
            return ProbeOutcome(False, "missing_key", False)
        token_field = "max_completion_tokens" if provider == "openai" else "max_tokens"
        body[token_field] = _ACTIVATION_PROBE_MAX_TOKENS
        return await _probe_chat_completions(f"{base_url.rstrip('/')}/chat/completions", api_key, body)
    if backend == "litellm_proxy" or provider == "litellm":
        from app.core.auth_provider import get_litellm_config

        config = get_litellm_config()
        if not config.get("url") or not config.get("key"):
            return ProbeOutcome(False, "litellm_unconfigured", False)
        body["max_tokens"] = _ACTIVATION_PROBE_MAX_TOKENS
        return await _probe_chat_completions(f"{config['url'].rstrip('/')}/v1/chat/completions", config["key"], body)
    return ProbeOutcome(False, "no_execution_path", False)


def _row_meta(row: dict[str, Any]) -> dict[str, Any]:
    meta = _coerce_json_object(row.get("metadata"))
    row["metadata"] = meta
    return meta


def _mark_activated(row: dict[str, Any], *, verification_status: str = "verified", verified_at: datetime | None = None) -> None:
    meta = _row_meta(row)
    meta["requires_admin_review"] = False
    meta.pop("activation_review_reason", None)
    meta.pop("deactivation_source", None)
    row.update(
        is_active=True, is_selectable=True, is_executable=True, activation_source="db",
        verification_status=verification_status,
    )
    if verified_at is not None:
        row["last_verified_at"] = verified_at


def _mark_review_required(row: dict[str, Any], reason: str) -> None:
    meta = _row_meta(row)
    meta["requires_admin_review"] = True
    meta["activation_review_reason"] = reason
    meta["deactivation_source"] = "system"  # 시스템이 끈 행 — 관리자 비활성화와 구분해 키 복구·정책 변경 후 재판정한다.
    row.update(is_active=False, is_selectable=False, is_executable=False, verification_status="review_required")


def _mark_manual_disabled(row: dict[str, Any], reason: str) -> None:
    meta = _row_meta(row)
    meta["requires_admin_review"] = False
    meta["manual_disabled"] = True
    meta["activation_review_reason"] = reason
    meta["deactivation_source"] = "admin"
    row.update(
        is_active=False, is_selectable=False, is_executable=False, activation_source="manual",
        verification_status=row.get("verification_status") or "review_required",
    )


def _mark_manual_policy_blocked(row: dict[str, Any]) -> None:
    """관리자가 켠 행이라도 Google 정책이 꺼져 있으면 실행하지 않는다.

    activation_source='manual' 은 남겨 둔다. 정책이 다시 켜지면 sync 가 관리자 활성 상태로 되돌린다
    (deactivation_source='policy_manual' 이 그 표식이다 — 관리자 비활성화와 구분한다).
    """
    meta = _row_meta(row)
    meta["requires_admin_review"] = True
    meta["activation_review_reason"] = "google_routes_disabled"
    meta["deactivation_source"] = "policy_manual"
    row.update(
        is_active=False, is_selectable=False, is_executable=False, activation_source="manual",
        verification_status="review_required",
    )


def _is_manual_disabled(prior: Mapping[str, Any]) -> bool:
    return str(prior.get("activation_source") or "") == "manual" and not prior.get("is_active")


def _is_system_disabled(prior: Mapping[str, Any]) -> bool:
    """시스템(키 불가·정책·probe 실패·은퇴)이 끈 행. 관리자 비활성화는 여기에 해당하지 않는다."""
    if prior.get("is_active") or _is_manual_disabled(prior):
        return False
    return prior.get("retired_at") is not None or str(prior.get("deactivation_source") or "") == "system"


def _snapshot_rank(model_id: str) -> int:
    """날짜 스냅샷(-YYYY-MM-DD, -YYYYMMDD)은 뒤로 미뤄 호출 상한 안에서 대표 id 가 먼저 검증되게 한다."""
    return 1 if re.search(r"-\d{4}-?\d{2}-?\d{2}$", model_id) else 0


async def apply_activation_probes(
    model_rows: list[dict[str, Any]],
    prior_state: Mapping[tuple[str, str], Mapping[str, Any]],
    *,
    now: datetime | None = None,
    prober: Prober | None = None,
    verified_codex_ids: Iterable[str] = (),
    skip_keys: Iterable[tuple[str, str]] = (),
    max_per_provider: int | None = None,
    google_routes_enabled: bool | None = None,
    activation_enabled: bool = True,
) -> dict[str, Any]:
    """발견 행을 실행 가능성 판정으로 켠다. model_rows 를 제자리에서 고친다.

    - 판정 대상은 metadata.model_source == 'discovery' 인 행뿐이다(템플릿·병합 행은 기존 규칙).
    - 관리자가 끈 행(activation_source='manual', 또는 probe 성공 뒤 시스템 표식 없이 꺼진 행)은 되살리지 않는다.
    - 시스템이 끈 행(metadata.deactivation_source='system', 은퇴)은 키 복구·정책 해제 뒤 재-probe 해서 다시 켠다.
    - Google 정책 검사는 모든 활성화 분기보다 앞이다(provider=antigravity 는 Google API 경로가 아니라 예외).
    - activation_enabled=False 는 새 활성화·probe 호출만 끈다. 이미 켜진 행의 상태와 probe 이력은 보존한다.
    - 판정 완료 행은 재호출하지 않는다. 실패는 24시간(일시 오류는 1시간) 뒤 재시도한다.
    - provider 당 호출은 max_per_provider(기본 5)건. 넘는 행은 다음 실행으로 미룬다.
    """
    now = now or datetime.now(timezone.utc)
    cap = _ACTIVATION_MAX_PER_PROVIDER if max_per_provider is None else max(0, int(max_per_provider))
    prober = prober or probe_model_execution
    skip = set(skip_keys)
    codex_verified = set(verified_codex_ids)
    summary: dict[str, Any] = {
        "probed": {}, "activated": [], "failed": [], "deferred": [], "manual_kept": [], "carried_active": [],
    }
    queues: dict[str, list[tuple[dict[str, Any], bool]]] = {}
    google_enabled = google_routes_enabled

    for row in model_rows:
        meta = _row_meta(row)
        if meta.get("model_source") != "discovery":
            continue
        provider, model_id = str(row["provider"]), str(row["model_id"])
        key = (provider, model_id)
        label = f"{provider}:{model_id}"
        prior = prior_state.get(key)
        prior_probe = _coerce_json_object(prior.get("activation_probe")) if prior else {}
        if prior_probe:
            meta["activation_probe"] = prior_probe
        if key in skip:
            continue

        if prior and _is_manual_disabled(prior):
            _mark_manual_disabled(row, "manual_disabled")
            summary["manual_kept"].append(label)
            continue
        if _is_google_route(row):
            if google_enabled is None:
                google_enabled = await _google_commercial_routes_enabled()
            if not google_enabled:
                _mark_review_required(row, "google_routes_disabled")
                continue
        if provider == "codex":
            if model_id not in codex_verified:
                _mark_review_required(row, "awaiting_host_cli_probe")
            elif activation_enabled:
                _mark_activated(row, verified_at=now)
                summary["activated"].append(label)
            elif prior and prior.get("is_active") and prior.get("is_executable"):
                # 자동 활성화를 꺼도 이미 켜진 행은 그대로 둔다 — 새로 켜는 것만 막는다.
                _mark_activated(row, verification_status=str(prior.get("verification_status") or "verified"))
                row["activation_source"] = str(prior.get("activation_source") or "db")
                summary["carried_active"].append(label)
            else:
                _mark_review_required(row, "auto_activation_disabled")
            continue
        if not meta.get("activation_candidate"):
            _mark_review_required(row, "not_text_model" if meta.get("runtime_executable") else "no_runtime_key")
            continue
        if not activation_enabled:
            if prior and prior.get("is_active") and prior.get("is_executable"):
                _mark_activated(row, verification_status=str(prior.get("verification_status") or "verified"))
                row["activation_source"] = str(prior.get("activation_source") or "db")
                summary["carried_active"].append(label)
            else:
                _mark_review_required(row, "auto_activation_disabled")
            continue
        if (
            prior and prior.get("is_active") and prior.get("is_executable") and not prior_probe
            and str(prior.get("discovery_source") or "") not in _NON_DISCOVERY_SOURCES
        ):
            _mark_activated(row, verification_status=str(prior.get("verification_status") or "verified"))
            summary["carried_active"].append(label)
            continue
        status = str(prior_probe.get("status") or "")
        if status == "ok":
            if prior and _is_system_disabled(prior):
                pass  # 시스템이 끈 행: 과거 성공 이력만으로 켜지 않고 아래에서 다시 판정한다.
            elif prior and not prior.get("is_active"):
                _mark_manual_disabled(row, "manual_disabled")
                summary["manual_kept"].append(label)
                continue
            else:
                _mark_activated(row)
                continue
        if status == "failed":
            wait = _ACTIVATION_TRANSIENT_RETRY_AFTER if prior_probe.get("transient") else _ACTIVATION_RETRY_AFTER
            failed_at = _parse_timestamp(prior_probe.get("at"))
            if failed_at is not None and now - failed_at < wait:
                _mark_review_required(row, str(prior_probe.get("error") or "probe_failed"))
                continue
        if not meta.get("execution_backend") and provider not in {"anthropic", "litellm"}:
            _mark_review_required(row, "no_execution_path")
            continue
        _mark_review_required(row, "probe_pending")
        queues.setdefault(provider, []).append((row, status == "failed"))

    async def run_provider(provider: str, items: list[tuple[dict[str, Any], bool]]) -> None:
        items.sort(key=lambda it: (it[1], _snapshot_rank(str(it[0]["model_id"])), str(it[0]["model_id"])))
        calls = 0
        provider_down = False
        for row, _retry in items:
            label = f"{provider}:{row['model_id']}"
            if calls >= cap or provider_down:
                _row_meta(row)["activation_review_reason"] = "probe_deferred"
                summary["deferred"].append(label)
                continue
            calls += 1
            try:
                outcome = await asyncio.wait_for(prober(row), timeout=_ACTIVATION_PROBE_TIMEOUT_SECONDS + 5)
            except asyncio.TimeoutError:
                outcome = ProbeOutcome(False, "probe_timeout", True)
            except Exception as exc:
                outcome = ProbeOutcome(False, f"probe_error:{type(exc).__name__}", True)
            meta = _row_meta(row)
            meta["activation_probe"] = {
                "status": "ok" if outcome.ok else "failed",
                "at": now.isoformat(),
                "error": outcome.error,
                "transient": bool(outcome.transient),
                "max_tokens": _ACTIVATION_PROBE_MAX_TOKENS,
            }
            if outcome.ok:
                _mark_activated(row, verified_at=now)
                summary["activated"].append(label)
            else:
                _mark_review_required(row, outcome.error or "probe_failed")
                summary["failed"].append(f"{label}={outcome.error}")
                provider_down = outcome.transient
        summary["probed"][provider] = calls

    if cap > 0:
        await asyncio.gather(*(run_provider(provider, items) for provider, items in queues.items()))
    else:
        for items in queues.values():
            for row, _retry in items:
                summary["deferred"].append(f"{row['provider']}:{row['model_id']}")
    return summary


def _merge_model_rows(template_rows: list[dict[str, Any]], discovered_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[tuple[str, str], dict[str, Any]] = {
        (row["provider"], row["model_id"]): dict(row)
        for row in template_rows
        if not _is_retired_catalog_row(row)
    }
    for row in discovered_rows:
        if _is_retired_catalog_row(row):
            continue
        key = (row["provider"], row["model_id"])
        if key not in merged:
            merged[key] = dict(row)
            continue
        existing = merged[key]
        metadata = _coerce_json_object(existing.get("metadata"))
        discovered_metadata = _coerce_json_object(row.get("metadata"))
        metadata["discovered"] = True
        metadata["model_source"] = "template+discovery"
        metadata["discovery_source"] = discovered_metadata.get("discovery_source")
        metadata["runtime_executable"] = discovered_metadata.get("runtime_executable", metadata.get("runtime_executable"))
        metadata["auto_discovery_supported"] = discovered_metadata.get("auto_discovery_supported", metadata.get("auto_discovery_supported"))
        metadata["discovery_requirement"] = discovered_metadata.get("discovery_requirement", metadata.get("discovery_requirement"))
        metadata["discovery_mode"] = discovered_metadata.get("discovery_mode", metadata.get("discovery_mode"))
        metadata.update({k: v for k, v in release_date_fields(discovered_metadata).items() if v is not None})
        metadata["raw"] = discovered_metadata.get("raw", {})
        if discovered_metadata.get("catalog_confirmed_at"):
            metadata["catalog_confirmed_at"] = discovered_metadata["catalog_confirmed_at"]
        existing["metadata"] = metadata
        existing["last_verified_at"] = existing.get("last_verified_at") or row.get("last_verified_at")
        existing["capabilities"] = row.get("capabilities") or existing.get("capabilities") or {}
        existing["pricing"] = existing.get("pricing") or row.get("pricing") or {}
    return sorted(merged.values(), key=lambda item: (item["provider"], item["family"], item["model_id"]))


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
        auto_discovery_supported = _provider_auto_discovery_supported(provider, state)
        discovery_requirement = _provider_discovery_requirement(provider)
        discovery_mode = _provider_discovery_mode(
            provider,
            runtime_executable=has_runtime_models,
            auto_discovery_supported=auto_discovery_supported,
        )
        activation_source = "fallback" if keyless or active_key_count == 0 else "db"
        linked_key_name = None if keyless else _pick_linked_key(keys)

        templates = _PROVIDER_TEMPLATES.get(provider, ())
        for template in templates:
            metadata = {
                "template_provider": provider,
                "model_source": "template",
                "raw_provider_aliases": sorted(state.get("raw_providers", set())),
                "active_key_count": active_key_count,
                "available_key_count": available_key_count,
                "rate_limited_key_count": rate_limited_key_count,
                "verified_key_count": verified_key_count,
                "last_used_at": _iso(state.get("last_used_at")),
                "last_verified_at": _iso(state.get("last_verified_at")),
                "requires_admin_review": False,
                "runtime_executable": has_runtime_models,
                "auto_discovery_supported": auto_discovery_supported,
                "discovery_requirement": discovery_requirement,
                "discovery_mode": discovery_mode,
                "execution_backend": template.execution_backend,
                "execution_model_id": template.execution_model_id,
                "execution_base_url": template.execution_base_url,
            }
            metadata.update(_reviewed_release_fields(provider, template.model_id))
            metadata.update(_compatibility_alias_metadata(template.model_id))
            metadata.update(_accepted_alias_metadata(provider, template.model_id))
            metadata.update(_commercial_metadata(provider, template.model_id))
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
                    "execution_backend": template.execution_backend,
                    "execution_model_id": template.execution_model_id,
                    "execution_base_url": template.execution_base_url,
                    "discovery_source": "template",
                    "verification_status": "verified" if has_runtime_models else "unknown",
                    "last_verified_at": state.get("last_verified_at"),
                    "capabilities": _model_capabilities(provider, template.model_id),
                    "pricing": _pricing_for(template.model_id),
                    "is_selectable": has_runtime_models,
                    "is_executable": has_runtime_models,
                }
            )

            for alias_id in _MODEL_ACCEPTED_ALIASES.get(template.model_id, ()):
                if alias_id == template.model_id:
                    continue
                alias_template = _build_template(provider, alias_id)
                alias_metadata = dict(metadata)
                alias_metadata["alias_of"] = template.model_id
                alias_metadata["model_source"] = "accepted_alias"
                model_rows.append(
                    {
                        "provider": provider,
                        "model_id": alias_id,
                        "display_name": alias_template.display_name or _display_name_for_provider(provider, alias_id),
                        "family": alias_template.family or template.family,
                        "category": alias_template.category or template.category,
                        "supports_tools": template.supports_tools,
                        "supports_thinking": template.supports_thinking,
                        "supports_vision": template.supports_vision,
                        "supports_coding": template.supports_coding,
                        "input_cost": template.input_cost,
                        "output_cost": template.output_cost,
                        "is_active": False,
                        "activation_source": activation_source,
                        "linked_key_name": linked_key_name,
                        "metadata": alias_metadata,
                        "execution_backend": template.execution_backend,
                        "execution_model_id": template.execution_model_id or template.model_id,
                        "execution_base_url": template.execution_base_url,
                        "discovery_source": "accepted_alias",
                        "verification_status": "verified" if has_runtime_models else "unknown",
                        "last_verified_at": state.get("last_verified_at"),
                        "capabilities": _model_capabilities(provider, alias_id),
                        "pricing": _pricing_for(alias_id),
                        "is_selectable": False,
                        "is_executable": False,
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
                "discovered_model_count": 0,
                "template_active_model_count": sum(
                    1
                    for row in model_rows
                    if row["provider"] == provider and row["is_active"] and row.get("discovery_source") == "template"
                ),
                "discovery_active_model_count": 0,
                "active_model_source": "template" if has_runtime_models and templates else "none",
                "runtime_executable": has_runtime_models,
                "auto_discovery_supported": auto_discovery_supported,
                "discovery_requirement": discovery_requirement,
                "discovery_mode": discovery_mode,
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
                "discovered_model_count": 0,
                "template_active_model_count": 0,
                "discovery_active_model_count": 0,
                "active_model_source": "none",
                "runtime_executable": int(state.get("available_key_count", 0)) > 0,
                "auto_discovery_supported": _provider_auto_discovery_supported(provider, state),
                "discovery_requirement": _provider_discovery_requirement(provider),
                "discovery_mode": "manual_review",
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
                   linked_key_name, metadata, updated_at,
                   execution_model_id, discovery_source, first_seen_at, last_seen_at,
                   retired_at, verification_status, last_verified_at, capabilities,
                   pricing, is_selectable, is_executable
            FROM llm_models
            ORDER BY
                CASE WHEN metadata->'raw'->>'created' ~ '^[0-9]+$'
                     THEN (metadata->'raw'->>'created')::bigint
                     ELSE NULL END DESC NULLS LAST,
                provider, family, model_id
            """
        )
    except asyncpg.UndefinedTableError:
        return None
    return [dict(row) for row in rows]


# Official release announcements checked 2026-10-30. Only exact IDs are
# covered; unknown/new models remain null until an official source is reviewed.
_VERIFIED_RELEASES = {
    ("openai", "gpt-4.1"): ("2025-04-14", "https://openai.com/index/gpt-4-1/"),
    ("openai", "gpt-4.1-mini"): ("2025-04-14", "https://openai.com/index/gpt-4-1/"),
    ("openai", "gpt-4.1-nano"): ("2025-04-14", "https://openai.com/index/gpt-4-1/"),
    ("anthropic", "claude-sonnet-4-5"): ("2025-09-29", "https://www.anthropic.com/news/claude-sonnet-4-5"),
    ("anthropic", "claude-sonnet-4-5-20250929"): ("2025-09-29", "https://www.anthropic.com/news/claude-sonnet-4-5"),
}


def _reviewed_release_fields(provider: str, model_id: str) -> dict[str, str]:
    release = _VERIFIED_RELEASES.get((provider, model_id))
    if release is None:
        return {}
    return {"release_date": release[0], "release_date_source": release[1],
            "release_date_verified_at": "2026-10-30T11:08:43Z"}


def release_date_fields(metadata: Any) -> dict[str, Any]:
    """Only explicit, sourced release metadata; never infer from created/name/first_seen."""
    meta = _coerce_json_object(metadata)
    value = meta.get("release_date")
    source = meta.get("release_date_source")
    verified = meta.get("release_date_verified_at")
    try:
        if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
            raise ValueError("invalid date")
        if not isinstance(source, str) or not source.startswith("https://"):
            raise ValueError("missing public source")
        if not isinstance(verified, str):
            raise ValueError("missing verification time")
        datetime.fromisoformat(verified.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return dict.fromkeys(("release_date", "release_date_source", "release_date_verified_at"))
    return {"release_date": value, "release_date_source": source, "release_date_verified_at": verified}


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
        normalized_row.update(release_date_fields(normalized_row["metadata"]))
        normalized_row["capabilities"] = _coerce_json_object(normalized_row.get("capabilities"))
        normalized_row["pricing"] = _coerce_json_object(normalized_row.get("pricing"))
        filtered.append(normalized_row)
    return _cache_set(cache_key, filtered)


async def ensure_runtime_models_fresh(*, provider: str | None = None, active_only: bool = False) -> dict[str, Any]:
    """active_only 조회 직전 stale runtime registry를 짧게 자동 복구한다."""
    if not active_only:
        return {"checked": False, "triggered": False, "reason": "inactive_request"}

    normalized_provider = normalize_provider(provider or "")
    scope = normalized_provider or "*"
    now_ts = time.time()
    last_attempt = _auto_refresh_attempts.get(scope, 0.0)
    if now_ts - last_attempt < _AUTO_REFRESH_MIN_INTERVAL_SECONDS:
        return {"checked": False, "triggered": False, "reason": "cooldown", "provider": normalized_provider or None}

    # 만료된 rate limit 즉시 정리 → 복구 re-sync
    try:
        rl_result = await clear_expired_rate_limits()
        if rl_result.get("cleared", 0) > 0 and rl_result.get("synced"):
            _auto_refresh_attempts[scope] = now_ts
            return {
                "checked": True,
                "triggered": True,
                "reason": "rate_limit_auto_cleared",
                "provider": normalized_provider or None,
                "cleared_keys": rl_result.get("keys", []),
            }
    except Exception:
        logger.warning("ensure_runtime_models_fresh.rate_limit_clear_failed")

    pool = get_pool()
    async with pool.acquire() as conn:
        key_rows = await _fetch_key_rows(conn)
        registry_rows = await _fetch_registry_rows(conn) or []

    key_state = _build_key_state(key_rows)
    providers = [normalized_provider] if normalized_provider else sorted(key_state.keys())
    stale_providers: list[str] = []
    for provider_name in providers:
        if not provider_name:
            continue
        templates = _PROVIDER_TEMPLATES.get(provider_name)
        if not templates:
            continue
        if int(key_state.get(provider_name, {}).get("available_key_count", 0)) <= 0:
            continue
        provider_rows = [row for row in registry_rows if row.get("provider") == provider_name]
        inactive_count = sum(1 for row in provider_rows if not row.get("is_active"))
        if not provider_rows or inactive_count == 0:
            continue
        total_count = len(provider_rows)
        if inactive_count > 0 and inactive_count >= total_count * 0.5:
            stale_providers.append(provider_name)
            continue
        template_ids = {t.model_id for t in templates}
        template_rows = [row for row in provider_rows if row.get("model_id") in template_ids]
        if template_rows and any(bool(row.get("is_active")) for row in template_rows):
            continue
        stale_providers.append(provider_name)

    if not stale_providers:
        return {"checked": True, "triggered": False, "reason": "healthy", "provider": normalized_provider or None}

    _auto_refresh_attempts[scope] = now_ts
    invalidate_registry_cache()
    result = await sync_model_registry(
        triggered_by="llm_models_auto_refresh",
        reason=f"active_only_stale:{','.join(stale_providers)}",
    )
    return {
        "checked": True,
        "triggered": bool(result.get("ok")),
        "reason": "stale_runtime_catalog",
        "provider": normalized_provider or None,
        "stale_providers": stale_providers,
        "sync_ok": bool(result.get("ok")),
    }


async def list_provider_summaries() -> list[dict[str, Any]]:
    cached = _cache_get("provider_summaries")
    if cached is not None:
        return cached

    pool = get_pool()
    async with pool.acquire() as conn:
        key_rows = await _fetch_key_rows(conn)
        registry_rows = await _fetch_registry_rows(conn)
    _, provider_rows = build_registry_snapshots(key_rows)
    if not registry_rows:
        return _cache_set("provider_summaries", provider_rows)

    aggregates: dict[str, dict[str, Any]] = {}
    for row in registry_rows:
        provider = str(row.get("provider") or "").strip()
        if not provider:
            continue
        metadata = _coerce_json_object(row.get("metadata"))
        aggregate = aggregates.setdefault(
            provider,
            {
                "total_model_count": 0,
                "active_model_count": 0,
                "selectable_model_count": 0,
                "executable_model_count": 0,
                "discovered_model_count": 0,
                "template_active_model_count": 0,
                "discovery_active_model_count": 0,
                "review_required_model_count": 0,
                "last_seen_at": None,
            },
        )
        aggregate["total_model_count"] += 1
        if row.get("is_active"):
            aggregate["active_model_count"] += 1
        if row.get("is_selectable"):
            aggregate["selectable_model_count"] += 1
        if row.get("is_executable"):
            aggregate["executable_model_count"] += 1
        if metadata.get("discovered") or row.get("discovery_source") not in {None, "", "template"}:
            aggregate["discovered_model_count"] += 1
        if row.get("is_active") and row.get("discovery_source") == "template":
            aggregate["template_active_model_count"] += 1
        if row.get("is_active") and row.get("discovery_source") not in {None, "", "template"}:
            aggregate["discovery_active_model_count"] += 1
        if row.get("verification_status") == "review_required":
            aggregate["review_required_model_count"] += 1
        row_last_seen = row.get("last_seen_at") or row.get("updated_at")
        if row_last_seen and (aggregate["last_seen_at"] is None or row_last_seen > aggregate["last_seen_at"]):
            aggregate["last_seen_at"] = row_last_seen

    provider_map = {row["provider"]: dict(row) for row in provider_rows}
    for provider, aggregate in aggregates.items():
        summary = provider_map.setdefault(
            provider,
            {
                "provider": provider,
                "display_name": _PROVIDER_META.get(provider, {}).get("display_name", _display_name_for(provider)),
                "template_available": False,
                "requires_admin_review": True,
                "active_key_count": 0,
                "available_key_count": 0,
                "rate_limited_key_count": 0,
                "verified_key_count": 0,
                "template_model_count": 0,
                "discovered_model_count": 0,
                "template_active_model_count": 0,
                "discovery_active_model_count": 0,
                "active_model_source": "none",
                "runtime_executable": False,
                "auto_discovery_supported": False,
                "discovery_requirement": _provider_discovery_requirement(provider),
                "discovery_mode": "manual_review",
                "last_used_at": None,
                "last_verified_at": None,
                "linked_key_name": None,
                "status": "review_required",
            },
        )
        summary.update(
            {
                "total_model_count": aggregate["total_model_count"],
                "active_model_count": aggregate["active_model_count"],
                "selectable_model_count": aggregate["selectable_model_count"],
                "executable_model_count": aggregate["executable_model_count"],
                "discovered_model_count": aggregate["discovered_model_count"],
                "template_active_model_count": aggregate["template_active_model_count"],
                "discovery_active_model_count": aggregate["discovery_active_model_count"],
                "active_model_source": (
                    "template+discovery"
                    if aggregate["template_active_model_count"] and aggregate["discovery_active_model_count"]
                    else "discovery"
                    if aggregate["discovery_active_model_count"]
                    else "template"
                    if aggregate["template_active_model_count"]
                    else "none"
                ),
                "review_required_model_count": aggregate["review_required_model_count"],
                "last_seen_at": aggregate["last_seen_at"].isoformat() if aggregate["last_seen_at"] else None,
            }
        )
        if aggregate["active_model_count"] > 0 and summary.get("status") in {"inactive", "review_required"}:
            summary["status"] = "active"
        if aggregate["review_required_model_count"] > 0:
            summary["requires_admin_review"] = True

    provider_rows = sorted(provider_map.values(), key=lambda row: row["provider"])
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
    for alias, canonical in _DEEPSEEK_COMPATIBILITY_ALIASES.items():
        if canonical in executable:
            executable.add(alias)
        if alias in executable:
            executable.add(canonical)
    return _cache_set("executable_ids", executable)


def _normalize_model_id(model_id: str) -> str:
    """접두사(codex:, litellm:, claude:) 제거 후 비교용 정규화."""
    normalized = str(model_id or "").strip()
    for prefix in ("codex:", "litellm:", "claude:"):
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix):]
            break
    return normalized


async def filter_executable_models(model_ids: Sequence[str]) -> list[str]:
    executable_ids = await get_executable_model_ids()
    if executable_ids is None or len(executable_ids) == 0:
        return list(model_ids)

    normalized_executable_ids: list[str] = []
    for executable_id in executable_ids:
        normalized_executable_id = _normalize_model_id(executable_id)
        if normalized_executable_id:
            normalized_executable_ids.append(normalized_executable_id)
    if len(normalized_executable_ids) == 0:
        return list(model_ids)

    filtered: list[str] = []
    for model_id in model_ids:
        normalized_model_id = _normalize_model_id(model_id)
        if not normalized_model_id:
            continue
        canonical_model_id = _canonical_model_id(normalized_model_id)
        if any(
            normalized_model_id == executable_id
            or canonical_model_id == _canonical_model_id(executable_id)
            or normalized_model_id.startswith(executable_id)
            or executable_id.startswith(normalized_model_id)
            for executable_id in normalized_executable_ids
        ):
            filtered.append(model_id)
    return filtered


# (tier, fallback_group) — 같은 계열의 기존 행(opus=S/premium, sonnet=A/standard)과 동일한 값만 적는다.
_MODEL_TIER_CATALOG: dict[tuple[str, str], tuple[str, str]] = {
    ("anthropic", "claude-opus-5-5"): ("S", "premium"),
    ("anthropic", "claude-opus-5"): ("S", "premium"),
    ("anthropic", "claude-sonnet-5"): ("A", "standard"),
}


def _catalog_tier(provider: str, model_id: str) -> tuple[str | None, str | None]:
    return _MODEL_TIER_CATALOG.get((provider, model_id), (None, None))


async def _fetch_last_discovery_success(conn: asyncpg.Connection) -> dict[str, str]:
    try:
        rows = await conn.fetch(
            """
            SELECT provider, MAX(created_at) AS last_success_at
            FROM llm_model_discovery_runs
            WHERE status = 'ok'
            GROUP BY provider
            """
        )
    except asyncpg.UndefinedTableError:
        return {}
    return {
        str(row["provider"]): row["last_success_at"].isoformat()
        for row in rows
        if row["last_success_at"] is not None
    }


def classify_stored_discovery_run(
    *,
    status: Any,
    error: Any,
    details: Any,
    created_at: Any,
    prior_success_at: Any = None,
) -> dict[str, Any]:
    """llm_model_discovery_runs 한 행을 읽는 시점 기준으로 다시 판정한다. 목록/요약 API 가 같은 규칙을 쓰게 하는 단일 경로."""
    details = _coerce_json_object(details)
    run = {
        "status": status,
        "error": error,
        "fetched_at": details.get("fetched_at"),
        "source_path": details.get("source_path"),
        "discovery_source": details.get("discovery_source"),
        "observed_at": created_at,
    }
    return classify_discovery_run(run, prior_success_at=prior_success_at or details.get("last_success_at"))


async def list_provider_discovery_freshness() -> dict[str, dict[str, Any]]:
    """회사별 마지막 시도/마지막 성공/현재 판정. 오래된 ok 는 읽는 시점 기준으로 stale 이 된다."""
    pool = get_pool()
    async with pool.acquire() as conn:
        try:
            latest = await conn.fetch(
                """
                SELECT DISTINCT ON (provider) provider, status, error, details, created_at
                FROM llm_model_discovery_runs
                ORDER BY provider, created_at DESC, id DESC
                """
            )
            prior_success = await _fetch_last_discovery_success(conn)
        except asyncpg.UndefinedTableError:
            return {}
    # 과거에 가상 공급자 'all' 로 남은 전역 비활성화 기록 — 그보다 오래된 회사별 기록은 비활성 이후 확인된 것이 아니다.
    global_run = next((row for row in latest if str(row["provider"]) == _GLOBAL_DISCOVERY_PROVIDER), None)
    result: dict[str, dict[str, Any]] = {}
    for row in latest:
        provider = str(row["provider"])
        if provider == _GLOBAL_DISCOVERY_PROVIDER:
            continue
        source = row
        if (
            global_run is not None
            and global_run["created_at"] is not None
            and row["created_at"] is not None
            and global_run["created_at"] > row["created_at"]
        ):
            source = global_run
        result[provider] = classify_stored_discovery_run(
            status=source["status"],
            error=source["error"],
            details=source["details"],
            created_at=source["created_at"],
            prior_success_at=prior_success.get(provider),
        )
    return result


_FRESHNESS_WINDOW = timedelta(days=7)
_SKIPPED_ALERT_AFTER = timedelta(hours=24)


def summarize_model_freshness(
    *,
    model_rows: Iterable[Mapping[str, Any]],
    freshness: Mapping[str, Mapping[str, Any]],
    keyed_providers: Iterable[str],
    now: datetime | None = None,
) -> dict[str, Any]:
    """회사별 최신화 현황. 마지막 성공 수집, 최근 7일 신규·자동활성·검토대기, 수집기 없는 키, 24시간 넘은 skipped/failed."""
    now = now or datetime.now(timezone.utc)
    since = now - _FRESHNESS_WINDOW
    collectors = set(_DISCOVERY_PROVIDERS)
    providers: dict[str, dict[str, Any]] = {}

    def bucket(provider: str) -> dict[str, Any]:
        return providers.setdefault(
            provider,
            {
                "has_collector": provider in collectors,
                "state": None, "reason_class": None, "last_attempt_at": None, "last_success_at": None,
                "new_models_7d": 0, "auto_activated_7d": 0, "review_pending_7d": 0, "review_pending_total": 0,
            },
        )

    for provider in collectors:
        bucket(provider)
    for provider, item in freshness.items():
        entry = bucket(provider)
        entry.update(
            state=item.get("state"), reason_class=item.get("reason_class") or None,
            last_attempt_at=item.get("last_attempt_at"), last_success_at=item.get("last_success_at"),
        )
    for row in model_rows:
        if row.get("retired_at") is not None:
            continue
        provider = str(row.get("provider") or "")
        if not provider:
            continue
        entry = bucket(provider)
        probe = _coerce_json_object(row.get("activation_probe"))
        probed_at = _parse_timestamp(probe.get("at"))
        if probe.get("status") == "ok" and probed_at is not None and probed_at >= since:
            entry["auto_activated_7d"] += 1
        if str(row.get("discovery_source") or "") in _NON_DISCOVERY_SOURCES:
            continue
        first_seen = _parse_timestamp(row.get("first_seen_at"))
        is_new = first_seen is not None and first_seen >= since
        review = str(row.get("verification_status") or "") == "review_required"
        if is_new:
            entry["new_models_7d"] += 1
        if review:
            entry["review_pending_total"] += 1
            if is_new:
                entry["review_pending_7d"] += 1

    stuck: list[dict[str, Any]] = []
    for provider, item in sorted(freshness.items()):
        if item.get("state") not in {"skipped", "error"}:
            continue
        last_success = _parse_timestamp(item.get("last_success_at"))
        if last_success is not None and now - last_success < _SKIPPED_ALERT_AFTER:
            continue
        stuck.append({
            "provider": provider,
            "state": item.get("state"),
            "reason_class": item.get("reason_class") or None,
            "last_attempt_at": item.get("last_attempt_at"),
            "last_success_at": item.get("last_success_at"),
            "hours_since_success": round((now - last_success).total_seconds() / 3600, 1) if last_success else None,
        })
    keyed = {normalize_provider(p) for p in keyed_providers if p}
    return {
        "window_days": _FRESHNESS_WINDOW.days,
        "generated_at": now.isoformat(),
        "providers": dict(sorted(providers.items())),
        "keyed_providers_without_collector": sorted(p for p in keyed if p and p not in collectors),
        "skipped_or_failed_24h": stuck,
        "skipped_or_failed_24h_providers": [item["provider"] for item in stuck],
    }


async def _build_model_freshness_report() -> dict[str, Any]:
    pool = get_pool()
    async with pool.acquire() as conn:
        key_rows = await _fetch_key_rows(conn)
        try:
            model_rows = await conn.fetch(
                """
                SELECT provider, first_seen_at, verification_status, discovery_source, retired_at,
                       metadata->'activation_probe' AS activation_probe
                FROM llm_models
                WHERE retired_at IS NULL
                """
            )
        except asyncpg.UndefinedTableError:
            model_rows = []
    freshness = await list_provider_discovery_freshness()
    key_state = _build_key_state(key_rows)
    keyed = [provider for provider, state in key_state.items() if int(state.get("active_key_count", 0)) > 0]
    return summarize_model_freshness(
        model_rows=[dict(row) for row in model_rows or []], freshness=freshness, keyed_providers=keyed,
    )


async def get_model_freshness_report() -> dict[str, Any]:
    cached = _cache_get("model_freshness_report")
    if cached is not None:
        return cached
    return _cache_set("model_freshness_report", await _build_model_freshness_report())


# health-check 는 DB 를 치지 않고 이 스냅샷만 읽는다(상태 점검이 서버를 멈춘 사고 대응). 채우는 곳은 sync_model_registry 끝이다.
_NOT_READY_FRESHNESS: dict[str, Any] = {"status": "not_ready"}
_freshness_snapshot: dict[str, Any] | None = None


def get_cached_model_freshness() -> dict[str, Any]:
    """캐시된 회사별 신선도. DB·네트워크 접근 없음. 아직 채워지지 않았으면 {"status": "not_ready"}."""
    return _freshness_snapshot if _freshness_snapshot is not None else dict(_NOT_READY_FRESHNESS)


async def refresh_model_freshness_cache() -> None:
    """모델 발견 주기 작업 끝에서 스냅샷을 채운다. 실패해도 sync 를 깨지 않는다."""
    global _freshness_snapshot
    try:
        report = await _build_model_freshness_report()
    except Exception as exc:
        logger.warning("model_registry.freshness_cache_refresh_failed: %s", type(exc).__name__)
        return
    _freshness_snapshot = report
    _cache_set("model_freshness_report", report)


async def _fetch_activation_state(conn: asyncpg.Connection) -> dict[tuple[str, str], dict[str, Any]]:
    """자동 활성화 판정이 참고하는 기존 행 상태(수동 비활성화·이전 판정 기록)."""
    rows = await conn.fetch(
        """
        SELECT provider, model_id, is_active, is_executable, activation_source, verification_status,
               discovery_source, retired_at, metadata->'activation_probe' AS activation_probe,
               metadata->>'deactivation_source' AS deactivation_source
        FROM llm_models
        """
    )
    return {(str(r["provider"]), str(r["model_id"])): dict(r) for r in rows or []}


async def sync_model_registry(*, triggered_by: str = "system", reason: str = "") -> dict[str, Any]:
    sync_token = uuid.uuid4().hex
    pool = get_pool()
    async with pool.acquire() as conn:
        key_rows = await _fetch_key_rows(conn)
        manual_rows = await conn.fetch(
            "SELECT provider, model_id FROM llm_models WHERE activation_source = 'manual' "
            "AND (is_active = TRUE OR metadata->>'deactivation_source' = 'policy_manual')"
        )
        # sync 이전에 이미 있던 행 — 이번 sync 에서 "새로 발견" 된 모델을 가려내는 기준.
        existing_keys = {
            (r["provider"], r["model_id"])
            for r in await conn.fetch("SELECT provider, model_id FROM llm_models")
        }
        verified_codex_ids = await cli_model_autoreg.fetch_verified_codex_ids(conn)
        prior_success = await _fetch_last_discovery_success(conn)
        activation_state = await _fetch_activation_state(conn)
    manual_active_keys = {(r["provider"], r["model_id"]) for r in manual_rows}
    newly_activated_keys: set[tuple[str, str]] = set()
    template_rows, provider_rows = build_registry_snapshots(key_rows)
    key_state = _build_key_state(key_rows)
    normalized_providers = _collect_provider_normalizations(key_state)
    discovered_rows, discovery_runs = await discover_provider_model_rows(key_rows)
    for run in discovery_runs:
        # 실패·skip 회차는 마지막 성공 시각을 전진시키지 않는다 — 이전 성공 기록을 그대로 둔다.
        if not run.get("last_success_at"):
            run["last_success_at"] = prior_success.get(str(run.get("provider") or ""))
    discovery_summary = summarize_discovery_states(discovery_runs)
    provider_status = [
        {
            key: run.get(key)
            for key in (
                "provider", "state", "latest_confirmed", "reason_class", "error", "manual_registration_required",
                "last_attempt_at", "last_success_at", "source_fetched_at", "count", "model_source",
            )
        }
        for run in discovery_runs
    ]
    model_rows = _merge_model_rows(template_rows, discovered_rows)
    activation: dict[str, Any] = {}
    # 플래그는 새 활성화·probe 호출만 끈다. 발견 행은 항상 이 단계를 거쳐야 기존 활성 상태·probe 이력이 UPSERT 에서 보존된다.
    activation = await apply_activation_probes(
        model_rows,
        activation_state,
        verified_codex_ids=verified_codex_ids,
        skip_keys=manual_active_keys,
        activation_enabled=os.getenv(_ACTIVATION_ENABLED_ENV, "1").strip().lower() not in {"0", "false", "no"},
    )
    # 호출 검증을 이번 회차에 처음 통과한 모델은 이전 sync 에 행이 있었어도 실행 후보로 처음 올린다.
    newly_activated_keys = {
        tuple(label.split(":", 1))
        for label in activation.get("activated", [])
        if ":" in label
    }
    for run in discovery_runs:
        run["active_count"] = sum(
            1
            for row in model_rows
            if row["provider"] == run.get("provider")
            and row.get("is_active")
            and str(row.get("discovery_source") or "") not in _NON_DISCOVERY_SOURCES
        )
    # 관리자 활성 복원도 Google 정책 검사를 먼저 거친다 — 수동 활성 Gemini 별칭이 정책을 우회하지 않게 한다.
    manual_google_enabled: bool | None = None
    for row in model_rows:
        if (row["provider"], row["model_id"]) not in manual_active_keys:
            continue
        if _is_google_route(row):
            if manual_google_enabled is None:
                manual_google_enabled = await _google_commercial_routes_enabled()
            if not manual_google_enabled:
                _mark_manual_policy_blocked(row)
                continue
        meta = _row_meta(row)
        if meta.get("deactivation_source") == "policy_manual":
            meta["deactivation_source"] = None  # UPSERT 가 metadata 를 병합해도 표식이 남지 않게 null 로 덮는다.
            meta.pop("activation_review_reason", None)
            meta["requires_admin_review"] = False
            row["verification_status"] = "verified"
        row["is_active"] = True
        row["is_selectable"] = True
        row["is_executable"] = True
        row["activation_source"] = "manual"
    # 자동 활성화가 꺼져 보류된 codex 행은 실행 가능 표시도 올리지 않는다.
    held_codex_ids = {
        str(row["model_id"])
        for row in model_rows
        if row["provider"] == "codex"
        and _row_meta(row).get("activation_review_reason") == "auto_activation_disabled"
    }
    cli_model_autoreg.apply_verified_codex(model_rows, set(verified_codex_ids) - held_codex_ids)
    # 캐시를 못 읽은 회차에는 codex_cli_cache 행을 은퇴시키지 않는다 — probe 가 세운 is_executable 을 지킨다.
    preserve_codex_cli_rows = not any(
        run.get("provider") == "codex" and run.get("status") == "ok" for run in discovery_runs
    )
    review_required_providers = sorted(
        {
            row["provider"]
            for row in provider_rows
            if row.get("requires_admin_review")
        }
        | {
            str(run.get("provider") or "").strip()
            for run in discovery_runs
            if run.get("status") in {"failed"}
        }
    )

    async with pool.acquire() as conn:
        try:
            async with conn.transaction():
                for row in model_rows:
                    metadata = _coerce_json_object(row.get("metadata"))
                    for key, value in _reviewed_release_fields(row["provider"], row["model_id"]).items():
                        metadata.setdefault(key, value)
                    metadata["sync_token"] = sync_token
                    capabilities = _coerce_json_object(row.get("capabilities"))
                    pricing = _coerce_json_object(row.get("pricing"))
                    tier, fallback_group = _catalog_tier(row["provider"], row["model_id"])
                    await conn.execute(
                        """
                        INSERT INTO llm_models (
                            provider, model_id, display_name, family, category,
                            supports_tools, supports_thinking, supports_vision, supports_coding,
                            input_cost, output_cost, is_active, activation_source,
                            linked_key_name, metadata, execution_model_id, discovery_source,
                            last_seen_at, retired_at, verification_status, last_verified_at,
                            capabilities, pricing, is_selectable, is_executable, updated_at,
                            tier, fallback_group
                        )
                        VALUES (
                            $1, $2, $3, $4, $5,
                            $6, $7, $8, $9,
                            $10, $11, $12, $13,
                            $14, $15::jsonb, $16, $17,
                            NOW(), NULL, $18, $19,
                            $20::jsonb, $21::jsonb, $22, $23, NOW(),
                            $24, $25
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
                            metadata = EXCLUDED.metadata || COALESCE((
                                SELECT jsonb_object_agg(key, value)
                                FROM jsonb_each(COALESCE(llm_models.metadata, '{}'::jsonb))
                                WHERE key IN ('release_date', 'release_date_source', 'release_date_verified_at')
                                  AND llm_models.metadata->>'release_date' IS NOT NULL
                                  AND llm_models.metadata->>'release_date_source' IS NOT NULL
                                  AND llm_models.metadata->>'release_date_verified_at' IS NOT NULL
                            ), '{}'::jsonb),
                            execution_model_id = EXCLUDED.execution_model_id,
                            discovery_source = EXCLUDED.discovery_source,
                            last_seen_at = NOW(),
                            retired_at = NULL,
                            verification_status = EXCLUDED.verification_status,
                            last_verified_at = EXCLUDED.last_verified_at,
                            capabilities = EXCLUDED.capabilities,
                            pricing = EXCLUDED.pricing,
                            is_selectable = EXCLUDED.is_selectable,
                            is_executable = EXCLUDED.is_executable,
                            tier = COALESCE(llm_models.tier, EXCLUDED.tier),
                            fallback_group = COALESCE(llm_models.fallback_group, EXCLUDED.fallback_group),
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
                        json.dumps(metadata, default=_json_default),
                        row.get("execution_model_id") or metadata.get("execution_model_id") or row["model_id"],
                        row.get("discovery_source") or metadata.get("discovery_source") or "template",
                        row.get("verification_status") or "unknown",
                        row.get("last_verified_at"),
                        json.dumps(capabilities, default=_json_default),
                        json.dumps(pricing, default=_json_default),
                        bool(row.get("is_selectable", row.get("is_active", False))),
                        bool(row.get("is_executable", row.get("is_active", False))),
                        tier,
                        fallback_group,
                    )

                await conn.execute(
                    """
                    UPDATE llm_models
                    SET is_active = FALSE,
                        is_selectable = FALSE,
                        is_executable = FALSE,
                        activation_source = CASE
                            WHEN activation_source = 'manual' THEN activation_source
                            ELSE 'fallback'
                        END,
                        metadata = COALESCE(metadata, '{}'::jsonb) || '{"retired": true}'::jsonb,
                        retired_at = COALESCE(retired_at, NOW()),
                        updated_at = NOW()
                    WHERE activation_source <> 'manual'
                      AND COALESCE(metadata->>'sync_token', '') <> $1
                      AND NOT ($2::boolean AND COALESCE(discovery_source, '') = 'codex_cli_cache')
                    """,
                    sync_token,
                    preserve_codex_cli_rows,
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
                        "normalized_providers": normalized_providers,
                        "review_required_providers": review_required_providers,
                        "discovery": discovery_runs,
                        "provider_status": provider_status,
                        "discovery_summary": discovery_summary,
                        "activation": activation,
                    },
                )
                for run in discovery_runs:
                    await conn.execute(
                        """
                        INSERT INTO llm_model_discovery_runs (
                            provider, status, discovered_count, active_count, error, details, triggered_by, reason
                        )
                        VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8)
                        """,
                        run.get("provider", "unknown"),
                        run.get("status", "unknown"),
                        int(run.get("count", 0) or 0),
                        int(run.get("active_count", 0) or 0),
                        run.get("error", ""),
                        json.dumps(run, default=_json_default),
                        triggered_by,
                        reason,
                    )
        except asyncpg.UndefinedTableError:
            logger.warning("model_registry.sync_missing_table")
            return {
                "ok": False,
                "error": "registry_tables_missing",
                "models_synced": 0,
                "providers": provider_rows,
                "normalized_providers": normalized_providers,
                "review_required_providers": review_required_providers,
            }

    # 후보 자동등록은 본 sync 트랜잭션 밖에서 한다 — 후보 테이블 문제로 레지스트리 sync 가 롤백되면 안 된다.
    cli_autoreg: dict[str, Any] = {}
    try:
        async with pool.acquire() as conn:
            cli_autoreg = await cli_model_autoreg.autoregister_after_sync(
                conn,
                model_rows=model_rows,
                existing_keys=existing_keys - newly_activated_keys,
                template_codex_ids=_PROVIDER_MODELS.get("codex", ()),
                verified_codex_ids=verified_codex_ids,
            )
    except Exception as exc:
        logger.warning("model_registry.cli_autoreg_failed: %s", str(exc)[:200])
        cli_autoreg = {"error": str(exc)[:200]}

    invalidate_registry_cache()
    await refresh_model_freshness_cache()
    # ok 는 '레지스트리 저장 성공' 이다. 회사별 최신 확인 여부는 discovery_status/provider_status 로 따로 본다.
    log_fn = logger.info if discovery_summary["discovery_complete"] else logger.warning
    log_fn(
        "model_registry.sync_done write_ok=True discovery_status=%s ok=%s skipped=%s error=%s stale=%s",
        discovery_summary["status"],
        ",".join(discovery_summary["ok"]),
        ",".join(discovery_summary["skipped"]),
        ",".join(discovery_summary["error"]),
        ",".join(discovery_summary["stale"]),
    )
    return {
        "ok": True,
        "models_synced": len(model_rows),
        "providers": provider_rows,
        "discovery": discovery_runs,
        "discovery_status": discovery_summary["status"],
        "discovery_complete": discovery_summary["discovery_complete"],
        "discovery_summary": discovery_summary,
        "provider_status": provider_status,
        "cli_autoreg": cli_autoreg,
        "activation": activation,
        "normalized_providers": normalized_providers,
        "review_required_providers": review_required_providers,
    }


async def clear_expired_rate_limits() -> dict[str, Any]:
    """만료된 rate_limited_until을 NULL로 초기화하고, 변경 시 registry를 재동기화한다."""
    pool = get_pool()
    cleared_keys: list[str] = []
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                UPDATE llm_api_keys
                SET rate_limited_until = NULL, updated_at = NOW()
                WHERE rate_limited_until IS NOT NULL
                  AND rate_limited_until <= NOW()
                RETURNING key_name, provider
                """
            )
            cleared_keys = [row["key_name"] for row in rows]
    except Exception:
        logger.exception("clear_expired_rate_limits.db_failed")
        return {"cleared": 0, "synced": False, "error": "db_failed"}

    if not cleared_keys:
        return {"cleared": 0, "synced": False, "reason": "none_expired"}

    logger.info(
        "clear_expired_rate_limits.cleared",
        extra={"keys": cleared_keys, "count": len(cleared_keys)},
    )
    try:
        from app.core.llm_key_provider import invalidate_key_cache
        invalidate_key_cache()
    except Exception:
        pass

    invalidate_registry_cache()
    try:
        result = await sync_model_registry(
            triggered_by="rate_limit_auto_clear",
            reason=f"expired_keys:{','.join(cleared_keys)}",
        )
        return {"cleared": len(cleared_keys), "synced": bool(result.get("ok")), "keys": cleared_keys}
    except Exception:
        logger.exception("clear_expired_rate_limits.sync_failed")
        return {"cleared": len(cleared_keys), "synced": False, "keys": cleared_keys, "error": "sync_failed"}
