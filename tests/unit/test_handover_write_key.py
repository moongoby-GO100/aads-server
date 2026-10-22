"""핸드오버 쓰기 전용 서비스 키(x-handover-key) 경계 테스트.

허용 범위는 (POST, /api/v1/handovers) 정확히 하나다. 실제 app(미들웨어 포함)을 ASGI 로 돌린다.
키 값은 테스트 안에서 만든 더미이며 실제 키가 아니다.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path

os.environ.setdefault("JWT_SECRET_KEY", "unit-test-secret-key-that-is-long-enough")

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

import app.auth as auth_module
import app.main as main_module
from app.api import handovers as handovers_api

ROOT = Path(__file__).resolve().parents[2]
WRITE_KEY = "dummy-handover-write-key-for-unit-test"
OLD_KEY = "dummy-previous-handover-write-key"
MONITOR_KEY = "dummy-monitor-key-for-unit-test"
HANDOVERS = "/api/v1/handovers"
BODY = {"project_key": "AADS", "title": "t", "body": "b", "entry_key": "runner:runner-abc12345"}


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class _Conn:
    async def fetchrow(self, *_a, **_k):
        return {"id": "11111111-1111-1111-1111-111111111111", "slug": "internal", "name": "Internal",
                "kind": "internal", "status": "active"}


class _Pool:
    @asynccontextmanager
    async def acquire(self):
        yield _Conn()


@pytest.fixture
def client(monkeypatch):
    calls = {"upsert": [], "list": 0}

    async def fake_pool():
        return _Pool()

    async def fake_upsert(**kwargs):
        calls["upsert"].append(kwargs)
        return {"id": "e1", "revision": 1}, True

    async def fake_list(**_kwargs):
        calls["list"] += 1
        return [], 0

    monkeypatch.setattr(auth_module, "_ensure_pool", fake_pool)
    monkeypatch.setattr(handovers_api, "upsert_handover_entry", fake_upsert)
    monkeypatch.setattr(handovers_api, "list_handover_entries", fake_list)
    monkeypatch.setenv("AADS_HANDOVER_WRITE_KEY_SHA256", _sha(WRITE_KEY))
    monkeypatch.setenv("AADS_MONITOR_KEY", MONITOR_KEY)
    test_client = TestClient(main_module.app, raise_server_exceptions=False)
    test_client.calls = calls
    return test_client


def _hk(key=WRITE_KEY):
    return {"x-handover-key": key}


def test_valid_key_posts_handover(client):
    response = client.post(HANDOVERS, json=BODY, headers=_hk())
    assert response.status_code == 201, response.text
    assert len(client.calls["upsert"]) == 1
    assert client.calls["upsert"][0]["changed_by"] == "handover-writer@aads.internal"


def test_valid_key_cannot_read_import_or_reach_admin_routes(client):
    for method, path, kwargs in (
        ("get", HANDOVERS, {}),
        ("get", f"{HANDOVERS}/export?project_key=AADS", {}),
        ("get", f"{HANDOVERS}/some-id", {}),
        ("post", f"{HANDOVERS}/import", {"json": {"project_key": "AADS", "content": "x"}}),
        ("put", HANDOVERS, {"json": BODY}),
        ("delete", HANDOVERS, {}),
        ("post", "/api/v1/ohvis/llmops/ingest-clients", {"json": {"client_id": "c", "project": "p"}}),
        ("get", "/api/v1/ops/runner-host-policy", {}),
        ("post", f"{HANDOVERS}/", {"json": BODY}),
    ):
        response = getattr(client, method)(path, headers=_hk(), follow_redirects=False, **kwargs)
        assert response.status_code in (401, 403), (method, path, response.status_code)
    assert client.calls["upsert"] == [] and client.calls["list"] == 0


@pytest.mark.parametrize("key", ["wrong-key", WRITE_KEY + "x", "   "])
def test_wrong_key_is_401(client, key):
    response = client.post(HANDOVERS, json=BODY, headers=_hk(key))
    assert response.status_code == 401, response.text
    assert client.calls["upsert"] == []


@pytest.mark.parametrize("value", ["", "   ", "not-a-hash", WRITE_KEY, _sha(WRITE_KEY)[:63]])
def test_empty_or_malformed_hash_env_disables_feature(client, monkeypatch, value):
    monkeypatch.setenv("AADS_HANDOVER_WRITE_KEY_SHA256", value)
    response = client.post(HANDOVERS, json=BODY, headers=_hk())
    assert response.status_code == 401, response.text
    monkeypatch.delenv("AADS_HANDOVER_WRITE_KEY_SHA256")
    assert client.post(HANDOVERS, json=BODY, headers=_hk()).status_code == 401
    assert client.calls["upsert"] == []


def test_comma_separated_hashes_accept_both_keys_during_rotation(client, monkeypatch):
    monkeypatch.setenv("AADS_HANDOVER_WRITE_KEY_SHA256", f"{_sha(OLD_KEY)}, {_sha(WRITE_KEY).upper()}")
    assert client.post(HANDOVERS, json=BODY, headers=_hk(OLD_KEY)).status_code == 201
    assert client.post(HANDOVERS, json=BODY, headers=_hk(WRITE_KEY)).status_code == 201
    assert client.post(HANDOVERS, json=BODY, headers=_hk("third")).status_code == 401


def test_no_credentials_at_all_is_401(client):
    assert client.post(HANDOVERS, json=BODY).status_code == 401


def test_monitor_key_path_is_unchanged(client):
    response = client.post(HANDOVERS, json=BODY, headers={"x-monitor-key": MONITOR_KEY})
    assert response.status_code == 201, response.text
    assert client.calls["upsert"][0]["changed_by"] == "system@aads.internal"
    assert client.get(HANDOVERS, headers={"x-monitor-key": MONITOR_KEY}).status_code == 200
    assert client.post(HANDOVERS, json=BODY, headers={"x-monitor-key": "wrong"}).status_code == 401


def test_handover_key_does_not_fall_back_to_monitor_key(client):
    headers = {"x-handover-key": "wrong", "x-monitor-key": MONITOR_KEY}
    assert client.post(HANDOVERS, json=BODY, headers=headers).status_code == 401
    assert client.get(HANDOVERS, headers=headers).status_code == 401


def _request(method: str, path: str, headers: dict[str, str]) -> Request:
    return Request({
        "type": "http", "method": method, "path": path, "query_string": b"",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
    })


def _principal(method: str, path: str, key: str = WRITE_KEY):
    request = _request(method, path, {"x-handover-key": key})
    return auth_module.get_current_user(request, authorization=None, x_tenant_id=None,
                                        x_monitor_key=None, x_handover_key=key)


@pytest.mark.asyncio
async def test_principal_is_non_admin_internal_member(client):
    user = await _principal("POST", HANDOVERS)
    assert user["user_id"] == "system:handover-writer"
    assert user["is_admin"] is False and user["is_internal_admin"] is False
    assert user["tenant_role"] == "member"
    assert user["current_tenant"]["slug"] == "internal"
    context = await auth_module.require_tenant_role(auth_module.TenantRole.MEMBER)(
        await auth_module.get_current_tenant_context(user))
    assert context["membership"]["role"] == "member"
    with pytest.raises(HTTPException) as denied:
        await auth_module.require_tenant_role(auth_module.TenantRole.ADMIN)(
            await auth_module.get_current_tenant_context(user))
    assert denied.value.status_code == 403
    with pytest.raises(HTTPException) as not_internal_admin:
        await auth_module.require_internal_admin(user)
    assert not_internal_admin.value.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path", [
    ("GET", HANDOVERS), ("POST", f"{HANDOVERS}/import"), ("POST", f"{HANDOVERS}/"),
    ("POST", "/api/v1/handovers/x"), ("PUT", HANDOVERS), ("POST", "/api/v1/ops/runner-host-policy"),
    ("POST", "/api/v1/pipeline/jobs"),
])
async def test_dependency_rejects_header_on_any_other_route(client, method, path):
    with pytest.raises(HTTPException) as err:
        await _principal(method, path)
    assert err.value.status_code == 401


def test_middleware_exemption_is_exact_method_and_path():
    assert auth_module.is_handover_write_request("POST", HANDOVERS)
    for method, path in (("GET", HANDOVERS), ("POST", f"{HANDOVERS}/import"), ("POST", f"{HANDOVERS}/"),
                         ("POST", "/api/v1/handovers/export"), ("POST", "/api/v1/handoversX")):
        assert not auth_module.is_handover_write_request(method, path), (method, path)


def test_feature_is_off_by_default(monkeypatch):
    monkeypatch.delenv("AADS_HANDOVER_WRITE_KEY_SHA256", raising=False)
    assert auth_module.handover_write_key_matches(WRITE_KEY) is False


# ── 러너 보조 스크립트: 헤더 선택 순서 ─────────────────────────────────────
def _helper():
    spec = importlib.util.spec_from_file_location("runner_handover_write", ROOT / "scripts" / "runner_handover_write.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Resp:
    status = 201

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def read(self):
        return json.dumps({"entry": {"id": "e", "revision": 2}, "changed": True}).encode()


PAYLOAD = {"project": "AADS", "job_id": "runner-abc12345", "stage": "awaiting_approval", "tenant_id": "t",
           "job": {"instruction": "TITLE: x", "changed_files": [], "result_output": ""}}


def _send(monkeypatch, **env):
    for name in ("AADS_API_URL", "AADS_HANDOVER_WRITE_KEY", "AADS_MONITOR_KEY"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    seen = []

    def opener(req, timeout):
        seen.append({k.lower(): v for k, v in req.header_items()})
        return _Resp()

    result = _helper().http_fallback_write(PAYLOAD, opener=opener)
    return result, seen


def test_runner_prefers_write_key_over_monitor_key(monkeypatch):
    result, seen = _send(monkeypatch, AADS_API_URL="http://x", AADS_HANDOVER_WRITE_KEY=WRITE_KEY,
                         AADS_MONITOR_KEY=MONITOR_KEY)
    assert result["ok"] is True
    assert seen[0]["x-handover-key"] == WRITE_KEY and "x-monitor-key" not in seen[0]


def test_runner_falls_back_to_monitor_key(monkeypatch):
    result, seen = _send(monkeypatch, AADS_API_URL="http://x", AADS_MONITOR_KEY=MONITOR_KEY)
    assert result["ok"] is True
    assert seen[0]["x-monitor-key"] == MONITOR_KEY and "x-handover-key" not in seen[0]


@pytest.mark.parametrize("env", [{"AADS_API_URL": "http://x"}, {"AADS_API_URL": "http://x", "AADS_HANDOVER_WRITE_KEY": "  "}])
def test_runner_without_any_key_is_unconfigured_and_never_sends(monkeypatch, env):
    result, seen = _send(monkeypatch, **env)
    assert result["ok"] is False and result["error"].startswith("http_fallback_unconfigured")
    assert seen == []


def test_runner_error_output_never_contains_key(monkeypatch):
    for name in ("AADS_HANDOVER_WRITE_KEY", "AADS_MONITOR_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AADS_API_URL", "http://x")
    monkeypatch.setenv("AADS_HANDOVER_WRITE_KEY", WRITE_KEY)

    def boom(req, timeout):
        raise RuntimeError("connection failed")

    result = _helper().http_fallback_write(PAYLOAD, opener=boom)
    assert result["ok"] is False and WRITE_KEY not in json.dumps(result)
