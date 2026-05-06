"""PC Agent 재연결 guard 회귀 테스트."""

from __future__ import annotations

from app.services.pc_agent_manager import PCAgentManager


class DummyWebSocket:
    pass


def test_stale_unregister_does_not_remove_new_connection() -> None:
    manager = PCAgentManager()
    old_ws = DummyWebSocket()
    new_ws = DummyWebSocket()

    manager.register_agent("ceo-pc", old_ws, {"hostname": "old"})
    manager.register_agent("ceo-pc", new_ws, {"hostname": "new"})

    removed = manager.unregister_agent("ceo-pc", old_ws)

    assert removed is False
    current = manager.get_agent("ceo-pc")
    assert current is not None
    assert current.hostname == "new"


def test_current_unregister_removes_connection() -> None:
    manager = PCAgentManager()
    ws = DummyWebSocket()

    manager.register_agent("ceo-pc", ws, {"hostname": "current"})

    removed = manager.unregister_agent("ceo-pc", ws)

    assert removed is True
    assert manager.get_agent("ceo-pc") is None
