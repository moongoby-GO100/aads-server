from __future__ import annotations

import pytest

from app.services import agent_sdk_service


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_orphan_reaper_requires_recognized_opt_in(monkeypatch, value: str) -> None:
    monkeypatch.setenv("AADS_ORPHAN_CLAUDE_REAPER_ENABLED", value)
    assert agent_sdk_service.orphan_claude_reaper_enabled() is True


@pytest.mark.parametrize("value", [None, "", "0", "false", "off", "unexpected"])
def test_orphan_reaper_is_disabled_by_default(monkeypatch, value: str | None) -> None:
    if value is None:
        monkeypatch.delenv("AADS_ORPHAN_CLAUDE_REAPER_ENABLED", raising=False)
    else:
        monkeypatch.setenv("AADS_ORPHAN_CLAUDE_REAPER_ENABLED", value)
    assert agent_sdk_service.orphan_claude_reaper_enabled() is False


def test_cleanup_does_not_signal_processes_without_opt_in(monkeypatch) -> None:
    monkeypatch.delenv("AADS_ORPHAN_CLAUDE_REAPER_ENABLED", raising=False)
    monkeypatch.setattr(agent_sdk_service, "_find_claude_child_pids", lambda: [101, 102, 103])

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("SIGTERM must not be sent while the reaper is disabled")

    monkeypatch.setattr(agent_sdk_service.os, "kill", fail_if_called)
    assert agent_sdk_service.cleanup_orphan_claude_processes() == 0
