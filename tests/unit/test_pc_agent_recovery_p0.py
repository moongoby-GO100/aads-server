from __future__ import annotations

import asyncio
import importlib.util
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.api.pc_agent import _event_metadata_dict
from app.browser_bridge import aads_adapter
from app.browser_bridge.models import BrowserEndpoint, BrowserEndpointKind, BrowserBridgeSession
from app.browser_bridge.service import BrowserBridgeService


ROOT = Path(__file__).resolve().parents[2]


def _load_shell_module():
    spec = importlib.util.spec_from_file_location(
        "pc_agent_shell_recovery_test", ROOT / "pc_agent" / "commands" / "shell.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_shell_command_wait_is_offloaded_from_websocket_loop(monkeypatch) -> None:
    shell = _load_shell_module()
    calls: list[tuple[object, tuple[object, ...], dict[str, object]]] = []

    async def fake_to_thread(func, *args, **kwargs):  # noqa: ANN001
        calls.append((func, args, kwargs))
        await asyncio.sleep(0)
        return SimpleNamespace(stdout="ok", stderr="", returncode=0)

    monkeypatch.setattr(shell.asyncio, "to_thread", fake_to_thread)

    result = await shell.execute({"command": "echo ok"})

    assert result["status"] == "success"
    assert result["data"]["output"] == "ok"
    assert calls[0][0] is shell._run_shell_command
    assert calls[0][1][0] == "echo ok"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({"worker_connected": True}, {"worker_connected": True}),
        ('{"worker_connected": false}', {"worker_connected": False}),
        ("not-json", {"raw": "not-json"}),
        (None, {}),
    ],
)
def test_event_metadata_dict_accepts_jsonb_dict_or_string(raw, expected) -> None:  # noqa: ANN001
    assert _event_metadata_dict(raw) == expected


@pytest.mark.asyncio
async def test_work_session_failure_uses_explicit_headless_fallback(monkeypatch) -> None:
    headless_context = object()

    class FakeService:
        async def ensure_work_session(self, **_kwargs):
            raise RuntimeError("PC_AGENT_OFFLINE")

        async def _headless_fallback_context(self):
            return headless_context

    monkeypatch.setattr(aads_adapter, "get_browser_bridge_service", FakeService)

    context, error = await aads_adapter.acquire_browser_context(
        browser_work_key="auth-recovery",
        url="https://aads.newtalk.kr/login",
    )

    assert context is headless_context
    assert error is None


def test_offline_local_agent_session_is_not_reused(monkeypatch) -> None:
    session = BrowserBridgeSession(
        session_id="bb-offline",
        label="offline PC",
        endpoint=BrowserEndpoint(
            kind=BrowserEndpointKind.LOCAL_AGENT,
            url="local-agent://ceo-pc/9222",
            metadata={"agent_id": "ceo-pc", "port": 9222},
        ),
        registered_at=datetime.now(timezone.utc),
    )
    monkeypatch.setattr(
        "app.services.pc_agent_manager.pc_agent_manager.get_agent",
        lambda _agent_id: None,
    )

    assert BrowserBridgeService._local_agent_online(session) is False
