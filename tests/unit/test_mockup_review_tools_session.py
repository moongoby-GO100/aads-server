"""mockup_review_submit create 가 채팅 세션 ID 를 목업 head 에 기록하는지 고정한다."""
from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock

import pytest

from app.services import mockup_review_service as svc
from app.services import mockup_review_tools as mrt
from tests.unit.test_mockup_review_tools import REVIEW, SESSION, TENANT, FakeConn, FakePool

OTHER_SESSION = "44444444-4444-4444-4444-444444444444"


@pytest.fixture
def create_mock(monkeypatch):
    mock = AsyncMock(return_value={"review_id": REVIEW, "status": "draft", "generation": 0, "idempotent": False})
    monkeypatch.setattr(svc, "create_review", mock)
    return mock


def _create(session_id, conn=None, **extra):
    inp = {"action": "create", "project": "AADS", "title": "목업", "change_type": "new", **extra}
    return asyncio.run(mrt.run_action(tenant_id=TENANT, session_id=session_id, inp=inp,
                                      pool=FakePool(conn or FakeConn())))


def test_create_records_chat_session_id(create_mock):
    out = _create(SESSION)
    assert out["review_id"] == REVIEW
    assert create_mock.await_args.args[5].session_id == uuid.UUID(SESSION)


def test_malformed_session_id_is_rejected_as_invalid_input(create_mock):
    conn = FakeConn()
    out = _create("not-a-uuid", conn)
    assert out["error"] == "invalid_input" and "session_id" in out["message"]
    assert conn.sql == [] and not create_mock.await_count


def test_auto_idempotency_key_differs_per_session(create_mock):
    _create(SESSION)
    first = create_mock.await_args.args[5].idempotency_key
    _create(OTHER_SESSION)
    second = create_mock.await_args.args[5].idempotency_key
    _create(SESSION)
    third = create_mock.await_args.args[5].idempotency_key
    assert first != second and first == third


def test_explicit_idempotency_key_is_kept(create_mock):
    _create(SESSION, idempotency_key="my-explicit-key")
    assert create_mock.await_args.args[5].idempotency_key == "my-explicit-key"


class _ScopeConn:
    def __init__(self, workspace_project, workspace_name, user_id="user-1"):
        self.row = {"id": SESSION, "user_id": user_id, "project_key": workspace_project,
                    "workspace_name": workspace_name, "workspace_display_name": workspace_name}

    async def fetchval(self, sql, *args):
        return None

    async def fetchrow(self, sql, *args):
        if "FROM chat_sessions s JOIN chat_workspaces" in sql:
            return self.row
        if "INSERT INTO mockup_review_heads" in sql:
            raise _Inserted(args)
        return None


class _Inserted(Exception):
    pass


@pytest.mark.parametrize("ws_project, ws_name, allowed", [
    ("CEO", svc.CEO_INTEGRATED_NAME, True),
    ("AADS", "[AADS] 프로젝트", True),
    ("CEO", "다른 워크스페이스", False),
    ("KIS", "[KIS] 프로젝트", False),
])
def test_session_scope_allows_ceo_integrated_workspace_for_aads(ws_project, ws_name, allowed):
    conn = _ScopeConn(ws_project, ws_name)
    coro = svc._session_scope(conn, TENANT, "AADS", uuid.UUID(SESSION), "user-1", False)
    if allowed:
        assert asyncio.run(coro)["id"] == SESSION
    else:
        with pytest.raises(Exception) as err:
            asyncio.run(coro)
        assert getattr(err.value, "status_code", None) == 404
