from __future__ import annotations

import os
import sys
import types
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import asyncpg
import pytest

from app.api.learned_artifacts import PromotionRequest, promote_learned_artifact
from app.core import db_pool
from app.services.golden_promotion_gate import MANDATORY_GOLDEN_CASES
from app.services.ohvis_harness import promote_skill_version
from app.services.smart_browser_learning import (
    auto_learn_site_visit,
    index_site_skill_embedding,
    resolve_site_skill,
)


def _url() -> str:
    value = os.getenv("SMART_BROWSER_M11_TEST_DATABASE_URL", "")
    if not value:
        pytest.skip("SMART_BROWSER_M11_TEST_DATABASE_URL is required")
    if "smartbrowser_m11_" not in value:
        pytest.fail("SMART_BROWSER_M11_TEST_DATABASE_URL must target a disposable smartbrowser_m11_* database")
    return value


def _nodes(*, include_searchbox: bool = True) -> list[dict[str, str]]:
    nodes = [{"role": "button", "name": "검색", "landmark": "search"}]
    if include_searchbox:
        nodes.insert(0, {"role": "searchbox", "name": "상품 검색", "landmark": "search"})
    return nodes


def _template() -> dict[str, object]:
    return {
        "page_type": "search-results",
        "required_anchors": [{"role": "searchbox", "name": "상품 검색"}],
        "stable_names": ["상품 검색", "검색"],
        "reuse_threshold": 0.8,
    }


def _gate_inputs() -> tuple[dict, dict, dict, dict, list[dict]]:
    focused = {
        suite: {"status": "passed", "critical_complete": True, "authorization_passed": True}
        for suite in ("security", "functional", "aria", "freshness", "regression", "audit")
    }
    focused["golden_cases"] = {
        name: {
            "status": "passed",
            "fixture_sha256": "a" * 64,
            "evidence": "object://m11/golden",
        }
        for name in MANDATORY_GOLDEN_CASES
    }
    affected = {"status": "passed", "selected": ["g1", "g2", "g3", "g4", "g5", "g6"]}
    metrics = {"success_rate": 1.0, "cost_usd": 0.0, "latency_ms": 1}
    evidence = [
        {"type": "aads_handover_db", "entry_key": "m11-integration-test"},
        {"type": "release_state", "commit": "test", "push": "test", "deploy": "test"},
    ]
    return focused, affected, metrics, dict(metrics), evidence


@pytest.mark.asyncio
async def test_m11_real_learning_promotion_reuse_vector_and_invalidation(monkeypatch) -> None:
    database_url = _url()
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("DB_POOL_MIN_SIZE", "1")
    monkeypatch.setenv("DB_POOL_MAX_SIZE", "5")
    db_pool._pool = None
    await db_pool.init_pool()

    tenant_id, site_profile_id = uuid4(), uuid4()
    suffix = uuid4().hex
    setup = await asyncpg.connect(database_url)
    original_doc_index = sys.modules.get("app.services.doc_index")
    try:
        await setup.execute(
            "INSERT INTO tenants(id,slug,name) VALUES($1,$2,$3)",
            tenant_id,
            f"m11-{suffix}",
            "M11 verification",
        )
        await setup.execute(
            """INSERT INTO authenticated_site_profiles
               (id,tenant_id,project_key,site_key,display_name,base_origin,allowed_origins,runtime)
               VALUES($1,$2,'AADS',$3,'M11 Shop','https://shop.example',
                      '[\"https://shop.example\"]'::jsonb,'playwright_server')""",
            site_profile_id,
            tenant_id,
            f"m11-shop-{suffix}",
        )
        kwargs = {
            "tenant_id": str(tenant_id),
            "site_profile_id": str(site_profile_id),
            "page_key": "catalog-search",
            "area_key": "catalog",
            "aria_nodes": _nodes(),
            "template_contract": _template(),
            "evidence": [f"object://m11/{suffix}/01-first-learning.png"],
            "expires_at": datetime.now(UTC) + timedelta(hours=1),
        }
        first = await auto_learn_site_visit(**kwargs)
        assert first["reason_code"] == "first_visit_candidates_created"
        assert first["version"] == "1"
        assert first["evidence_refs"] == kwargs["evidence"]

        scope = await setup.fetchrow(
            """SELECT page_artifact_id,skill_id FROM browser_site_learning_scopes
               WHERE tenant_id=$1 AND site_profile_id=$2 AND page_key='catalog-search'""",
            tenant_id,
            site_profile_id,
        )
        focused, affected, candidate_metrics, active_metrics, evidence = _gate_inputs()
        context = {"tenant": {"id": str(tenant_id)}, "user": {"email": "m11@test.local"}}
        for stage in ("shadow", "active"):
            request = PromotionRequest(
                idempotency_key=f"m11-page-{stage}-{suffix}",
                evidence=evidence,
                focused_results=focused,
                affected_regressions=affected,
                candidate_metrics=candidate_metrics,
                active_metrics=active_metrics,
            )
            promoted_page = await promote_learned_artifact(
                "page_template", str(scope["page_artifact_id"]), "1", request, context,
            )
            assert promoted_page["status"] == stage
            promoted_skill = await promote_skill_version(
                tenant_id=str(tenant_id),
                skill_id=str(scope["skill_id"]),
                version="1",
                actor="m11@test.local",
                evidence=evidence,
                idempotency_key=f"m11-skill-{stage}-{suffix}",
                focused_results=focused,
                affected_regressions=affected,
                candidate_metrics=candidate_metrics,
                active_metrics=active_metrics,
            )
            assert promoted_skill["status"] == stage

        async def embed(_query: str) -> list[float]:
            return [1.0, *([0.0] * 1023)]

        sys.modules["app.services.doc_index"] = types.SimpleNamespace(
            QWEN_DIMENSION=1024,
            QWEN_INSTRUCTION_VERSION="qwen3-m11-test",
            QWEN_MODEL_ID="qwen3-m11-test",
            embed_qwen_query=embed,
        )
        indexed = await index_site_skill_embedding(
            tenant_id=str(tenant_id),
            site_profile_id=str(site_profile_id),
            skill_id=str(scope["skill_id"]),
            version="1",
        )
        assert indexed["dimensions"] == 1024

        slug = await setup.fetchval("SELECT slug FROM ops_skill_library WHERE id=$1", scope["skill_id"])
        exact = await resolve_site_skill(
            tenant_id=str(tenant_id),
            site_profile_id=str(site_profile_id),
            query=slug,
            allow_llm_fallback=False,
        )
        vector = await resolve_site_skill(
            tenant_id=str(tenant_id),
            site_profile_id=str(site_profile_id),
            query="저렴한 사과를 찾아줘",
            allow_llm_fallback=False,
        )
        assert (exact["route"], vector["route"]) == ("exact", "qwen3_vector")

        reused = await auto_learn_site_visit(**kwargs)
        assert reused["state"] == "active_reused"
        assert reused["active_version"] == "1"

        invalidated = await auto_learn_site_visit(
            **{**kwargs, "aria_nodes": _nodes(include_searchbox=False)},
        )
        assert invalidated["state"] == "candidate_created"
        assert invalidated["version"] == "2"
        assert invalidated["active_preserved"] is True
        assert invalidated["human_gateway_required"] is True

        statuses = await setup.fetch(
            """SELECT version,status FROM browser_learned_artifact_versions
               WHERE artifact_id=$1 ORDER BY version""",
            scope["page_artifact_id"],
        )
        assert [(row["version"], row["status"]) for row in statuses] == [
            ("1", "active"),
            ("2", "candidate"),
        ]
    finally:
        if original_doc_index is None:
            sys.modules.pop("app.services.doc_index", None)
        else:
            sys.modules["app.services.doc_index"] = original_doc_index
        await setup.execute("DELETE FROM tenants WHERE id=$1", tenant_id)
        await setup.close()
        await db_pool.close_pool()
