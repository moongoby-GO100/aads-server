from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient


def _build_client():
    import app.api.braming as braming_api

    app = FastAPI()
    app.include_router(braming_api.router)
    return app, braming_api


@pytest.mark.asyncio
async def test_get_graph_uses_optional_user_from_bearer(monkeypatch):
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret")
    app, _ = _build_client()

    mock_get_graph = AsyncMock(
        return_value={"session": {"id": "session-1"}, "nodes": [], "edges": []},
    )
    with patch("app.api.braming.verify_token", return_value={"sub": "ceo-1"}), \
         patch("app.api.braming.get_session_graph", new=mock_get_graph):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            resp = await client.get(
                "/api/v1/braming/sessions/session-1",
                headers={"Authorization": "Bearer token"},
            )

    assert resp.status_code == 200
    mock_get_graph.assert_awaited_once_with("session-1", current_user_id="ceo-1")


@pytest.mark.asyncio
async def test_update_node_vote_calls_service_with_current_user(monkeypatch):
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret")
    app, braming_api = _build_client()
    app.dependency_overrides[braming_api.get_current_user] = lambda: {"user_id": "ceo-1"}

    mock_set_node_vote = AsyncMock(return_value={
        "id": "node-1",
        "myVote": "down",
        "voteSummary": {"up": 1, "down": 2, "total": 3, "score": -1},
    })
    with patch("app.api.braming.set_node_vote", new=mock_set_node_vote):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            resp = await client.put(
                "/api/v1/braming/sessions/session-1/nodes/node-1/vote",
                json={"vote": "down"},
            )

    assert resp.status_code == 200
    assert resp.json()["node"]["myVote"] == "down"
    mock_set_node_vote.assert_awaited_once_with("session-1", "node-1", "ceo-1", "down")
