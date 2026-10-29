from __future__ import annotations

import pytest

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
    from uuid import uuid4
    binding = {"credential_id": str(uuid4()), "field": "password"}
    recording.set_credential_scope({"credential_1": binding})
    recording.record_step(
        {"action": "fill", "selector": "input[type=password]", "value": "{{credential_1}}"}
    )
    await recording.finish_recording()

    recipe = saved["recipe"]
    serialized = recipe.to_yaml()
    assert "credential_scope" in serialized
    assert recipe.metadata["credential_scope"]["inputs"]["credential_1"] == binding
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
        "membership": {"user_id": "7fb5f50a-1111-4222-8333-444455556666", "role": "owner"},
        "user": {
            "user_id": "7fb5f50a-1111-4222-8333-444455556666",
            "email": "moong76@gmail.com",
            "is_internal_admin": admin,
        },
    }


def _viewer_override(context):
    async def override():
        return context
    return override


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
    assert ("GET", "/api/v1/ohvis/recipes/registrations") in paths
    assert ("GET", "/api/v1/ohvis/recipes/registrations/{registration_id}") in paths
    assert ("POST", "/api/v1/ohvis/recipes/registrations/{registration_id}/decision") in paths


async def test_ohvis_recipes_requires_internal_admin(monkeypatch):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from app.api import ohvis_console, ohvis_recipes

    app = FastAPI()
    app.include_router(ohvis_recipes.router)
    app.dependency_overrides[ohvis_console.require_viewer] = _viewer_override(_admin_ctx(admin=False))
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    response = await client.post(
        "/ohvis/recipes/recording", json={"name": "login", "domain": "example.com"}
    )
    assert response.status_code == 403


async def test_ohvis_recipes_full_recording_cycle(monkeypatch, recording_db):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

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
        assert kwargs["principal"]["user"]["email"] == "moong76@gmail.com"
        return {
            "id": registration_id,
            "status": "approved",
            "recipe": {"id": "recipe-1", "version": 1},
        }

    monkeypatch.setattr(ohvis_recipes, "decide_registration_as_human", decide_registration)

    app = FastAPI()
    app.include_router(ohvis_recipes.router)
    app.dependency_overrides[ohvis_console.require_viewer] = _viewer_override(_admin_ctx())
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    started = await client.post(
        "/ohvis/recipes/recording", json={"name": "example_login", "domain": "https://example.com/login"}
    )
    assert started.status_code == 200
    recording_id = started.json()["recording_id"]

    stepped = await client.post(
        f"/ohvis/recipes/recording/{recording_id}/steps",
        json={"payload": {"action": "navigate", "url": "https://example.com/login"}},
    )
    assert stepped.status_code == 200
    assert stepped.json()["step_count"] == 1

    finished = await client.post(f"/ohvis/recipes/recording/{recording_id}/finish", json={})
    assert finished.status_code == 200
    assert finished.json()["status"] == "approval_required"
    assert finished.json()["registration"]["id"] == "registration-1"
    assert finished.json()["registration"]["dry_run"]["steps"] == [{"seq": 1}]
    assert saved["requested_by"] == "moong76@gmail.com"

    approved = await client.post(
        "/ohvis/recipes/registrations/registration-1/decision",
        json={"decision": "approve", "reason": "dry-run 확인"},
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"
    assert approved.json()["registration"]["recipe"]["id"] == "recipe-1"

    # 종료된 recording_id 는 재사용할 수 없다
    stale = await client.get(f"/ohvis/recipes/recording/{recording_id}")
    assert stale.status_code == 404


async def test_ohvis_recipes_lists_pending_registrations_for_current_tenant(monkeypatch):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from app.api import ohvis_console, ohvis_recipes

    async def list_registrations(**kwargs):
        assert kwargs == {
            "tenant_id": _admin_ctx()["tenant"]["id"],
            "status": "pending",
        }
        return [
            {
                "id": "registration-1",
                "tenant_id": _admin_ctx()["tenant"]["id"],
                "name": "example_login",
                "domain": "example.com",
                "spec": {"steps": [{"action": "navigate"}]},
                "status": "pending",
                "requested_at": "2026-09-22T00:00:00+00:00",
            }
        ]

    monkeypatch.setattr(ohvis_recipes, "list_registrations", list_registrations)
    app = FastAPI()
    app.include_router(ohvis_recipes.router)
    app.dependency_overrides[ohvis_console.require_viewer] = _viewer_override(_admin_ctx())

    response = await AsyncClient(transport=ASGITransport(app=app), base_url="http://test").get("/ohvis/recipes/registrations")

    assert response.status_code == 200
    assert response.json()["registrations"][0]["id"] == "registration-1"
    assert response.json()["registrations"][0]["status"] == "pending"


async def test_ohvis_recipes_accepts_status_decision_contract(monkeypatch):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from app.api import ohvis_console, ohvis_recipes

    async def decide_registration(registration_id, **kwargs):
        assert registration_id == "registration-1"
        assert kwargs["decision"] == "approve"
        assert kwargs["reason"] == "검토 완료"
        return {"id": registration_id, "status": "approved"}

    monkeypatch.setattr(ohvis_recipes, "decide_registration_as_human", decide_registration)
    app = FastAPI()
    app.include_router(ohvis_recipes.router)
    app.dependency_overrides[ohvis_console.require_viewer] = _viewer_override(_admin_ctx())

    response = await AsyncClient(transport=ASGITransport(app=app), base_url="http://test").post(
        "/ohvis/recipes/registrations/registration-1/decision",
        json={"status": "approved", "reason": "검토 완료"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "approved"


@pytest.fixture
def recording_db(monkeypatch):
    """Exercise the DB adapter with serialized rows and transaction rollback."""
    from contextlib import asynccontextmanager
    from copy import deepcopy

    from app.services.work_recipe import recording_store

    rows = {}

    class Connection:
        @asynccontextmanager
        async def transaction(self):
            snapshot = deepcopy(rows)
            try:
                yield
            except Exception:
                rows.clear()
                rows.update(snapshot)
                raise

        async def fetchrow(self, query, rid, tenant):
            assert "tenant_id=$2" in query and "FOR UPDATE" in query
            state = rows.get((rid, tenant))
            return {"state": state} if state is not None else None

        async def execute(self, query, rid, tenant, *args):
            if query.startswith("DELETE"):
                rows.pop((rid, tenant))
            else:
                rows[(rid, tenant)] = args[0]

    class Pool:
        @asynccontextmanager
        async def acquire(self):
            yield Connection()

    monkeypatch.setattr(recording_store, "get_pool", lambda: Pool())
    return rows


@pytest.fixture(autouse=True)
def no_recording_notifications(monkeypatch):
    async def notify(*args, **kwargs):
        return False
    monkeypatch.setattr(recorder_module.approval_notifier, "notify_pending_created", notify)


async def test_pc_recording_survives_reconstruction(recording_db):
    from uuid import uuid4

    from fastapi import HTTPException

    from app.api import ohvis_recipes as api
    from app.services.work_recipe import recording_store
    tenant = str(uuid4())
    context = {"tenant": {"id": tenant}}
    started = await api.start_recording(api.RecordingStartIn(
        name="pc login", domain="example.com", lane="pc", browser_work_key="pc-work",
        credential_scope={"account": {"credential_id": str(uuid4()), "field": "username"}},
    ), context)
    rid = started["recording_id"]
    await api.record_recording_step(rid, api.RecordingStepIn(payload={
        "action": "fill", "selector": "#username", "value": "{{account}}",
    }), context)
    # No recorder object is retained: each request deserializes the DB row.
    async with recording_store.owned(rid, tenant) as restored:
        assert restored.flow_metadata["lane"] == "pc"
        assert restored.flow_metadata["browser_work_key"] == "pc-work"
        assert restored.flow_metadata["credential_scope"]["inputs"]["account"]["field"] == "username"
    await api.record_recording_step(rid, api.RecordingStepIn(payload={"action": "snapshot"}), context)
    assert (await api.get_recording_status(rid, context))["step_count"] == 2
    with pytest.raises(HTTPException) as exc:
        await api.get_recording_status(rid, {"tenant": {"id": str(uuid4())}})
    assert exc.value.status_code == 404


async def test_rejected_step_and_finish_preserve_state(recording_db):
    from uuid import uuid4

    from fastapi import HTTPException

    from app.api import ohvis_recipes as api
    context = {"tenant": {"id": str(uuid4())}}
    rid = (await api.start_recording(api.RecordingStartIn(name="login", domain="example.com"), context))["recording_id"]
    with pytest.raises(HTTPException):
        await api.finish_recording(rid, api.RecordingFinishIn(), context)
    assert (await api.get_recording_status(rid, context))["step_count"] == 0
    with pytest.raises(HTTPException):
        await api.record_recording_step(rid, api.RecordingStepIn(payload={
            "action": "fill", "selector": "#password", "value": uuid4().hex,
        }), context)
    assert (await api.get_recording_status(rid, context))["step_count"] == 0


def test_scope_validation_and_plaintext_rejection():
    from uuid import uuid4
    recorder = recorder_module
    recording = recorder.start_recording("login", "example.com")
    binding = {"credential_id": str(uuid4()), "field": "password"}
    recording.set_credential_scope({"password": binding})
    with pytest.raises(ValueError, match="plaintext"):
        recording.record_step({"action": "fill", "selector": "#password", "value": uuid4().hex})
    with pytest.raises(ValueError):
        recording.set_credential_scope({"password": {**binding, "value": uuid4().hex}})
    recording.record_step({"action": "fill", "selector": "#password", "value": "{{password}}"})
    with pytest.raises(ValueError, match="mismatch"):
        recording.set_credential_scope({"password": {**binding, "field": "username"}})


async def test_step_scope_is_saved_and_invalid_update_rolls_back(recording_db):
    from uuid import uuid4

    from fastapi import HTTPException

    from app.api import ohvis_recipes as api

    context = {"tenant": {"id": str(uuid4())}}
    rid = (await api.start_recording(
        api.RecordingStartIn(name="manual_login", domain="example.com", lane="pc"), context
    ))["recording_id"]
    binding = {"credential_id": str(uuid4()), "field": "password"}
    await api.record_recording_step(rid, api.RecordingStepIn(
        credential_scope={"account_password": binding},
        payload={"action": "fill", "selector": "#password", "value": "{{account_password}}"},
    ), context)
    before = await api.get_recording_status(rid, context)
    assert before["metadata"]["credential_scope"]["inputs"]["account_password"] == binding
    with pytest.raises(HTTPException) as exc:
        await api.record_recording_step(rid, api.RecordingStepIn(
            credential_scope={"account_password": {**binding, "field": "username"}},
            payload={"action": "snapshot"},
        ), context)
    assert exc.value.status_code == 400
    assert await api.get_recording_status(rid, context) == before


def test_bound_secret_is_rejected_outside_fill_value():
    from uuid import uuid4

    recording = recorder_module.start_recording("manual_login", "example.com")
    recording.set_credential_scope({
        "account_password": {"credential_id": str(uuid4()), "field": "password"},
    })
    with pytest.raises(ValueError, match="secret_input_unsupported_use"):
        recording.record_step({"action": "navigate", "url": "https://example.com/{{account_password}}"})
    assert recording.steps == []


def test_scope_rejects_invalid_variable_name():
    from uuid import uuid4

    recording = recorder_module.start_recording("manual_login", "example.com")
    with pytest.raises(ValueError):
        recording.set_credential_scope({
            "invalid variable": {"credential_id": str(uuid4()), "field": "username"},
        })
    assert recording.flow_metadata == {}


@pytest.mark.parametrize("operation", ["create", "owned"])
async def test_missing_recording_table_returns_service_unavailable(monkeypatch, operation):
    from contextlib import asynccontextmanager
    from asyncpg import UndefinedTableError
    from fastapi import HTTPException
    from app.services.work_recipe import recording_store

    class Connection:
        @asynccontextmanager
        async def transaction(self):
            yield

        async def execute(self, *args):
            raise UndefinedTableError("missing recording table")

        async def fetchrow(self, *args):
            raise UndefinedTableError("missing recording table")

    class Pool:
        @asynccontextmanager
        async def acquire(self):
            yield Connection()

    monkeypatch.setattr(recording_store, "get_pool", lambda: Pool())
    recording = recorder_module.start_recording("fixture", "example.com", "11111111-1111-1111-1111-111111111111")
    with pytest.raises(HTTPException) as error:
        if operation == "create":
            await recording_store.create("fixture-id", recording)
        else:
            async with recording_store.owned("fixture-id", "11111111-1111-1111-1111-111111111111"):
                pytest.fail("missing table must not yield a recording")
    assert error.value.status_code == 503
    assert error.value.detail == "recording_store_unavailable"


async def test_recording_schema_ensure_uses_idempotent_migration(monkeypatch):
    from contextlib import asynccontextmanager
    from app.services.work_recipe import recording_store

    statements = []

    class Connection:
        async def execute(self, sql):
            statements.append(sql)

    class Pool:
        @asynccontextmanager
        async def acquire(self):
            yield Connection()

    monkeypatch.setattr(recording_store, "get_pool", lambda: Pool())
    await recording_store.ensure_recording_schema()
    assert len(statements) == 1
    assert "CREATE TABLE IF NOT EXISTS work_recipe_recordings" in statements[0]
    assert "CREATE INDEX IF NOT EXISTS work_recipe_recordings_tenant_idx" in statements[0]
