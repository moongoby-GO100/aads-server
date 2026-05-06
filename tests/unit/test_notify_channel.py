from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from app.services.tool_executor import ToolExecutor
from app.services.tool_registry import ToolRegistry


@pytest.fixture(autouse=True)
def clear_notify_dedup():
    ToolExecutor._notify_dedup.clear()
    yield
    ToolExecutor._notify_dedup.clear()


def test_notify_channel_registered_in_registry():
    registry = ToolRegistry()
    tool = registry.get_tool("notify_channel")

    assert tool["name"] == "notify_channel"
    assert "notify_channel" in registry.list_groups()["ops"]


@pytest.mark.asyncio
async def test_notify_channel_execute_routes_warning_to_telegram():
    executor = ToolExecutor()

    with patch.object(
        executor,
        "_send_telegram_message",
        new_callable=AsyncMock,
        return_value={"status": "sent", "channel": "telegram"},
    ) as mock_telegram, patch.object(executor, "_log_notify_message") as mock_log:
        raw_result = await executor.execute(
            "notify_channel",
            {"message": "disk warning", "severity": "warning"},
        )

    result = json.loads(raw_result)
    assert result["status"] == "sent"
    assert result["channels"] == ["telegram"]
    mock_telegram.assert_awaited_once_with("disk warning")
    mock_log.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("severity", "expected_channels", "expected_telegram_calls", "expected_log_calls"),
    [
        ("info", ["system"], 0, 1),
        ("warning", ["telegram"], 1, 0),
        ("critical", ["telegram", "system"], 1, 1),
    ],
)
async def test_notify_channel_routes_by_severity(
    severity,
    expected_channels,
    expected_telegram_calls,
    expected_log_calls,
):
    executor = ToolExecutor()

    with patch.object(
        executor,
        "_send_telegram_message",
        new_callable=AsyncMock,
        return_value={"status": "sent", "channel": "telegram"},
    ) as mock_telegram, patch.object(executor, "_log_notify_message") as mock_log:
        result = await executor._notify_channel(
            {"message": f"{severity} alert", "severity": severity}
        )

    assert result["status"] == "sent"
    assert result["channels"] == expected_channels
    assert mock_telegram.await_count == expected_telegram_calls
    assert mock_log.call_count == expected_log_calls


@pytest.mark.asyncio
async def test_notify_channel_skips_duplicate_with_same_key():
    executor = ToolExecutor()

    with patch.object(
        executor,
        "_send_telegram_message",
        new_callable=AsyncMock,
        return_value={"status": "sent", "channel": "telegram"},
    ) as mock_telegram:
        first = await executor._notify_channel(
            {"message": "duplicate alert", "severity": "warning", "dedup_key": "disk-68"}
        )
        second = await executor._notify_channel(
            {"message": "duplicate alert", "severity": "warning", "dedup_key": "disk-68"}
        )

    assert first["status"] == "sent"
    assert second == {
        "status": "skipped",
        "reason": "duplicate",
        "dedup_key": "disk-68",
    }
    mock_telegram.assert_awaited_once_with("duplicate alert")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("channel", "severity", "expected_channels", "expected_telegram_calls", "expected_log_calls"),
    [
        ("telegram", "info", ["telegram"], 1, 0),
        ("system", "critical", ["system"], 0, 1),
    ],
)
async def test_notify_channel_direct_channel_override(
    channel,
    severity,
    expected_channels,
    expected_telegram_calls,
    expected_log_calls,
):
    executor = ToolExecutor()

    with patch.object(
        executor,
        "_send_telegram_message",
        new_callable=AsyncMock,
        return_value={"status": "sent", "channel": "telegram"},
    ) as mock_telegram, patch.object(executor, "_log_notify_message") as mock_log:
        result = await executor._notify_channel(
            {
                "message": f"{channel} override",
                "severity": severity,
                "channel": channel,
            }
        )

    assert result["status"] == "sent"
    assert result["channels"] == expected_channels
    assert mock_telegram.await_count == expected_telegram_calls
    assert mock_log.call_count == expected_log_calls


@pytest.mark.asyncio
async def test_notify_channel_includes_telegram_error_in_result():
    executor = ToolExecutor()

    with patch.object(
        executor,
        "_send_telegram_message",
        new_callable=AsyncMock,
        return_value={"status": "error", "channel": "telegram", "error": "bot down"},
    ) as mock_telegram, patch.object(executor, "_log_notify_message") as mock_log:
        result = await executor._notify_channel(
            {"message": "critical alert", "severity": "critical"}
        )

    assert result["status"] == "partial"
    assert result["channels"] == ["system"]
    assert result["errors"] == [{"channel": "telegram", "error": "bot down"}]
    mock_telegram.assert_awaited_once_with("critical alert")
    mock_log.assert_called_once_with("critical", "critical alert")
