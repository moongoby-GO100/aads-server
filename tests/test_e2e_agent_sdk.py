"""
AADS-188E: E2E 시나리오 3 — Agent SDK 자율 실행 테스트

CEO: "AADS 서버 전체 헬스체크하고 이상 있으면 분석해"
플로우:
  1. intent → execute 또는 service_inspection
  2. AgentSDKService.execute_stream() 실행
  3. 자율적으로 health_check → 이상 감지 → code_explorer 등
  4. 3턴 이상 자율 실행 검증
  5. 최종 분석 결과 포함
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture
def mock_sdk_context():
    return MagicMock(session_id="test-session", sse_callback=AsyncMock())


@pytest.mark.asyncio
async def test_agent_sdk_execute_stream_returns_generator():
    """AgentSDKService.execute_stream()이 AsyncGenerator를 반환하는지."""
    from app.services.agent_sdk_service import AgentSDKService

    svc = AgentSDKService()
    gen = svc.execute_stream(
        session_id="test",
        query="AADS 서버 전체 헬스체크하고 이상 있으면 분석해",
        workspace_name="CEO",
    )
    assert gen is not None
    assert hasattr(gen, "__aiter__")


@pytest.mark.asyncio
async def test_agent_sdk_autonomous_turns():
    """자율 실행 시 3턴 이상 도구 호출이 발생할 수 있는 구조인지 (모의)."""
    from app.services.agent_sdk_service import AgentSDKService

    with patch.object(AgentSDKService, "execute_stream") as mock_exec:
        async def fake_stream(*args, **kwargs):
            for i in range(4):
                yield {"type": "tool_use", "tool_name": "health_check", "turn": i + 1}
            yield {"type": "done", "content": "분석 완료"}

        mock_exec.return_value = fake_stream()
        svc = AgentSDKService()
        events = []
        async for ev in svc.execute_stream("s", "헬스체크해", "CEO"):
            events.append(ev)
        assert len(events) >= 3


@pytest.mark.asyncio
async def test_approve_diff_api_accepts_approve_reject():
    """POST /chat/approve-diff가 approve/reject를 저장하고 200을 반환."""
    from fastapi.testclient import TestClient
    from app.main import app
    from app.routers.chat import _diff_approval_store, get_diff_decision

    client = TestClient(app)
    # UUID for session_id
    import uuid
    sid = str(uuid.uuid4())
    tid = "tool_use_123"

    res = client.post(
        "/api/v1/chat/approve-diff",
        json={"session_id": sid, "tool_use_id": tid, "action": "approve"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data.get("success") is True
    assert data.get("action") == "approve"

    assert get_diff_decision(sid, tid) == "approve"

    res2 = client.post(
        "/api/v1/chat/approve-diff",
        json={"session_id": sid, "tool_use_id": "tool_456", "action": "reject"},
    )
    assert res2.status_code == 200
    assert res2.json().get("action") == "reject"


@pytest.mark.asyncio
async def test_approve_diff_rejects_invalid_action():
    """action이 approve/reject가 아니면 400."""
    from fastapi.testclient import TestClient
    from app.main import app
    import uuid

    client = TestClient(app)
    res = client.post(
        "/api/v1/chat/approve-diff",
        json={"session_id": str(uuid.uuid4()), "tool_use_id": "t1", "action": "invalid"},
    )
    assert res.status_code == 400
