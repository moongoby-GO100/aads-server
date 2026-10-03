"""POST /chat/messages/send 수동 파싱 ValidationError → 500 대신 422 구조화 오류.

실측(2026-10-03): idempotency_key 가 64자를 넘으면 pydantic string_too_long 이
처리되지 않고 'Exception in ASGI application' → 500 이 되었다.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from app.models.chat import MessageSendRequest
from app.routers import chat as chat_router

SESSION_ID = str(uuid.uuid4())
KEY_64 = "k" * 64
KEY_65 = "SECRET-" + "k" * 58  # 65자, 원문 일부가 응답에 새면 안 된다
CONTEXT = {"tenant": {"id": str(uuid.uuid4())}, "user": {"user_id": str(uuid.uuid4())}}


class FakeRequest:
    def __init__(self, body=None, *, content_type="application/json", json_error=None, form=None):
        self.headers = {"content-type": content_type}
        self._body = body
        self._json_error = json_error
        self._form = form

    async def json(self):
        if self._json_error:
            raise self._json_error
        return self._body

    async def form(self):
        return self._form


class FakeForm(dict):
    def getlist(self, name):
        return []


def _payload(**over):
    body = {"session_id": SESSION_ID, "content": "hello"}
    body.update(over)
    return body


@pytest.fixture
def svc_mocks(monkeypatch):
    """접수 이후 단계(세션 조회·스트림 시작·메시지 저장)를 전부 호출 추적용 mock 으로 교체."""
    async def _stream(**kwargs):
        yield "data: {}\n\n"

    m = MagicMock()
    m.get_session = AsyncMock(return_value={"id": SESSION_ID})
    m.get_html_edit_context_state = AsyncMock(return_value={})
    m.send_message_stream = MagicMock(side_effect=lambda **kw: _stream(**kw))
    m.with_background_completion = MagicMock(side_effect=lambda stream, **kw: stream)
    m._save_message = AsyncMock()
    monkeypatch.setattr(chat_router, "svc", m)
    monkeypatch.setattr(chat_router, "is_streaming", lambda sid: False)
    monkeypatch.setattr(
        "app.services.tenant_usage_limits.check_tenant_usage_limit", AsyncMock(return_value=None)
    )
    return m


async def _send(request):
    return await chat_router.send_message(request, contract_version=None, context=CONTEXT)


def _assert_nothing_accepted(m):
    m.get_session.assert_not_called()
    m.send_message_stream.assert_not_called()
    m._save_message.assert_not_called()


async def test_65_char_key_returns_422_without_leaking_input(svc_mocks):
    with pytest.raises(HTTPException) as ei:
        await _send(FakeRequest(_payload(idempotency_key=KEY_65)))

    assert ei.value.status_code == 422
    detail = ei.value.detail
    assert detail["code"] == "invalid_message_payload"
    assert detail["accepted"] is False
    assert detail["errors"] == [
        {"field": "idempotency_key", "type": "string_too_long", "max_length": 64}
    ]
    assert "SECRET" not in repr(detail)
    assert "input" not in repr(detail)


async def test_422_does_not_accept_message(svc_mocks):
    with pytest.raises(HTTPException):
        await _send(FakeRequest(_payload(idempotency_key=KEY_65)))
    _assert_nothing_accepted(svc_mocks)


async def test_64_char_key_passes_validation_and_is_forwarded(svc_mocks):
    response = await _send(FakeRequest(_payload(idempotency_key=KEY_64)))

    assert response.status_code == 200
    svc_mocks.send_message_stream.assert_called_once()
    assert svc_mocks.send_message_stream.call_args.kwargs["idempotency_key"] == KEY_64


async def test_resend_same_key_is_forwarded_both_times(svc_mocks):
    for _ in range(2):
        await _send(FakeRequest(_payload(idempotency_key=KEY_64)))

    keys = [c.kwargs["idempotency_key"] for c in svc_mocks.send_message_stream.call_args_list]
    assert keys == [KEY_64, KEY_64]


async def test_duplicate_key_save_message_still_dedups():
    from app.services import chat_service

    conn = MagicMock()
    txn = MagicMock()
    txn.__aenter__ = AsyncMock(return_value=None)
    txn.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=txn)
    conn.fetchrow = AsyncMock(return_value=None)  # ON CONFLICT DO NOTHING → RETURNING 없음
    conn.execute = AsyncMock()

    result = await chat_service._save_message(
        conn, uuid.UUID(SESSION_ID), "user", "hello", idempotency_key=KEY_64
    )

    assert result is None
    sql = conn.fetchrow.call_args.args[0]
    assert "ON CONFLICT (idempotency_key)" in sql
    conn.execute.assert_not_called()


async def test_missing_required_field_is_422_with_field_name(svc_mocks):
    with pytest.raises(HTTPException) as ei:
        await _send(FakeRequest({"session_id": SESSION_ID}))

    assert ei.value.status_code == 422
    assert {"field": "content", "type": "missing"} in ei.value.detail["errors"]
    _assert_nothing_accepted(svc_mocks)


async def test_invalid_uuid_does_not_echo_input(svc_mocks):
    with pytest.raises(HTTPException) as ei:
        await _send(FakeRequest(_payload(session_id="TOPSECRET-not-a-uuid")))

    assert ei.value.status_code == 422
    assert "TOPSECRET" not in repr(ei.value.detail)
    assert ei.value.detail["errors"][0]["field"] == "session_id"


@pytest.mark.parametrize("body", [[1, 2], "text", 7, None])
async def test_non_dict_body_is_422(svc_mocks, body):
    with pytest.raises(HTTPException) as ei:
        await _send(FakeRequest(body))

    assert ei.value.status_code == 422
    assert ei.value.detail["accepted"] is False
    _assert_nothing_accepted(svc_mocks)


async def test_malformed_json_is_400(svc_mocks):
    with pytest.raises(HTTPException) as ei:
        await _send(FakeRequest(json_error=ValueError("boom: SECRET")))

    assert ei.value.status_code == 400
    assert ei.value.detail["code"] == "invalid_message_payload"
    assert ei.value.detail["accepted"] is False
    assert "SECRET" not in repr(ei.value.detail)
    _assert_nothing_accepted(svc_mocks)


async def test_multipart_65_char_key_is_422(svc_mocks):
    form = FakeForm(session_id=SESSION_ID, content="hello", idempotency_key=KEY_65)
    with pytest.raises(HTTPException) as ei:
        await _send(FakeRequest(content_type="multipart/form-data; boundary=x", form=form))

    assert ei.value.status_code == 422
    assert ei.value.detail["errors"] == [
        {"field": "idempotency_key", "type": "string_too_long", "max_length": 64}
    ]
    assert "SECRET" not in repr(ei.value.detail)
    _assert_nothing_accepted(svc_mocks)


async def test_multipart_64_char_key_passes(svc_mocks):
    form = FakeForm(session_id=SESSION_ID, content="hello", idempotency_key=KEY_64)
    response = await _send(FakeRequest(content_type="multipart/form-data; boundary=x", form=form))

    assert response.status_code == 200
    assert svc_mocks.send_message_stream.call_args.kwargs["idempotency_key"] == KEY_64


def test_router_limit_matches_model_constraint():
    meta = MessageSendRequest.model_fields["idempotency_key"].metadata
    assert any(getattr(m, "max_length", None) == 64 for m in meta)
    assert chat_router._IDEMPOTENCY_KEY_MAX_LENGTH == 64
