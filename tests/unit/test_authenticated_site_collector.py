import importlib

import pytest


@pytest.fixture()
def collector_modules(tmp_path, monkeypatch):
    monkeypatch.setenv("AADS_PC_AGENT_COLLECTION_QUEUE_PATH", str(tmp_path / "queue.json"))
    monkeypatch.setenv("AADS_AUTHENTICATED_SITE_PROFILES_PATH", str(tmp_path / "site_profiles.json"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("YEOLJEONG_FINANCE_DATABASE_URL", raising=False)

    import app.services.pc_agent_collection_queue as queue_module
    import app.services.authenticated_site_collector as collector

    queue_module = importlib.reload(queue_module)
    collector = importlib.reload(collector)
    return collector, queue_module


async def test_collector_overview_uses_project_defaults_without_database(collector_modules):
    collector, _queue_module = collector_modules

    overview = await collector.collector_overview(tenant_id="00000000-0000-0000-0000-000000000001")

    assert overview["demo"] is True
    assert overview["totals"]["connected_sites"] >= 6
    assert {item["project_key"] for item in overview["projects"]} >= {"AADS", "KIS", "GO100", "SF", "NTV2", "NAS"}


async def test_site_profile_upsert_is_available_without_database(collector_modules):
    collector, _queue_module = collector_modules

    profile = await collector.upsert_site_profile(
        tenant_id="00000000-0000-0000-0000-000000000001",
        user_id="ceo",
        payload={
            "project_key": "NTV2",
            "site_key": "newtalk.seller",
            "display_name": "NewTalk seller portal",
            "base_origin": "https://seller.newtalk.kr/login",
            "runtime": "webview2",
            "data_categories": ["settlement", "orders"],
        },
    )
    profiles = await collector.list_site_profiles(
        tenant_id="00000000-0000-0000-0000-000000000001",
        project_key="NTV2",
    )

    assert profile["base_origin"] == "https://seller.newtalk.kr"
    assert profiles["demo"] is False
    assert profiles["sites"][0]["site_key"] == "newtalk.seller"


async def test_create_and_resume_collector_job_preserves_work_key(collector_modules):
    collector, _queue_module = collector_modules
    tenant_id = "00000000-0000-0000-0000-000000000001"

    await collector.upsert_site_profile(
        tenant_id=tenant_id,
        user_id="ceo",
        payload={
            "project_key": "SF",
            "site_key": "sf.creator",
            "display_name": "ShortFlow creator",
            "base_origin": "https://studio.example/login",
            "runtime": "chrome_extension",
            "data_categories": ["uploads"],
        },
    )
    created = await collector.create_collection_job(
        tenant_id=tenant_id,
        user_id="ceo",
        payload={
            "project_key": "SF",
            "site_key": "sf.creator",
            "recipe_id": "sf.creator.collect",
            "work_key": "sf creator local",
        },
    )
    job = created["job"]
    resumed = collector.resume_collection_job(
        job_id=job["id"],
        resolution="completed",
        note="OTP entered by user",
    )

    assert created["status"] == "created"
    assert job["work_key"] == "sf-creator-local"
    assert resumed is not None
    assert resumed["same_work_key"] is True
    assert resumed["job"]["work_key"] == "sf-creator-local"
    assert collector.list_jobs(project_key="SF")["count"] == 1


def test_collector_recipe_dry_run_maps_webview2_to_pc_agent(collector_modules):
    collector, _queue_module = collector_modules

    plan = collector.build_collector_recipe_dry_run(
        {
            "recipe_id": "ntv2.partner.collect",
            "version": "v1",
            "allowed_origins": ["https://newtalk.kr"],
            "work_key_template": "ntv2 partner",
        },
        target_url="https://newtalk.kr/partners",
        project_key="NTV2",
        site_environment="webview2",
    )

    assert plan["runtime"] == "pc_agent"
    assert plan["saas_extension"]["project_key"] == "NTV2"
    assert plan["saas_extension"]["site_environment"] == "webview2"


def test_collector_api_overview_uses_tenant_context(collector_modules):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import app.api.authenticated_site_collector as api_module

    app = FastAPI()
    app.include_router(api_module.router)
    app.dependency_overrides[api_module.require_viewer] = lambda: {
        "tenant": {"id": "00000000-0000-0000-0000-000000000001"},
        "membership": {"user_id": "ceo"},
    }

    response = TestClient(app).get("/authenticated-site-collector/overview")

    assert response.status_code == 200
    assert response.json()["totals"]["connected_sites"] >= 6
