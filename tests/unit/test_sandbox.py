"""
services/sandbox.py 단위 테스트 — 커버리지 확대.
execute_in_sandbox, fallback_code_only 검증.
"""
import pytest
import sys
import os
from unittest.mock import AsyncMock, patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))


@pytest.mark.asyncio
async def test_fallback_code_only_returns_graceful():
    """fallback_code_only: E2B 없을 때 코드만 반환."""
    from app.services.sandbox import fallback_code_only
    result = await fallback_code_only("print('hello')")
    assert result["exit_code"] == 0
    assert result["error"] is False
    assert result["sandbox_id"] is None
    assert "E2B unavailable" in result["stdout"]
    assert result["code"] == "print('hello')"


@pytest.mark.asyncio
async def test_fallback_code_only_empty_code():
    """빈 코드도 graceful degradation."""
    from app.services.sandbox import fallback_code_only
    result = await fallback_code_only("")
    assert result["exit_code"] == 0
    assert result["error"] is False


@pytest.mark.asyncio
async def test_execute_in_sandbox_success():
    """execute_in_sandbox 성공 케이스 — E2B mock."""
    from app.services.sandbox import execute_in_sandbox

    mock_execution = MagicMock()
    mock_execution.text = "Hello World"
    mock_execution.error = None

    mock_sandbox = AsyncMock()
    mock_sandbox.run_code = AsyncMock(return_value=mock_execution)
    mock_sandbox.sandbox_id = "test-sandbox-id"
    mock_sandbox.kill = AsyncMock()

    mock_sandbox_class = MagicMock()
    mock_sandbox_class.create = AsyncMock(return_value=mock_sandbox)

    mock_settings = MagicMock()
    mock_settings.E2B_API_KEY.get_secret_value.return_value = "test-key"
    mock_settings.SANDBOX_TIMEOUT_SECONDS = 60

    # AsyncSandbox는 함수 내부에서 import되므로 e2b_code_interpreter 모듈 자체를 mock
    with patch.dict("sys.modules", {"e2b_code_interpreter": MagicMock(AsyncSandbox=mock_sandbox_class)}):
        with patch("app.config.settings", mock_settings):
            result = await execute_in_sandbox("print('Hello World')")

    assert result["stdout"] == "Hello World"
    assert result["exit_code"] == 0
    assert result["error"] is False
    assert result["sandbox_id"] == "test-sandbox-id"


@pytest.mark.asyncio
async def test_execute_in_sandbox_exception_fallback():
    """E2B API 예외 시 에러 dict 반환."""
    from app.services.sandbox import execute_in_sandbox

    mock_sandbox_class = MagicMock()
    mock_sandbox_class.create = AsyncMock(side_effect=Exception("E2B API error"))

    mock_settings = MagicMock()
    mock_settings.E2B_API_KEY.get_secret_value.return_value = "test-key"
    mock_settings.SANDBOX_TIMEOUT_SECONDS = 60

    with patch.dict("sys.modules", {"e2b_code_interpreter": MagicMock(AsyncSandbox=mock_sandbox_class)}):
        with patch("app.config.settings", mock_settings):
            result = await execute_in_sandbox("print('test')")

    # retry 3회 후 에러 반환
    assert result["exit_code"] == 1
    assert result["error"] is True
