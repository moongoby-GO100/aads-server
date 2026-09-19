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
