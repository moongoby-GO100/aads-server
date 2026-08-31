"""
app/api/health.py 단위 테스트 — FastAPI TestClient 사용.
health 엔드포인트 응답 구조 및 상태 코드 검증.
"""
import pytest
import sys
import os
from unittest.mock import patch, AsyncMock, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))


@pytest.fixture
def client():
    """FastAPI TestClient 생성 (graph_ready=True 상태)."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.health import router

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")

    # health.py 내부에서 from app.main import app_state를 하므로 app.main.app_state를 mock
    with patch("app.main.app_state", {"graph": MagicMock()}):
        with TestClient(app) as c:
            yield c


@pytest.fixture
def client_no_graph():
    """graph 없는 상태의 TestClient."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.health import router

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")

    with patch("app.main.app_state", {}):
        with TestClient(app) as c:
            yield c


def test_health_ok(client):
    """graph_ready=True → status ok."""
    resp = client.get("/api/v1/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["graph_ready"] is True
    assert data["version"] == "0.2.1"


def test_health_initializing(client_no_graph):
    """graph 없음 → status initializing."""
    resp = client_no_graph.get("/api/v1/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "initializing"
    assert data["graph_ready"] is False


def test_health_response_keys(client):
    """응답 키 구조 검증."""
    resp = client.get("/api/v1/health")
    data = resp.json()
    assert "status" in data
    assert "graph_ready" in data
    assert "version" in data


def test_normalize_relay_capacity_exposes_only_safe_operational_fields():
    from app.api.health import _normalize_relay_capacity

    result = _normalize_relay_capacity({
        "status": "ok",
        "max_concurrent": 12,
        "semaphore_available": 7,
        "lease_count": 5,
        "active_leases": {"claude": 2, "codex": 3, "antigravity": 0, "secret-provider": 9},
        "acquire_timeout_sec": 45,
        "acquire_metrics_uptime_sec": 120.5,
        "acquire_metrics": {
            "codex": {
                "attempts": 8,
                "successes": 7,
                "timeouts": 1,
                "wait_attempts": 3,
                "waited_successes": 2,
                "wait_success_rate_pct": 66.7,
                "avg_success_wait_sec": 4.2,
                "private": "hidden",
            },
        },
        "oauth_label": "must-not-leak@example.com",
        "token_available": True,
    })

    assert result["status"] == "ok"
    assert result["max_concurrent"] == 12
    assert result["used"] == 5
    assert result["available"] == 7
    assert result["usage_percent"] == 41.7
    assert result["active_leases"] == {"claude": 2, "codex": 3, "antigravity": 0}
    assert result["acquire_metrics"]["codex"]["wait_success_rate_pct"] == 66.7
    assert "private" not in result["acquire_metrics"]["codex"]
    assert "oauth_label" not in result
    assert "token_available" not in result


def test_normalize_relay_capacity_handles_invalid_payload():
    from app.api.health import _normalize_relay_capacity

    result = _normalize_relay_capacity({"status": "ok", "max_concurrent": "bad"})

    assert result["status"] == "unavailable"
    assert result["max_concurrent"] == 0
    assert result["used"] == 0


def test_relay_capacity_fallback_reuses_recent_safe_snapshot(monkeypatch):
    from datetime import datetime, timezone
    from app.api import health

    cached = health._normalize_relay_capacity({
        "status": "ok",
        "max_concurrent": 12,
        "semaphore_available": 5,
        "active_leases": {"codex": 7},
    })
    monkeypatch.setattr(health, "_relay_capacity_cache", (datetime.now(timezone.utc), cached))

    result = health._relay_capacity_fallback()

    assert result["status"] == "ok"
    assert result["max_concurrent"] == 12
    assert result["used"] == 7
    assert result["stale"] is True
    assert result["stale_age_sec"] >= 0
