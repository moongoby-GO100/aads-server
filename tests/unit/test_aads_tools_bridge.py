"""aads_tools_bridge 단위 테스트."""
import os
import sys
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))


@pytest.mark.asyncio
async def test_ensure_db_uses_env_backed_init_pool():
    """DATABASE_URL가 있으면 init_pool()을 인자 없이 호출한다."""
    import mcp_servers.aads_tools_bridge as bridge

    original = bridge._db_initialized
    bridge._db_initialized = False
    try:
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://test/test"}, clear=False):
            with patch("app.core.db_pool.init_pool", new=AsyncMock()) as mock_init:
                await bridge._ensure_db()

        mock_init.assert_awaited_once_with()
        assert bridge._db_initialized is True
    finally:
        bridge._db_initialized = original
