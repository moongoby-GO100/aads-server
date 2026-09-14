"""`ask_session` 이 원세션을 잃지 않게 한다.

2026-09-15 #310 주도 세션에서 5회 연속 `origin_session_missing` 이 났다.
질문은 한 건도 나가지 못했고, 실패 코드만 돌아와 담당은 같은 호출을 반복했다.

원인 계열은 하나다 — **세션을 찾는 길이 호출 경로마다 달랐다.** 이 도구만
contextvar 를 직접 읽어, 브릿지 env 가 빈 문자열인 경로(Agent SDK)에서
세션이 사라졌다. 길을 한 벌(`_resolve_bound_chat_session_id`)로 모으고,
도구 입력으로도 실어 보내 운반로를 둘로 만든다.
"""
import asyncio
import inspect

import pytest

from app.services import session_relay, tool_executor


def _strip_comments(text: str) -> str:
    return "\n".join(l for l in text.splitlines() if not l.strip().startswith("#"))


def test_ask_session_uses_shared_resolver():
    src = _strip_comments(inspect.getsource(tool_executor.ToolExecutor._ask_session))
    assert "_resolve_bound_chat_session_id" in src
    # 옛 경로(직접 contextvar 읽기)가 남아 있으면 길이 다시 둘로 갈린다.
    assert "current_chat_session_id.get()" not in src


def test_resolver_falls_back_to_sdk_stream(monkeypatch):
    """contextvar 가 비어도 SDK 스트림에 묶인 세션을 찾는다."""
    import app.services.agent_sdk_service as sdk

    monkeypatch.setattr(sdk, "_active_chat_session_id", "5090a247-47f7-4a05-a965-da89f844ad2f")
    tool_executor.current_chat_session_id.set("")
    assert tool_executor._resolve_bound_chat_session_id("") == "5090a247-47f7-4a05-a965-da89f844ad2f"


def test_origin_missing_tells_the_caller_what_to_do():
    """코드만 돌려주면 담당은 같은 호출을 반복한다."""
    out = asyncio.run(session_relay.ask("", "StockDiscoveryOwner", "질문"))
    assert out["sent"] is False
    assert out["error"] == "origin_session_missing"
    assert "session_id" in out["message"]


def test_ask_session_is_session_bound_on_both_carriers():
    from mcp_servers import aads_tools_bridge
    from app.services import model_selector

    assert "ask_session" in model_selector._SESSION_BOUND_TOOLS
    assert "ask_session" in aads_tools_bridge._SESSION_BOUND_TOOLS


def test_schema_accepts_session_id():
    from app.services.tool_registry import _TOOLS

    schema = _TOOLS["ask_session"]["input_schema"]
    assert "session_id" in schema["properties"]
    assert "session_id" not in schema["required"]
