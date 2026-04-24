from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
import uuid

import pytest

from app.services import braming_service


class _AcquireCtx:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _Pool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _AcquireCtx(self._conn)


def _session_row(session_id: str) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "id": session_id,
        "title": "신사업 아이디어",
        "topic": "AADS 브레인스토밍",
        "status": "active",
        "config": {},
        "summary": None,
        "total_cost": 0,
        "created_at": now,
        "updated_at": now,
    }


def _node_row(
    *,
    node_id: str,
    session_id: str,
    label: str = "핵심 아이디어",
    parent_id: str | None = None,
    ceo_opinion: str | None = None,
    ceo_opinion_updated_at: datetime | None = None,
    picked: bool = False,
    picked_at: datetime | None = None,
    picked_by: str | None = None,
) -> dict:
    return {
        "id": node_id,
        "session_id": session_id,
        "parent_id": parent_id,
        "node_type": "idea",
        "label": label,
        "content": "구체적 실행안",
        "agent_role": "strategist",
        "position_x": 120.0,
        "position_y": 240.0,
        "metadata": {"generated_from": "ideas"},
        "cost": 0.0123,
        "created_at": datetime(2026, 4, 24, 1, 2, 3, tzinfo=timezone.utc),
        "ceo_opinion": ceo_opinion,
        "ceo_opinion_updated_at": ceo_opinion_updated_at,
        "picked": picked,
        "picked_at": picked_at,
        "picked_by": picked_by,
    }


@pytest.mark.asyncio
async def test_get_session_graph_includes_interaction_fields():
    session_id = str(uuid.uuid4())
    node_id = str(uuid.uuid4())
    picked_at = datetime(2026, 4, 24, 2, 0, tzinfo=timezone.utc)

    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=_session_row(session_id))
    conn.fetch = AsyncMock(side_effect=[
        [
            _node_row(
                node_id=node_id,
                session_id=session_id,
                ceo_opinion="CEO 코멘트",
                ceo_opinion_updated_at=picked_at,
                picked=True,
                picked_at=picked_at,
                picked_by="ceo-1",
            ),
        ],
        [{"node_id": node_id, "up_votes": 3, "down_votes": 1}],
        [{"node_id": node_id, "vote": "up"}],
    ])

    with patch("app.services.braming_service.get_pool", return_value=_Pool(conn)):
        graph = await braming_service.get_session_graph(session_id, current_user_id="ceo-1")

    assert graph["session"]["id"] == session_id
    node_data = graph["nodes"][0]["data"]
    assert node_data["ceoOpinion"] == "CEO 코멘트"
    assert node_data["voteSummary"] == {"up": 3, "down": 1, "total": 4, "score": 2}
    assert node_data["myVote"] == "up"
    assert node_data["picked"] is True
    assert node_data["pickedBy"] == "ceo-1"


@pytest.mark.asyncio
async def test_save_node_ceo_opinion_returns_updated_detail():
    session_id = str(uuid.uuid4())
    node_id = str(uuid.uuid4())
    updated_at = datetime(2026, 4, 24, 3, 0, tzinfo=timezone.utc)

    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=[
        _node_row(node_id=node_id, session_id=session_id),
        _node_row(
            node_id=node_id,
            session_id=session_id,
            ceo_opinion="우선 검증 필요",
            ceo_opinion_updated_at=updated_at,
        ),
    ])
    conn.fetch = AsyncMock(side_effect=[[], []])
    conn.execute = AsyncMock(return_value="UPDATE 1")

    with patch("app.services.braming_service.get_pool", return_value=_Pool(conn)):
        result = await braming_service.save_node_ceo_opinion(
            session_id,
            node_id,
            "  우선 검증 필요  ",
            current_user_id="ceo-1",
        )

    assert result["ceoOpinion"] == "우선 검증 필요"
    assert result["ceoOpinionUpdatedAt"] == updated_at.isoformat()
    first_update_query = conn.execute.await_args_list[0].args[0]
    assert "SET ceo_opinion = $3" in first_update_query
    assert conn.execute.await_args_list[0].args[3] == "우선 검증 필요"


@pytest.mark.asyncio
async def test_set_node_vote_same_vote_toggles_off():
    session_id = str(uuid.uuid4())
    node_id = str(uuid.uuid4())

    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=[
        _node_row(node_id=node_id, session_id=session_id),
        {"vote": "up"},
        _node_row(node_id=node_id, session_id=session_id),
    ])
    conn.fetch = AsyncMock(side_effect=[[], []])
    conn.execute = AsyncMock(return_value="DELETE 1")

    with patch("app.services.braming_service.get_pool", return_value=_Pool(conn)):
        result = await braming_service.set_node_vote(session_id, node_id, "ceo-1", "up")

    assert result["myVote"] is None
    assert result["voteSummary"] == {"up": 0, "down": 0, "total": 0, "score": 0}
    delete_query = conn.execute.await_args_list[0].args[0]
    assert "DELETE FROM braming_node_votes" in delete_query


@pytest.mark.asyncio
async def test_set_node_pick_marks_node_as_picked():
    session_id = str(uuid.uuid4())
    node_id = str(uuid.uuid4())
    picked_at = datetime(2026, 4, 24, 4, 0, tzinfo=timezone.utc)

    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=[
        _node_row(node_id=node_id, session_id=session_id),
        _node_row(
            node_id=node_id,
            session_id=session_id,
            picked=True,
            picked_at=picked_at,
            picked_by="ceo-1",
        ),
    ])
    conn.fetch = AsyncMock(side_effect=[[], []])
    conn.execute = AsyncMock(return_value="UPDATE 1")

    with patch("app.services.braming_service.get_pool", return_value=_Pool(conn)):
        result = await braming_service.set_node_pick(
            session_id,
            node_id,
            picked=True,
            current_user_id="ceo-1",
        )

    assert result["picked"] is True
    assert result["pickedBy"] == "ceo-1"
    update_query = conn.execute.await_args_list[0].args[0]
    assert "SET picked = TRUE" in update_query
