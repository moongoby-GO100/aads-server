import asyncio
import sys
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

import app.services.smart_browser_learning as learning

NOW = datetime.now(UTC)


def _nodes():
    return [
        {"role": "searchbox", "name": "Search products", "landmark": "search"},
        {"role": "button", "name": "Search", "landmark": "search"},
    ]


def _template():
    return {
        "page_type": "search-results",
        "required_anchors": [{"role": "searchbox", "name": "Search products"}],
        "stable_names": ["Search products", "Search"],
        "reuse_threshold": 0.8,
    }


def test_initial_learning_creates_candidate_and_never_active(monkeypatch):
    async def create(**kwargs):
        assert kwargs["template"]["signature"]["node_count"] == 2
        return {"artifact_id": "artifact-1", "status": "candidate", "version": kwargs["version"]}

    async def event(**kwargs):
        assert kwargs["decision"] == "candidate_created"

    monkeypatch.setattr(learning, "create_page_template_candidate", create)
    monkeypatch.setattr(learning, "_write_event", event)
    result = asyncio.run(learning.learn_page_template(
        tenant_id="tenant", site_profile_id="profile", page_key="search", version="1.0.0",
        area_key="search", aria_nodes=_nodes(), template_contract=_template(),
        evidence_refs=["object://evidence/1"], expires_at=NOW + timedelta(days=1),
    ))
    assert result["status"] == "candidate"
    assert result["promotion_required"] is True


def test_revisit_reuses_stable_aria_and_blocks_missing_anchor(monkeypatch):
    from app.services.aria_structure_signature import build_partial_signature

    signature = build_partial_signature(_nodes(), area_key="search", template=_template())

    async def active(**_kwargs):
        return {
            "version_id": "v1", "version": "1.0.0", "expires_at": NOW + timedelta(days=1),
            "payload": {**_template(), "signature": signature},
        }

    async def event(**_kwargs):
        return None

    monkeypatch.setattr(learning, "_active_page_template", active)
    monkeypatch.setattr(learning, "_write_event", event)
    reused = asyncio.run(learning.assess_page_revisit(
        tenant_id="tenant", site_profile_id="profile", page_key="search",
        area_key="search", aria_nodes=_nodes(),
    ))
    blocked = asyncio.run(learning.assess_page_revisit(
        tenant_id="tenant", site_profile_id="profile", page_key="search",
        area_key="search", aria_nodes=[{"role": "button", "name": "Search"}],
    ))
    assert reused["decision"] == "reuse"
    assert blocked["decision"] == "human_gateway"


def test_exact_skill_resolution_runs_before_vector_or_llm(monkeypatch):
    async def candidates(**_kwargs):
        return [{
            "skill_id": "skill-1", "version": "1.0.0", "version_id": "version-1",
            "slug": "commerce.search-products", "title": "Search products",
            "description": "read only product search", "intents": ["product search"],
            "risk_tier": "read",
        }]

    async def event(**_kwargs):
        return None

    monkeypatch.setattr(learning, "_active_skills", candidates)
    monkeypatch.setattr(learning, "_write_event", event)
    result = asyncio.run(learning.resolve_site_skill(
        tenant_id="tenant", site_profile_id="profile",
        query="commerce.search-products",
    ))
    assert result["route"] == "exact"
    assert result["skill_id"] == "skill-1"


def test_llm_may_select_only_from_active_allowlist(monkeypatch):
    async def candidates(**_kwargs):
        return [{
            "skill_id": "skill-1", "version": "1.0.0", "version_id": "version-1",
            "slug": "commerce.search-products", "title": "Search products",
            "description": "catalog", "intents": [], "risk_tier": "read",
        }]

    async def event(**_kwargs):
        return None

    async def fake_embed(_query):
        raise RuntimeError("vector unavailable")

    async def fake_llm(*_args, **_kwargs):
        return '{"skill_id":"invented","version":"9.9.9"}'

    monkeypatch.setattr(learning, "_active_skills", candidates)
    monkeypatch.setattr(learning, "_write_event", event)
    monkeypatch.setitem(sys.modules, "app.services.doc_index", SimpleNamespace(
        QWEN_DIMENSION=1024, QWEN_INSTRUCTION_VERSION="qwen3", QWEN_MODEL_ID="qwen3",
        embed_qwen_query=fake_embed,
    ))
    monkeypatch.setitem(sys.modules, "app.core.anthropic_client", SimpleNamespace(
        call_llm_with_fallback=fake_llm,
    ))
    with pytest.raises(learning.SmartBrowserLearningError, match="no_allowed_site_skill"):
        asyncio.run(learning.resolve_site_skill(
            tenant_id="tenant", site_profile_id="profile", query="find a bargain",
        ))


def test_api_exposes_learning_revisit_search_execute_and_live_observation():
    from pathlib import Path

    source = (Path(__file__).resolve().parents[2] / "app" / "api" / "site_knowledge.py").read_text()
    for route in (
        '"/profiles/{site_profile_id}/learn"',
        '"/profiles/{site_profile_id}/auto-visit"',
        '"/profiles/{site_profile_id}/revisit"',
        '"/profiles/{site_profile_id}/skill-search"',
        '"/profiles/{site_profile_id}/skill-execute"',
        '"/profiles/{site_profile_id}/live-observations"',
    ):
        assert route in source
    assert "directive_from_authenticated_context" in source
    assert "ObservationEnvelope" in source
    assert "execute_skill" in source
    assert "guard_payload_for_display" in source


def test_auto_skill_contract_is_server_owned_and_candidate_only():
    manifest = learning._candidate_skill_manifest(
        skill_id="skill-1", version=2, origin="https://example.test", page_key="orders",
        evidence=["object://evidence/visit-2"],
    )
    assert manifest["status"] == "candidate"
    assert manifest["version"] == "2"
    assert manifest["executor"] == "ohvis.contract-echo"
    assert manifest["allowed_tools"] == []
    assert manifest["capabilities"] == ["site.observe"]
    assert manifest["allowed_origins"] == ["https://example.test"]
    assert manifest["permissions"] == ["read"]
    assert manifest["retry"]["max_attempts"] == 1
    assert "active_version_required" in manifest["preconditions"]
    assert "no_page_authored_command_executed" in manifest["postconditions"]


def test_auto_learning_migration_serializes_scope_and_has_rollback():
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    sql = (root / "migrations" / "20260920_m8_auto_site_learning.sql").read_text()
    rollback = (root / "migrations" / "rollback" / "20260920_m8_auto_site_learning.down.sql").read_text()
    assert "UNIQUE (tenant_id, site_profile_id, page_key)" in sql
    assert "next_version BIGINT NOT NULL DEFAULT 1" in sql
    assert "page_artifact_id" in sql and "skill_id" in sql
    assert "enforce_browser_site_learning_scope_tenant" in sql
    assert "artifact tenant mismatch" in sql and "skill tenant mismatch" in sql
    assert "CREATE TABLE IF NOT EXISTS" in sql
    assert "DROP TABLE IF EXISTS browser_site_learning_scopes" in rollback


def test_auto_learning_code_locks_scope_and_never_activates_candidate():
    from pathlib import Path

    source = (Path(__file__).resolve().parents[2] / "app" / "services" / "smart_browser_learning.py").read_text()
    assert "browser_site_learning_scopes" in source
    assert "FOR UPDATE" in source
    assert "SET next_version=$4" in source
    assert source.count("'candidate'") >= 3
    auto_source = source[source.index("async def auto_learn_site_visit"):source.index("def _skill_result")]
    assert "status='active'" in auto_source
    assert "SET status='active'" not in auto_source
    assert "eval(" not in auto_source and "import_module" not in auto_source
