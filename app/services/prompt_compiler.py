from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from app.core.db_pool import get_pool
from app.core.feature_flags import governance_enabled
from app.core.prompts.system_prompt_v2 import build_layer1, build_layer4


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


async def _table_exists(conn: Any, table_name: str) -> bool:
    try:
        return bool(await conn.fetchval("SELECT to_regclass($1)", f"public.{table_name}"))
    except Exception:
        return False


@dataclass
class CompiledPrompt:
    system_prompt: str
    provenance: dict[str, Any]


class PromptCompiler:
    async def compile(
        self,
        *,
        workspace_name: str,
        intent: str,
        model: str,
        session_id: str,
        base_system_prompt: str = "",
    ) -> CompiledPrompt:
        workspace_key = (workspace_name or "CEO").strip() or "CEO"
        intent_key = (intent or "").strip()
        model_key = (model or "").strip()
        compiled = (base_system_prompt or "").strip()
        if not compiled:
            compiled = build_layer1(workspace_key, "", intent=intent_key)
            compiled = compiled + "\n\n" + build_layer4()

        provenance: dict[str, Any] = {
            "workspace": workspace_key,
            "intent": intent_key,
            "model": model_key,
            "session_id": session_id,
            "governance_enabled": False,
            "base_prompt_chars": len(compiled),
            "applied_assets": [],
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
                if assets_exist:
                    rows = await conn.fetch(
                        """
                        SELECT slug, layer_id, content, model_variants, priority
                        FROM prompt_assets
                        WHERE enabled = TRUE
                          AND (workspace_scope IS NULL OR array_length(workspace_scope, 1) IS NULL OR $1 = ANY(workspace_scope) OR '*' = ANY(workspace_scope))
                          AND (intent_scope IS NULL OR array_length(intent_scope, 1) IS NULL OR $2 = ANY(intent_scope) OR '*' = ANY(intent_scope))
                          AND (target_models IS NULL OR array_length(target_models, 1) IS NULL OR $3 = ANY(target_models) OR '*' = ANY(target_models))
                        ORDER BY layer_id ASC, priority ASC, slug ASC
                        """,
                        workspace_key,
                        intent_key,
                        model_key,
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
                            selected = variants.get(model_key) or variants.get(model_key.lower())
                            if isinstance(selected, dict):
                                text = str(selected.get("content") or "").strip()
                            elif isinstance(selected, str):
                                text = selected.strip()
                        if not text:
                            text = str(row["content"] or "").strip()
                        if not text:
                            continue
                        extras.append(text)
                        applied_assets.append(
                            {
                                "slug": row["slug"],
                                "layer_id": row["layer_id"],
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
) -> None:
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
