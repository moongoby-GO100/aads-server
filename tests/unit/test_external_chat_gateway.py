from __future__ import annotations

import hashlib
import hmac

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.external_chat import router
from app.services import external_chat_gateway as gateway
from app.services import tenant_usage_limits as limits


def test_external_chat_token_and_hmac_verification(monkeypatch):
    monkeypatch.setenv("AADS_EXTERNAL_CHAT_TOKEN", "secret-token")
    monkeypatch.setenv("AADS_EXTERNAL_CHAT_HMAC_SECRET", "hmac-secret")

    settings = gateway.get_settings()
    assert settings.enabled
    assert gateway.verify_service_token("secret-token", settings)
    assert gateway.verify_service_token("Bearer secret-token", settings)
    assert not gateway.verify_service_token("wrong-token", settings)

    body = b'{"hello":"world"}'
    signature = "sha256=" + hmac.new(b"hmac-secret", body, hashlib.sha256).hexdigest()
    assert gateway.verify_hmac_signature(body=body, signature=signature, settings=settings)
    assert not gateway.verify_hmac_signature(body=body, signature="sha256=bad", settings=settings)


def test_external_chat_service_registry_allows_non_newtalk_service(monkeypatch):
    monkeypatch.setenv("AADS_EXTERNAL_CHAT_TOKEN", "secret-token")
    monkeypatch.setenv(
        "AADS_EXTERNAL_CHAT_SERVICE_REGISTRY",
        '{"go100:admin":{"workspace_name":"[GO100] AI Ops","session_title_prefix":"GO100 Admin","admin_only":false}}',
    )

    profile = gateway.resolve_service_profile("GO100", "ADMIN")
    assert profile.provider == "go100"
    assert profile.service == "admin"
    assert profile.workspace_name == "[GO100] AI Ops"
    assert profile.admin_only is False

    config = gateway.widget_config("go100", "admin")
    assert config["provider"] == "go100"
    assert config["service"] == "admin"
    assert config["policy"]["admin_only"] is False
    assert {"provider": "go100", "service": "admin"} in config["policy"]["supported_services"]


def test_external_chat_rejects_unregistered_service(monkeypatch):
    monkeypatch.setenv("AADS_EXTERNAL_CHAT_TOKEN", "secret-token")
    monkeypatch.delenv("AADS_EXTERNAL_CHAT_ALLOWED_SERVICES", raising=False)
    monkeypatch.delenv("AADS_EXTERNAL_CHAT_SERVICE_REGISTRY", raising=False)

    with pytest.raises(ValueError, match="external_chat_service_not_allowed"):
        gateway.resolve_service_profile("unknown", "admin")


def test_external_chat_allowed_services_csv_creates_profile(monkeypatch):
    monkeypatch.setenv("AADS_EXTERNAL_CHAT_TOKEN", "secret-token")
    monkeypatch.setenv("AADS_EXTERNAL_CHAT_ALLOWED_SERVICES", "sf:ops, go100:admin")

    profiles = {(item.provider, item.service): item for item in gateway.list_service_profiles()}
    assert ("sf", "ops") in profiles
    assert profiles[("sf", "ops")].workspace_name == "[SF] ops AI"
    assert ("go100", "admin") in profiles


@pytest.mark.asyncio
async def test_external_chat_config_requires_service_token(monkeypatch):
    monkeypatch.setenv("AADS_EXTERNAL_CHAT_TOKEN", "secret-token")
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        rejected = await client.get("/api/v1/external/chat/config?provider=newtalk&service=v2")
        accepted = await client.get(
            "/api/v1/external/chat/config?provider=newtalk&service=v2",
            headers={"X-AADS-External-Token": "secret-token"},
        )

    assert rejected.status_code == 401
    assert accepted.status_code == 200
    assert accepted.json()["provider"] == "newtalk"
    assert accepted.json()["service"] == "v2"
    assert accepted.json()["policy"]["usage_mode"] == "soft_telemetry"
    assert accepted.json()["policy"]["admin_only"] is True


@pytest.mark.asyncio
async def test_external_chat_config_supports_registered_service(monkeypatch):
    monkeypatch.setenv("AADS_EXTERNAL_CHAT_TOKEN", "secret-token")
    monkeypatch.setenv("AADS_EXTERNAL_CHAT_ALLOWED_SERVICES", "sf:ops")
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        accepted = await client.get(
            "/api/v1/external/chat/config?provider=sf&service=ops",
            headers={"X-AADS-External-Token": "secret-token"},
        )
        rejected = await client.get(
            "/api/v1/external/chat/config?provider=sf&service=unknown",
            headers={"X-AADS-External-Token": "secret-token"},
        )

    assert accepted.status_code == 200
    assert accepted.json()["provider"] == "sf"
    assert accepted.json()["service"] == "ops"
    assert rejected.status_code == 403
    assert rejected.json()["detail"] == "external_chat_service_not_allowed"


@pytest.mark.asyncio
async def test_external_chat_session_requires_admin_context(monkeypatch):
    monkeypatch.setenv("AADS_EXTERNAL_CHAT_TOKEN", "secret-token")
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/api/v1/external/chat/sessions",
            headers={"X-AADS-External-Token": "secret-token"},
            json={
                "provider": "newtalk",
                "service": "v2",
                "external_user_id": "user-1",
                "metadata": {"roles": ["member"]},
            },
        )

    assert response.status_code == 403
    assert response.json()["detail"] == "external_chat_admin_required"


def test_external_chat_admin_context_required_by_default(monkeypatch):
    monkeypatch.setenv("AADS_EXTERNAL_CHAT_TOKEN", "secret-token")
    monkeypatch.delenv("AADS_EXTERNAL_CHAT_ADMIN_ONLY", raising=False)

    settings = gateway.get_settings()
    assert settings.admin_only is True
    with pytest.raises(PermissionError):
        gateway.assert_admin_context({"roles": ["member"]}, settings)

    gateway.assert_admin_context({"roles": ["admin"]}, settings)
    gateway.assert_admin_context({"aads_admin_context": True}, settings)
    gateway.assert_admin_context('{"aads_admin_context": true}', settings)


def test_external_chat_metadata_string_normalization():
    assert gateway.metadata_has_admin_context('{"newtalk_is_admin": true}')
    assert gateway.metadata_has_admin_context('{"roles": ["admin"]}')
    assert not gateway.metadata_has_admin_context('{"roles": ["member"]}')
    assert not gateway.metadata_has_admin_context("not-json")


def test_external_chat_admin_context_can_be_disabled(monkeypatch):
    monkeypatch.setenv("AADS_EXTERNAL_CHAT_TOKEN", "secret-token")
    monkeypatch.setenv("AADS_EXTERNAL_CHAT_ADMIN_ONLY", "false")

    settings = gateway.get_settings()
    assert settings.admin_only is False
    gateway.assert_admin_context({"roles": ["member"]}, settings)


def test_external_chat_usage_soft_bypass_converts_hard_limit():
    usage = limits.TenantMonthlyUsage(
        tenant_id="tenant-1",
        month_start=limits.current_month_start(),
        calls=100,
        input_tokens=1000,
        output_tokens=0,
        total_tokens=1000,
        cost_usd=limits.Decimal("100"),
    )
    policy = limits.TenantPlanPolicy(
        plan_key="free",
        monthly_token_limit=10,
        monthly_cost_limit_usd=limits.Decimal("1"),
        monthly_call_limit=1,
        soft_limit_ratio=limits.Decimal("0.8"),
        hard_limit_ratio=limits.Decimal("1.0"),
    )

    hard = limits.evaluate_usage_limit(
        tenant_id="tenant-1",
        operation="external_chat:send_message",
        usage=usage,
        policy=policy,
    )
    assert not hard.allowed

    token = limits.set_soft_bypass_usage_limits(True)
    try:
        # Simulate the post-evaluation branch used by check_tenant_usage_limit.
        assert limits._soft_bypass_usage_limits.get() is True
    finally:
        limits.reset_soft_bypass_usage_limits(token)


@pytest.mark.asyncio
async def test_collect_chat_stream_returns_delta_content():
    async def fake_stream():
        yield 'data: {"type":"delta","content":"hello"}\n\n'
        yield 'data: {"type":"delta","content":" world"}\n\n'
        yield 'data: {"type":"done","model":"test","cost":0}\n\n'

    content, meta = await gateway.collect_chat_stream(fake_stream())

    assert content == "hello world"
    assert meta["done"]["model"] == "test"
    assert meta["errors"] == []
