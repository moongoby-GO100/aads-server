from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

asyncpg = pytest.importorskip("asyncpg")

from app.core import db_pool
from app.services.smart_browser_learning import (
    SmartBrowserLearningError,
    auto_learn_site_visit,
)


def _url() -> str:
    value = os.getenv("SMART_BROWSER_M8_TEST_DATABASE_URL", "")
    if not value:
        pytest.skip("SMART_BROWSER_M8_TEST_DATABASE_URL is required")
    if "smartbrowser_m8_" not in value:
        pytest.fail("SMART_BROWSER_M8_TEST_DATABASE_URL must target a disposable smartbrowser_m8_* database")
    return value


def _nodes() -> list[dict[str, str]]:
    return [
        {"role": "searchbox", "name": "Search products", "landmark": "search"},
        {"role": "button", "name": "Search", "landmark": "search"},
    ]


def _template() -> dict[str, object]:
    return {
        "page_type": "search-results",
        "required_anchors": [{"role": "searchbox", "name": "Search products"}],
        "stable_names": ["Search products", "Search"],
        "reuse_threshold": 0.8,
    }


@pytest.mark.asyncio
async def test_concurrent_first_visit_is_single_candidate_and_tenant_isolated(monkeypatch):
    database_url = _url()
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("DB_POOL_MIN_SIZE", "1")
    monkeypatch.setenv("DB_POOL_MAX_SIZE", "5")
    db_pool._pool = None
    await db_pool.init_pool()

    tenant_a = uuid4()
    tenant_b = uuid4()
    site = uuid4()
    suffix = uuid4().hex
    setup = await asyncpg.connect(database_url)
    try:
        await setup.executemany(
            "INSERT INTO tenants(id,slug,name) VALUES($1,$2,$3)",
            [(tenant_a, f"m8-a-{suffix}", "M8 A"), (tenant_b, f"m8-b-{suffix}", "M8 B")],
        )
        await setup.execute(
            """INSERT INTO authenticated_site_profiles
               (id,tenant_id,project_key,site_key,display_name,base_origin,allowed_origins,runtime)
               VALUES($1,$2,'AADS',$3,'M8 Site','https://shop.example',
                      '[\"https://shop.example\"]'::jsonb,'playwright_server')""",
            site, tenant_a, f"m8-site-{suffix}",
        )

        kwargs = {
            "tenant_id": str(tenant_a),
            "site_profile_id": str(site),
            "page_key": "search",
            "area_key": "search",
            "aria_nodes": _nodes(),
            "template_contract": _template(),
            "evidence": [f"object://evidence/{suffix}"],
            "expires_at": datetime.now(UTC) + timedelta(hours=1),
        }
        results = await asyncio.gather(
            auto_learn_site_visit(**kwargs),
            auto_learn_site_visit(**kwargs),
        )
        assert {item["reason_code"] for item in results} == {
            "first_visit_candidates_created",
            "candidate_already_exists",
        }

        counts = await setup.fetchrow(
            """SELECT
                 (SELECT count(*) FROM browser_learned_artifact_versions v
                    JOIN browser_learned_artifacts a ON a.id=v.artifact_id
                   WHERE a.tenant_id=$1) AS template_versions,
                 (SELECT count(*) FROM ops_skill_versions v
                    JOIN ops_skill_library l ON l.id=v.skill_id
                   WHERE l.tenant_id=$1) AS skill_versions,
                 (SELECT next_version FROM browser_site_learning_scopes
                   WHERE tenant_id=$1 AND site_profile_id=$2 AND page_key='search') AS next_version""",
            tenant_a, site,
        )
        assert dict(counts) == {
            "template_versions": 1,
            "skill_versions": 1,
            "next_version": 2,
        }

        with pytest.raises(SmartBrowserLearningError, match="site_profile_not_found"):
            await auto_learn_site_visit(**{**kwargs, "tenant_id": str(tenant_b)})
    finally:
        await setup.execute("DELETE FROM tenants WHERE id=ANY($1::uuid[])", [tenant_a, tenant_b])
        await setup.close()
        await db_pool.close_pool()
