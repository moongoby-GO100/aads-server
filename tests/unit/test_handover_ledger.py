from __future__ import annotations

import inspect
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.services import handover_store


def test_project_key_normalization_is_global_and_header_safe():
    assert handover_store.normalize_project_key("go100") == "GO100"
    assert handover_store.normalize_project_key("[AADS] control") == "AADS"
    assert handover_store.normalize_project_key("custom-project_1") == "CUSTOM-PROJECT_1"

    with pytest.raises(ValueError):
        handover_store.normalize_project_key('GO100"\r\nX-Evil: yes')


def test_legacy_markdown_import_is_stable_and_export_is_reversible():
    content = "# GO100 HANDOVER\n\nintro\n\n## Current status\n\nAPI healthy\n"
    first = handover_store.parse_markdown_sections(content, source_path="docs/HANDOVER.md")
    second = handover_store.parse_markdown_sections(content, source_path="docs/HANDOVER.md")

    assert first == second
    assert [item["title"] for item in first] == ["GO100 HANDOVER", "Current status"]

    entries = [
        {
            **first[1],
            "status": "active",
            "priority": "P1",
            "entry_type": "status",
            "revision": 2,
        }
    ]
    rendered = handover_store.render_handover_markdown(
        entries,
        project_key="GO100",
        generated_at=datetime(2026, 9, 9, tzinfo=timezone.utc),
    )
    assert "# GO100 HANDOVER" in rendered
    assert "정본은 AADS 중앙 handover ledger" in rendered
    assert "## [P1] Current status" in rendered
    assert "API healthy" in rendered


def test_handover_tools_are_eager_and_globally_available():
    from app.services.tool_registry import ToolRegistry

    registry = ToolRegistry()
    eager = {tool["name"] for tool in registry.get_eager_tools()}
    all_tools = {tool["name"] for tool in registry.get_tools("all")}

    assert {"handover_write", "handover_search"} <= eager
    assert {"handover_write", "handover_search", "handover_export"} <= all_tools


def test_tenant_scope_and_append_only_schema_contract():
    service_source = inspect.getsource(handover_store)
    migration = Path("migrations/167_global_handover_ledger.sql").read_text(encoding="utf-8")

    assert "tenant_id = $1::uuid" in service_source
    assert "UNIQUE (tenant_id, project_key, entry_key)" in migration
    assert "search_vector" in migration
    assert "gin_trgm_ops" in migration
    assert "trg_handover_events_append_only" in migration
    assert "DROP TABLE" not in migration.upper()
    assert "TRUNCATE" not in migration.upper()


@pytest.mark.asyncio
async def test_write_rejects_oversized_metadata_before_database_access():
    with pytest.raises(ValueError, match="metadata exceeds"):
        await handover_store.upsert_handover_entry(
            tenant_id="00000000-0000-0000-0000-000000000001",
            project_key="GO100",
            title="size guard",
            body="body",
            metadata={"payload": "x" * 100_001},
        )
