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
    assert data["version"] == "0.1.0"


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
