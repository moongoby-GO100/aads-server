from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

import structlog

from app.core.db_pool import get_pool
from app.core.feature_flags import governance_enabled
from app.core.prompts.system_prompt_v2 import build_layer1, build_layer4

logger = structlog.get_logger(__name__)

LAYER_NAMES = {1: "global", 2: "project", 3: "role", 4: "intent", 5: "model"}


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _normalize_text_list(value: Any) -> list[str]:
    if value in (None, "", []):
        return []
    if isinstance(value, list):
        items = value
    elif isinstance(value, tuple):
        items = list(value)
    else:
        items = [value]
    normalized: list[str] = []
    for item in items:
        text = str(item or "").strip()
        if text and text not in normalized:
            normalized.append(text)
    return normalized


def _add_unique(items: list[str], value: Any) -> None:
    text = str(value or "").strip()
    if text and text not in items:
        items.append(text)


def _split_provider_qualified_model(value: str) -> tuple[str | None, str]:
    text = str(value or "").strip()
    if ":" not in text:
        return None, text
    provider, model_id = text.split(":", 1)
    provider = provider.strip().lower()
    model_id = model_id.strip()
    known_providers = {
        "anthropic",
        "codex",
        "deepseek",
        "gemini",
        "groq",
        "kimi",
        "litellm",
        "minimax",
        "openai",
        "openrouter",
        "qwen",
    }
    if provider in known_providers and model_id:
        return provider, model_id
    return None, text


def _infer_model_keys_from_text(model_id: str) -> list[str]:
    raw = str(model_id or "").strip()
    if not raw:
        return []
    provider, model = _split_provider_qualified_model(raw)
    lower = model.lower()
    keys: list[str] = []
    _add_unique(keys, raw)
    _add_unique(keys, lower)
    if provider:
        _add_unique(keys, f"provider:{provider}")
        _add_unique(keys, f"{provider}:{model}")

    if any(token in lower for token in ("claude", "opus", "sonnet", "haiku")):
        _add_unique(keys, "provider:anthropic")
        _add_unique(keys, "family:claude")
        _add_unique(keys, "capability:tools")
        _add_unique(keys, "capability:coding")
        if "opus" in lower:
            _add_unique(keys, "performance:deep")
            _add_unique(keys, "cost:high")
        elif "haiku" in lower:
            _add_unique(keys, "performance:fast")
            _add_unique(keys, "cost:low")
        else:
            _add_unique(keys, "performance:balanced")
            _add_unique(keys, "cost:medium")
    if lower.startswith(("gpt-", "o1", "o3", "o4")) or "openai" in lower:
        _add_unique(keys, "provider:openai")
        _add_unique(keys, "family:gpt")
        _add_unique(keys, "capability:tools")
        _add_unique(keys, "capability:coding")
        if any(token in lower for token in ("reason", "o1", "o3", "o4", "gpt-5")):
            _add_unique(keys, "capability:thinking")
            _add_unique(keys, "category:reasoning")
    if "codex" in lower:
        _add_unique(keys, "provider:codex")
        _add_unique(keys, "family:codex")
        _add_unique(keys, "category:coding")
        _add_unique(keys, "capability:coding")
        _add_unique(keys, "capability:tools")
    if "gemini" in lower or "gemma" in lower:
        _add_unique(keys, "provider:gemini")
        _add_unique(keys, "family:gemini")
        if "vision" in lower or "image" in lower:
            _add_unique(keys, "capability:vision")
    if "qwen" in lower:
        _add_unique(keys, "provider:qwen")
        _add_unique(keys, "family:qwen")
    if lower.startswith("groq-") or "groq" in lower:
        _add_unique(keys, "provider:groq")
        _add_unique(keys, "performance:fast")
    if "kimi" in lower:
        _add_unique(keys, "provider:kimi")
        _add_unique(keys, "family:kimi")
    if "deepseek" in lower:
        _add_unique(keys, "provider:deepseek")
        _add_unique(keys, "family:deepseek")
    if "minimax" in lower:
        _add_unique(keys, "provider:minimax")
    if "vision" in lower or "image" in lower:
        _add_unique(keys, "capability:vision")
    return keys


def _cost_tier(input_cost: Any, output_cost: Any) -> str:
    try:
        high_watermark = max(float(input_cost or 0), float(output_cost or 0))
    except (TypeError, ValueError):
        return ""
    if high_watermark >= 10:
        return "high"
    if high_watermark >= 1:
        return "medium"
    if high_watermark > 0:
        return "low"
    return ""


async def _registry_model_keys(conn: Any, model_ids: list[str]) -> list[str]:
    if not model_ids or not await _table_exists(conn, "llm_models"):
        return []
    normalized_model_ids: list[str] = []
    provider_pairs: set[tuple[str, str]] = set()
    for item in model_ids:
        provider, model_id = _split_provider_qualified_model(item)
        if model_id:
            _add_unique(normalized_model_ids, model_id)
        if provider and model_id:
            provider_pairs.add((provider, model_id))
    if not normalized_model_ids:
        return []
    rows = await conn.fetch(
        """
        SELECT provider, model_id, family, category,
               supports_tools, supports_thinking, supports_vision, supports_coding,
               input_cost, output_cost
        FROM llm_models
        WHERE model_id = ANY($1::text[])
        """,
        normalized_model_ids,
    )
    keys: list[str] = []
    for row in rows:
        provider = str(row["provider"] or "").strip().lower()
        model_id = str(row["model_id"] or "").strip()
        if provider_pairs and (provider, model_id) not in provider_pairs and model_id not in model_ids:
            continue
        _add_unique(keys, model_id)
        if provider:
            _add_unique(keys, f"{provider}:{model_id}")
            _add_unique(keys, f"provider:{provider}")
        if row["family"]:
            _add_unique(keys, f"family:{str(row['family']).strip().lower()}")
        if row["category"]:
            _add_unique(keys, f"category:{str(row['category']).strip().lower()}")
        if row["supports_tools"]:
            _add_unique(keys, "capability:tools")
        if row["supports_thinking"]:
            _add_unique(keys, "capability:thinking")
        if row["supports_vision"]:
            _add_unique(keys, "capability:vision")
        if row["supports_coding"]:
            _add_unique(keys, "capability:coding")
        tier = _cost_tier(row["input_cost"], row["output_cost"])
        if tier:
            _add_unique(keys, f"cost:{tier}")
    return keys


async def _table_exists(conn: Any, table_name: str) -> bool:
    try:
        return bool(await conn.fetchval("SELECT to_regclass($1)", f"public.{table_name}"))
    except Exception:
        return False


@dataclass
class CompiledPrompt:
    system_prompt: str
    provenance: dict[str, Any]


# ── 프롬프트 토큰 예산 — 2026-09-16 실측 ────────────────────────────────────
#
# **글자 수로 토큰을 추정하지 마라.** 그날 두 번 틀렸고, 둘 다 우선순위를
# 거꾸로 잡게 만들었다.
#
#   추정            실측        배수
#   도구 25,000tok  1,768tok    14배 과대   (글자 수 ÷ 3 으로 계산)
#   메모리 14,700   2,700       5.4배 과대  (총량 − 자산합 뺄셈)
#
# 실측 계수: 한국어 혼합 프롬프트 36,546자 = 26,677토큰 → **약 1.37자/토큰**.
# (기존 코드의 ÷1.5 는 과소 추정이다)
#
# 한 턴 입력 44,343토큰의 실제 분해 (질문·히스토리 제외):
#
#   AADS 시스템 프롬프트   26,677  60.2%
#     ├ 프롬프트 자산 16,359자 ≈ 11,900   ← global 15개가 자산의 82%. 최대 절감처
#     └ context_builder 17,788자 ≈ 12,983
#          layer1  9,000자 ≈ 6,570 / memory 3,702자 ≈ 2,700
#          preload 3,300자 ≈ 2,400 / 기타 1,786자 ≈ 1,300
#   CLI 자체 프롬프트      13,258  29.9%   손댈 수 없음
#   Task/Agent              2,664   6.0%   차단하면 서브에이전트가 죽는다(실측)
#   MCP 도구 142개          1,768   4.0%   상주 11개로 줄여도 1,727 절감뿐
#
# 측정 방법: claude -p --output-format stream-json --verbose 로 usage 의
# input+cache_creation+cache_read 합을 읽고, 옵션을 하나씩 빼며 차분을 본다.
# 오류 사전: prompt.token_budget_estimated_by_chars
# 설계: aads-docs/docs/PRD-PROMPT-CONTEXT-RETRIEVAL-v1.0.md


def _section_chars_snapshot() -> dict:
    """직전 턴의 구간별 자수. 조립은 context_builder 가 하고 기록은 여기서 한다."""
    try:
        from app.services.context_builder import _SECTION_CHARS_LAST

        return dict(_SECTION_CHARS_LAST)
    except Exception:
        return {}


def _server_ledger_snapshot(session_id: str) -> dict:
    """**이 세션** 이 이번 턴에 주입한 서버 원장의 출처·조회시각·hash.

    이것을 남기지 않으면 "그 턴이 어떤 서버 구성을 보고 답했는지" 를 나중에
    증명도 반증도 할 수 없다. 2026-06 "3서버" 오인이 그래서 추적에 오래 걸렸다.

    `session_id` 를 반드시 받는다. 예전에는 모듈 전역 "직전 값" 을 읽었는데,
    두 세션이 동시에 조립하면 나중 조립이 앞 값을 덮어 **세션 A 의 기록에
    세션 B 의 원장** 이 들어갔다(2026-10-01 root 검수 지적). 꺼낼 때 칸을
    비우므로, 원장 주입이 없었던 턴은 빈 값이 되어 이전 턴 값을 물려받지 않는다.
    """
    try:
        from app.services.context_builder import take_ledger_provenance

        return dict(take_ledger_provenance(session_id) or {})
    except Exception:
        return {}


class PromptCompiler:
    async def compile(
        self,
        *,
        workspace_name: str,
        intent: str,
        model: str,
        session_id: str,
        role: str = "",
        selected_model_id: str = "",
        execution_model_id: str = "",
        model_match_keys: list[str] | None = None,
        base_system_prompt: str = "",
    ) -> CompiledPrompt:
        workspace_key = (workspace_name or "CEO").strip() or "CEO"
        intent_key = (intent or "").strip()
        model_key = (model or "").strip()
        selected_model_key = (selected_model_id or model_key).strip()
        execution_model_key = (execution_model_id or model_key).strip()
        role_key = (role or "").strip()
        compiled = (base_system_prompt or "").strip()
        if not compiled:
            compiled = build_layer1(workspace_key, "", intent=intent_key)
            compiled = compiled + "\n\n" + build_layer4()

        base_model_keys: list[str] = []
        for item in (model_key, selected_model_key, execution_model_key, *(model_match_keys or [])):
            _add_unique(base_model_keys, item)
        expanded_model_keys: list[str] = []
        for item in base_model_keys:
            for key in _infer_model_keys_from_text(item):
                _add_unique(expanded_model_keys, key)
        if not expanded_model_keys:
            expanded_model_keys = [""]

        provenance: dict[str, Any] = {
            "workspace": workspace_key,
            "intent": intent_key,
            "model": model_key,
            "selected_model_id": selected_model_key,
            "execution_model_id": execution_model_key,
            "model_match_keys": expanded_model_keys,
            "role": role_key,
            "session_id": session_id,
            "governance_enabled": False,
            "base_prompt_chars": len(compiled),
            "applied_assets": [],
            "layers_applied": {},
            "blueprint": None,
            "fallback_used": True,
        }

        if not await governance_enabled(default=True):
            provenance["system_prompt_hash"] = _sha256(compiled)
            provenance["system_prompt_chars"] = len(compiled)
            return CompiledPrompt(system_prompt=compiled, provenance=provenance)

        try:
            pool = get_pool()
            async with pool.acquire() as conn:
                assets_exist = await _table_exists(conn, "prompt_assets")
                blueprints_exist = await _table_exists(conn, "session_blueprints")

                applied_assets: list[dict[str, Any]] = []
                layers_applied: dict[str, int] = {}
                if assets_exist:
                    registry_keys = await _registry_model_keys(conn, base_model_keys)
                    for key in registry_keys:
                        _add_unique(expanded_model_keys, key)
                    provenance["model_match_keys"] = expanded_model_keys
                    rows = await conn.fetch(
                        """
                        SELECT slug, layer_id, content, model_variants, priority
                        FROM prompt_assets
                        WHERE enabled = TRUE
                          AND (workspace_scope IS NULL OR array_length(workspace_scope, 1) IS NULL OR $1 = ANY(workspace_scope) OR '*' = ANY(workspace_scope))
                          AND (intent_scope IS NULL OR array_length(intent_scope, 1) IS NULL OR $2 = ANY(intent_scope) OR '*' = ANY(intent_scope))
                          AND (target_models IS NULL OR array_length(target_models, 1) IS NULL OR target_models && $3::text[] OR '*' = ANY(target_models))
                          AND (role_scope IS NULL OR array_length(role_scope, 1) IS NULL OR $4 = ANY(role_scope) OR '*' = ANY(role_scope))
                        ORDER BY layer_id ASC, priority ASC, slug ASC
                        """,
                        workspace_key,
                        intent_key,
                        expanded_model_keys,
                        role_key,
                    )
                    # 역할을 줬는데 한 건도 안 걸리면 범위가 어긋난 것이다.
                    #
                    # 2026-09-14 같은 함정에 두 번 빠졌다 — 한 번은 role 을 빈
                    # 문자열로, 한 번은 workspace 를 표시명으로 넘겨서. 둘 다
                    # 오류 없이 "자산 0건" 으로 조용히 끝났고, 프롬프트는 3만
                    # 자가 넘으니 길이만 봐서는 붙은 줄 안다.
                    if role_key and not rows:
                        logger.warning(
                            "prompt_assets_none_selected",
                            workspace=workspace_key,
                            role=role_key,
                            intent=intent_key,
                            hint="workspace_scope/role_scope 와 넘긴 값이 어긋났을 수 있다",
                        )

                    extras: list[str] = []
                    for row in rows:
                        variants = row["model_variants"] or {}
                        text = ""
                        if isinstance(variants, str):
                            try:
                                variants = json.loads(variants)
                            except json.JSONDecodeError:
                                variants = {}
                        if isinstance(variants, dict):
                            selected = None
                            for key in expanded_model_keys:
                                selected = variants.get(key) or variants.get(key.lower())
                                if selected:
                                    break
                            if isinstance(selected, dict):
                                text = str(selected.get("content") or "").strip()
                            elif isinstance(selected, str):
                                text = selected.strip()
                        if not text:
                            text = str(row["content"] or "").strip()
                        if not text:
                            continue
                        extras.append(text)
                        lid = row["layer_id"] or 0
                        layer_name = LAYER_NAMES.get(lid, f"L{lid}")
                        layers_applied[layer_name] = layers_applied.get(layer_name, 0) + 1
                        applied_assets.append(
                            {
                                "slug": row["slug"],
                                "layer_id": lid,
                                "layer_name": layer_name,
                                "priority": row["priority"],
                                "chars": len(text),
                            }
                        )
                    if extras:
                        compiled = compiled + "\n\n" + "\n\n".join(extras)

                blueprint = None
                if blueprints_exist:
                    row = await conn.fetchrow(
                        """
                        SELECT slug, lite_mode, skip_sections, extra_sections, extra_skip_sections
                        FROM session_blueprints
                        WHERE is_active = TRUE
                          AND (workspace_scope IS NULL OR array_length(workspace_scope, 1) IS NULL OR $1 = ANY(workspace_scope) OR '*' = ANY(workspace_scope))
                          AND (intent_scope IS NULL OR array_length(intent_scope, 1) IS NULL OR $2 = ANY(intent_scope) OR '*' = ANY(intent_scope))
                        ORDER BY priority ASC, slug ASC
                        LIMIT 1
                        """,
                        workspace_key,
                        intent_key,
                    )
                    if row:
                        blueprint = {
                            "slug": row["slug"],
                            "lite_mode": bool(row["lite_mode"]),
                            "skip_sections": _normalize_text_list(row["skip_sections"]),
                            "extra_sections": _normalize_text_list(row["extra_sections"]),
                            "extra_skip_sections": _normalize_text_list(row["extra_skip_sections"]),
                        }

                provenance.update(
                    {
                        "governance_enabled": True,
                        "applied_assets": applied_assets,
                        "layers_applied": layers_applied,
                        "blueprint": blueprint,
                        "fallback_used": not bool(applied_assets or blueprint),
                    }
                )
        except Exception as exc:
            provenance["compile_error"] = str(exc)

        provenance["system_prompt_hash"] = _sha256(compiled)
        provenance["system_prompt_chars"] = len(compiled)
        return CompiledPrompt(system_prompt=compiled, provenance=provenance)


async def record_prompt_provenance(
    *,
    conn: Any,
    session_id: str,
    execution_id: str | None,
    intent: str,
    model: str,
    compiled_prompt: CompiledPrompt,
    server_ledger: dict | None = None,
) -> None:
    # 증거는 **테이블 존재 확인보다 먼저** 채운다.
    #
    # 예전에는 `_table_exists()` 가 거짓이면 여기서 바로 return 했고, 그
    # 경로에서는 `section_chars`·`server_ledger` 가 `compiled_prompt` 에
    # 아예 붙지 않았다. DB INSERT 를 건너뛰는 것은 맞지만, 호출자가 메모리의
    # provenance 를 읽는 경로(SDK 조기-return 등)까지 같이 비어 버린다 —
    # 2026-10-01 root 검수가 지적한 조기-return 증거 손실이 이것이다.
    # 채우는 일은 DB 와 무관하므로 순서를 바꾸는 것만으로 양쪽이 산다.

    # 구간별 자수. 컴파일 시점에 넣으면 아직 조립 전이라 비어 있거나 직전 턴
    # 값이 들어간다(2026-09-16 실측: 전부 `{}`). 이 함수는 조립이 끝난 뒤
    # 호출되므로 그때 값이 맞다. 총량만 남기면 구간 크기를 뺄셈으로 추정하게
    # 되고 그 추정이 틀린다 — 오류 사전 prompt.token_budget_estimated_by_chars.
    try:
        sections = _section_chars_snapshot()
        if sections:
            compiled_prompt.provenance["section_chars"] = sections
    except Exception:
        pass

    # 어떤 서버 원장을 보고 이 턴을 처리했는지 남긴다 (PRD §7).
    # 조회 실패도 그대로 남긴다 — `available: false` 가 보이면 그 턴의 서버
    # 발언은 근거가 없었다는 뜻이고, 그 판정을 사후에 할 수 있어야 한다.
    #
    # 호출자가 조립 직후 꺼내 둔 값(`server_ledger`)을 **우선** 쓴다. 그 값은
    # 그 턴의 조립이 반환한 것이라 다른 세션·다른 턴과 섞일 길이 없다.
    # 넘기지 않은 호출자만 세션 칸에서 꺼낸다(하위 호환).
    try:
        ledger = (
            dict(server_ledger)
            if server_ledger is not None
            else _server_ledger_snapshot(session_id)
        )
        if ledger:
            compiled_prompt.provenance["server_ledger"] = ledger
    except Exception:
        pass

    if not await _table_exists(conn, "compiled_prompt_provenance"):
        return

    await conn.execute(
        """
        INSERT INTO compiled_prompt_provenance (
            session_id,
            execution_id,
            intent,
            model,
            system_prompt_hash,
            system_prompt_chars,
            provenance
        )
        VALUES ($1::uuid, $2::uuid, $3, $4, $5, $6, $7::jsonb)
        """,
        session_id,
        execution_id,
        intent,
        model,
        compiled_prompt.provenance.get("system_prompt_hash"),
        compiled_prompt.provenance.get("system_prompt_chars"),
        json.dumps(compiled_prompt.provenance, ensure_ascii=False),
    )
