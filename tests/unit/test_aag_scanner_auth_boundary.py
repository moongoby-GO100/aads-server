"""AAG scanner/runner routes must reach their own credential check through the JWT middleware.

The v2 runner-brief and snapshot-publish routes authenticate an opaque
``X-AAG-Scanner-Token`` inside the route.  The global JWT middleware used to answer
``401 Bearer 토큰을 제공하세요`` first, so a correct credential never got a verdict.
These tests drive the real ``app`` (middleware included) through ASGI.
"""
from __future__ import annotations

import os

os.environ.setdefault("JWT_SECRET_KEY", "unit-test-secret-key-that-is-long-enough")

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.api import aag as aag_api
from app.services.aag_governance import AAGAuthorizationError, ScannerPrincipal

GOOD_TOKEN = "good-scanner-token-0123456789"
RUNNER_BRIEF = "/api/v1/aag/v2/runner-brief"
SNAPSHOTS = "/api/v1/aag/v2/snapshots"
BODY = {"project": "go100", "target": "app/api/aag.py", "repository_id": "r", "target_ref": "refs/heads/main"}


@pytest.fixture
def client(monkeypatch):
    calls = []

    async def fake_authenticate(token, project):
        calls.append((token, project))
        if token == GOOD_TOKEN and str(project).upper() == "GO100":
            return ScannerPrincipal("cred-1", "GO100", "scanner:cred-1")
        if token == "expired-or-revoked-token-0123":
            raise AAGAuthorizationError("scanner credential is not valid for project")
        if token == GOOD_TOKEN:
            raise AAGAuthorizationError("scanner credential is not valid for project")
        raise AAGAuthorizationError("scanner credential required")

    async def no_snapshot(**_kwargs):
        return None

    async def record(**_kwargs):
        return None

    monkeypatch.setenv("AAG_V2_ENABLED", "1")
    monkeypatch.setattr(aag_api, "authenticate_scanner", fake_authenticate)
    monkeypatch.setattr(aag_api, "load_authoritative_snapshot", no_snapshot)
    monkeypatch.setattr(aag_api, "record_consumer_event", record)
    test_client = TestClient(main_module.app, raise_server_exceptions=False)
    test_client.auth_calls = calls
    return test_client


def _post(client, path, token=None, body=None):
    headers = {"X-AAG-Scanner-Token": token} if token is not None else {}
    return client.post(path, json=body or BODY, headers=headers)


def test_valid_scanner_token_reaches_route_authentication(client):
    response = _post(client, RUNNER_BRIEF, GOOD_TOKEN)
    assert response.status_code == 404, response.text
    assert "Bearer" not in response.text
    assert client.auth_calls == [(GOOD_TOKEN, "GO100")]


@pytest.mark.parametrize("token", [None, "", "short", "expired-or-revoked-token-0123", "unknown-token-0123456789abc"])
def test_missing_wrong_expired_or_revoked_token_is_403_from_the_route(client, token):
    response = _post(client, RUNNER_BRIEF, token)
    assert response.status_code == 403, response.text
    assert "Bearer" not in response.text


def test_token_for_another_project_is_403(client):
    response = _post(client, RUNNER_BRIEF, GOOD_TOKEN, {**BODY, "project": "kis"})
    assert response.status_code == 403
    assert client.auth_calls[-1] == (GOOD_TOKEN, "KIS")


def test_snapshot_publish_route_also_reaches_scanner_authentication(client):
    response = client.post(SNAPSHOTS, json={}, headers={"X-AAG-Scanner-Token": GOOD_TOKEN})
    assert response.status_code == 422, "body validation means the middleware let the request through"
    assert "Bearer" not in response.text


@pytest.mark.parametrize(("method", "path"), [
    ("post", RUNNER_BRIEF + "/"),
    ("post", RUNNER_BRIEF + "/extra"),
    ("post", RUNNER_BRIEF + "x"),
    ("post", "/api/v1/aag/v2/runner-brief/../findings"),
    ("get", RUNNER_BRIEF),
    ("put", RUNNER_BRIEF),
    ("get", SNAPSHOTS),
    ("post", "/api/v1/aag/snapshot"),
    ("get", "/api/v1/aag/v2/findings?project=go100"),
    ("get", "/api/v1/aag/v2/latest?project=go100&repository_id=r&target_ref=refs/heads/main"),
    ("get", "/api/v1/aag/v2/history"),
    ("get", "/api/v1/aag/v2/brief?project=go100"),
    ("post", "/api/v1/aag/v2/exceptions"),
    ("get", "/api/v1/aag/findings"),
    ("get", "/api/v1/aag/projects"),
])
def test_everything_else_under_aag_still_requires_jwt(client, method, path):
    response = getattr(client, method)(path, headers={"X-AAG-Scanner-Token": GOOD_TOKEN})
    assert response.status_code == 401, (method, path, response.text)
    assert client.auth_calls == []


def test_scanner_header_does_not_substitute_for_jwt_elsewhere(client):
    response = client.get("/api/v1/aag/v2/findings?project=go100", headers={"X-AAG-Scanner-Token": GOOD_TOKEN})
    assert response.status_code == 401
    forged = client.get("/api/v1/aag/v2/findings?project=go100", headers={"Authorization": "Bearer forged"})
    assert forged.status_code == 401


def test_exemption_set_is_exact_post_pairs_without_prefix():
    assert main_module._SERVICE_AUTH_EXACT_ROUTES == {
        ("POST", RUNNER_BRIEF),
        ("POST", SNAPSHOTS),
    }
    assert not any(p.startswith("/api/v1/aag") for p in main_module._AUTH_EXEMPT_PREFIXES)
    assert not any("aag" in p for p in main_module._SERVICE_AUTH_EXACT_PATHS)
