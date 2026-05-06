"""notify_channel 도구 단위 테스트."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from app.services.tool_executor import ToolExecutor


@pytest.fixture(autouse=True)
def clear_notify_dedup():
    """인스턴스 레벨 dedup 캐시 초기화."""
    ToolExecutor._notify_dedup_cache = {}
    yield
    ToolExecutor._notify_dedup_cache = {}


@pytest.mark.asyncio
async def test_notify_channel_rejects_empty_message():
    result = await ToolExecutor()._notify_channel({"message": ""})
    assert "error" in result


@pytest.mark.asyncio
async def test_notify_channel_rejects_invalid_channel():
    result = await ToolExecutor()._notify_channel({"message": "test", "channel": "email"})
    assert "error" in result
    assert "channel" in result["error"]


@pytest.mark.asyncio
async def test_notify_channel_sends_telegram():
    mock_send = AsyncMock(return_value="ok")

    with patch("app.api.ceo_chat_tools.tool_send_telegram", mock_send, create=True):
        result = await ToolExecutor()._notify_channel({
            "message": "서버 경고",
            "channel": "telegram",
            "level": "warn",
        })

    assert result["sent"] is True
    assert result["channel"] == "telegram"
    assert result["results"]["telegram"]["success"] is True
    mock_send.assert_awaited_once()


@pytest.mark.asyncio
async def test_notify_channel_dedup_skips_second():
    mock_send = AsyncMock(return_value="ok")

    with patch("app.api.ceo_chat_tools.tool_send_telegram", mock_send, create=True):
        executor = ToolExecutor()
        first = await executor._notify_channel({
            "message": "disk alert",
            "channel": "telegram",
            "dedup_key": "disk-68",
        })
        second = await executor._notify_channel({
            "message": "disk alert",
            "channel": "telegram",
            "dedup_key": "disk-68",
        })

    assert first["sent"] is True
    assert second["skipped"] is True
    assert "dedup" in second["reason"]


@pytest.mark.asyncio
async def test_notify_channel_slack_not_implemented():
    result = await ToolExecutor()._notify_channel({
        "message": "슬랙 테스트",
        "channel": "slack",
    })

    assert result["sent"] is True
    assert result["results"]["slack"]["success"] is False


@pytest.mark.asyncio
async def test_notify_channel_all_sends_to_both():
    mock_send = AsyncMock(return_value="ok")

    with patch("app.api.ceo_chat_tools.tool_send_telegram", mock_send, create=True):
        result = await ToolExecutor()._notify_channel({
            "message": "전체 알림",
            "channel": "all",
        })

    assert result["sent"] is True
    assert "telegram" in result["results"]
    assert "slack" in result["results"]


@pytest.mark.asyncio
async def test_notify_channel_telegram_error_captured():
    mock_send = AsyncMock(side_effect=Exception("bot down"))

    with patch("app.api.ceo_chat_tools.tool_send_telegram", mock_send, create=True):
        result = await ToolExecutor()._notify_channel({
            "message": "에러 테스트",
            "channel": "telegram",
        })

    assert result["sent"] is True
    assert result["results"]["telegram"]["success"] is False
    assert "bot down" in result["results"]["telegram"]["error"]


@pytest.mark.asyncio
async def test_notify_channel_registered_in_dispatch():
    executor = ToolExecutor()
    raw = await executor.execute("notify_channel", {"message": ""})
    result = json.loads(raw)
    assert "error" in result
