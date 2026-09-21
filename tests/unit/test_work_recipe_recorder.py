from __future__ import annotations

from app.services.work_recipe import orchestrator
from app.services.work_recipe import recorder as recorder_module


async def test_recorder_replaces_credentials_before_saving(monkeypatch):
    saved = {}

    async def request_registration(recipe, **kwargs):
        saved["recipe"] = recipe
        saved.update(kwargs)
        return {"id": "registration-1", "status": "pending", "spec": recipe.to_dict()}

    monkeypatch.setattr(
        recorder_module.registration, "request_registration", request_registration
    )

    recording = recorder_module.start_recording(
        "example_login", "https://www.example.com/login", "tenant-1"
    )
    recording.record_step({"action": "navigate", "url": "https://example.com/login"})
    recording.record_step(
        {"action": "fill", "selector": "input[type=password]", "value": "raw-password"}
    )
    await recording.finish_recording()

    recipe = saved["recipe"]
    serialized = recipe.to_yaml()
    assert "raw-password" not in serialized
    assert recipe.steps[1].value == "{{credential_1}}"
    assert recipe.inputs[0].secret is True
    assert recipe.domain == "example.com"
    assert recipe.version == 1
    assert saved["tenant_id"] == "tenant-1"


def test_recorder_preserves_explicit_read_only_navigation_risk():
    recording = recorder_module.start_recording(
        "public_search", "https://www.example.com/search", "tenant-1"
    )

    step = recording.record_step(
        {
            "action": "navigate",
            "url": "https://www.example.com/search?q=vacuum",
            "risk": "READ",
        }
    )

    assert step is not None
    assert step.risk == "READ"


async def test_orchestrator_returns_none_without_candidate(monkeypatch):
    async def no_recipes(**kwargs):
        return []

    monkeypatch.setattr(orchestrator, "list_recipes", no_recipes)
    assert await orchestrator.resolve_recipe("처음 보는 사이트 작업", "tenant-1") is None


async def test_orchestrator_prefers_explicit_name_over_same_domain(monkeypatch):
    rows = [
        {
            "name": "orders_search",
            "domain": "example.com",
            "spec": {
                "name": "orders_search",
                "domain": "example.com",
                "steps": [{"action": "snapshot"}],
            },
        },
        {
            "name": "login_check",
            "domain": "example.com",
            "spec": {
                "name": "login_check",
                "domain": "example.com",
                "steps": [{"action": "snapshot"}],
            },
        },
    ]

    async def recipes(**kwargs):
        return rows

    monkeypatch.setattr(orchestrator, "list_recipes", recipes)
    resolved = await orchestrator.resolve_recipe(
        "login_check example.com 화면 확인", "tenant-1"
    )

    assert resolved is not None
    assert resolved.name == "login_check"


async def test_secret_input_requires_scoped_credential_reference():
    recipe = recorder_module.WorkRecipe(
        name="login",
        domain="example.com",
        inputs=[recorder_module.RecipeInput(name="password", secret=True)],
        steps=[recorder_module.RecipeStep(seq=1, action="snapshot")],
    )
    try:
        await orchestrator._scoped_inputs(recipe, {"password": "plain-secret"})
    except ValueError as exc:
        assert "credential_scope" in str(exc)
    else:
        raise AssertionError("plain secret input was accepted")


# ─────────────────────────────────────────────── ohvis_recipes 라우터 (AADS-WORKRECIPE-M2-RESCUE-R4)
#
# R3(runner-1ac68967)이 recorder 를 부르는 라우터 없이 push 되어 ORPHAN_ROUTER 로
# 잡혔다. 이 테스트는 라우터가 실제로 mount 되어 있고, 관리자 게이트 밖으로
# 새지 않는다는 것을 지킨다.


def _admin_ctx(*, admin: bool = True) -> dict:
    return {
        "tenant": {"id": "2d701a8c-9596-4757-8588-faa4f7837112"},
        "membership": {"user_id": "u-1", "role": "owner"},
        "user": {"email": "moong76@gmail.com", "is_internal_admin": admin},
    }


def test_ohvis_recipes_router_is_mounted():
    from app.main import app

    paths = {
        (method, getattr(route, "path", None))
        for route in app.routes
        for method in getattr(route, "methods", None) or ()
        if method not in ("HEAD", "OPTIONS")
    }
    assert ("POST", "/api/v1/ohvis/recipes/recording") in paths
    assert ("GET", "/api/v1/ohvis/recipes/recording/{recording_id}") in paths
    assert ("POST", "/api/v1/ohvis/recipes/recording/{recording_id}/steps") in paths
    assert ("POST", "/api/v1/ohvis/recipes/recording/{recording_id}/finish") in paths
    assert ("GET", "/api/v1/ohvis/recipes/registrations/{registration_id}") in paths
    assert ("POST", "/api/v1/ohvis/recipes/registrations/{registration_id}/decision") in paths


def test_ohvis_recipes_requires_internal_admin(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import ohvis_console, ohvis_recipes

    app = FastAPI()
    app.include_router(ohvis_recipes.router)
    app.dependency_overrides[ohvis_console.require_viewer] = lambda: _admin_ctx(admin=False)
    client = TestClient(app)

    response = client.post(
        "/ohvis/recipes/recording", json={"name": "login", "domain": "example.com"}
    )
    assert response.status_code == 403


async def test_ohvis_recipes_full_recording_cycle(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import ohvis_console, ohvis_recipes

    saved = {}

    async def request_registration(recipe, **kwargs):
        saved["recipe"] = recipe
        saved["requested_by"] = kwargs.get("requested_by")
        return {
            "id": "registration-1",
            "status": "pending",
            "dry_run": {"scope": "recipe_registration", "steps": [{"seq": 1}]},
            "spec": recipe.to_dict(),
        }

    monkeypatch.setattr(
        recorder_module.registration, "request_registration", request_registration
    )

    async def decide_registration(registration_id, **kwargs):
        assert registration_id == "registration-1"
        assert kwargs["tenant_id"] == _admin_ctx()["tenant"]["id"]
        assert kwargs["decision"] == "approve"
        return {
            "id": registration_id,
            "status": "approved",
            "recipe": {"id": "recipe-1", "version": 1},
        }

    monkeypatch.setattr(ohvis_recipes, "decide_registration", decide_registration)

    app = FastAPI()
    app.include_router(ohvis_recipes.router)
    app.dependency_overrides[ohvis_console.require_viewer] = lambda: _admin_ctx()
    client = TestClient(app)

    started = client.post(
        "/ohvis/recipes/recording", json={"name": "example_login", "domain": "https://example.com/login"}
    )
    assert started.status_code == 200
    recording_id = started.json()["recording_id"]

    stepped = client.post(
        f"/ohvis/recipes/recording/{recording_id}/steps",
        json={"payload": {"action": "navigate", "url": "https://example.com/login"}},
    )
    assert stepped.status_code == 200
    assert stepped.json()["step_count"] == 1

    finished = client.post(f"/ohvis/recipes/recording/{recording_id}/finish", json={})
    assert finished.status_code == 200
    assert finished.json()["status"] == "approval_required"
    assert finished.json()["registration"]["id"] == "registration-1"
    assert finished.json()["registration"]["dry_run"]["steps"] == [{"seq": 1}]
    assert saved["requested_by"] == "moong76@gmail.com"

    approved = client.post(
        "/ohvis/recipes/registrations/registration-1/decision",
        json={"decision": "approve", "reason": "dry-run 확인"},
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"
    assert approved.json()["registration"]["recipe"]["id"] == "recipe-1"

    # 종료된 recording_id 는 재사용할 수 없다
    stale = client.get(f"/ohvis/recipes/recording/{recording_id}")
    assert stale.status_code == 404
