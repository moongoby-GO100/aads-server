"""goal_id 조건부 포함 검증: 미지정 시 payload에 키 없음, 지정 시 값 그대로."""

from unittest.mock import patch

import pytest


_SESSION_ID = "11111111-1111-1111-1111-111111111111"


class _FakeResponse:
    def __init__(self, *, payload=None, status_code: int = 200) -> None:
        self._payload = {} if payload is None else payload
        self.status_code = status_code

    def json(self):
        return self._payload


class _FakeAsyncClient:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response
        self.calls: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, *, headers=None, json=None, timeout=None):
        self.calls.append({
            "url": url,
            "headers": headers,
            "json": json,
            "timeout": timeout,
        })
        return self._response


async def _submit(monkeypatch, inp: dict) -> dict:
    """Helper: ToolExecutor._pipeline_runner_submit 호출 후 payload 반환."""
    from app.services.tool_executor import ToolExecutor, current_chat_session_id

    client = _FakeAsyncClient(_FakeResponse(payload={"job_id": "runner-test"}))
    token = current_chat_session_id.set(_SESSION_ID)
    try:
        with patch("httpx.AsyncClient", return_value=client):
            await ToolExecutor()._pipeline_runner_submit(inp)
        return client.calls[0]["json"]
    finally:
        current_chat_session_id.reset(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("goal_id", [None, ""])
async def test_pipeline_runner_submit_omits_goal_id_when_not_provided(goal_id):
    """goal_id 미지정 or 빈 문자열 시, payload에 'goal_id' 키가 없다."""
    inp = {
        "project": "AADS",
        "instruction": "test",
    }
    if goal_id is not None:
        inp["goal_id"] = goal_id

    payload = await _submit(None, inp)

    assert "goal_id" not in payload


@pytest.mark.asyncio
@pytest.mark.parametrize("goal_id", ["goal-123", "uuid-abc-def"])
async def test_pipeline_runner_submit_includes_goal_id_when_provided(goal_id):
    """goal_id 지정 시, payload에 'goal_id' 키가 그 값 그대로 들어간다."""
    inp = {
        "project": "AADS",
        "instruction": "test",
        "goal_id": goal_id,
    }

    payload = await _submit(None, inp)

    assert payload["goal_id"] == goal_id
