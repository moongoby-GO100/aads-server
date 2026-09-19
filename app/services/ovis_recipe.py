"""Canonical OVISRecipe references shared by legacy recipe registries.

The legacy tables remain readable/writable adapters during the migration.  This
module deliberately stores a lossless snapshot and a stable reference rather
than attempting to translate browser, work, and Site Skill payloads into a
lowest-common-denominator schema.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def recipe_checksum(definition: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(definition).encode("utf-8")).hexdigest()


async def sync_legacy_reference(
    *,
    tenant_id: Any,
    canonical_key: str,
    version: str,
    status: str,
    source_type: str,
    source_id: Any,
    definition: Mapping[str, Any],
    approval_scope: Mapping[str, Any] | None = None,
) -> dict[str, str] | None:
    """Mirror a legacy version into OVISRecipe without changing legacy behavior.

    `None` means the additive migration has not been applied yet.  This keeps
    old API callers compatible during a rolling migration while making the
    reference contract the only new cross-module integration point.
    """
    from app.core.db_pool import get_pool

    payload = dict(definition)
    scope = dict(approval_scope or {})
    checksum = recipe_checksum(payload)
    try:
        async with get_pool().acquire() as conn:
            async with conn.transaction():
                recipe = await conn.fetchrow(
                    """
                    INSERT INTO ovis_recipes (tenant_id, canonical_key)
                    VALUES ($1::uuid, $2)
                    ON CONFLICT (tenant_id, canonical_key) DO UPDATE
                        SET updated_at=clock_timestamp()
                    RETURNING id
                    """,
                    str(tenant_id), canonical_key,
                )
                version_row = await conn.fetchrow(
                    """
                    INSERT INTO ovis_recipe_versions
                        (recipe_id, version, status, definition, definition_sha256, approval_scope)
                    VALUES ($1, $2, $3, $4::jsonb, $5, $6::jsonb)
                    ON CONFLICT (recipe_id, version) DO UPDATE
                       SET updated_at=ovis_recipe_versions.updated_at
                    RETURNING id
                    """,
                    recipe["id"], version, status, canonical_json(payload), checksum, canonical_json(scope),
                )
                await conn.execute(
                    """
                    INSERT INTO ovis_recipe_legacy_refs
                        (ovis_recipe_version_id, source_type, source_id, rollback_definition)
                    VALUES ($1, $2, $3, $4::jsonb)
                    ON CONFLICT (source_type, source_id) DO UPDATE
                       SET ovis_recipe_version_id=EXCLUDED.ovis_recipe_version_id,
                           rollback_definition=EXCLUDED.rollback_definition,
                           updated_at=clock_timestamp()
                    """,
                    version_row["id"], source_type, str(source_id), canonical_json(payload),
                )
    except Exception as exc:
        # Undefined-table is expected before the additive migration.  Do not
        # hide a real database failure once OVISRecipe exists.
        if getattr(exc, "sqlstate", None) == "42P01":
            return None
        raise
    return {"recipe_id": str(recipe["id"]), "version_id": str(version_row["id"])}
