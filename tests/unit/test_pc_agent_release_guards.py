"""PC Agent v1.0.56 release guard regressions."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[2]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_launcher_updater_blocks_server_downgrade() -> None:
    updater = _load("launcher_updater_test", ROOT / "pc_agent" / "updater.py")

    assert updater._is_remote_newer("1.0.55", "1.0.52") is True
    assert updater._is_remote_newer("1.0.50", "1.0.52") is False
    assert updater._is_remote_newer("1.0.52", "1.0.52") is False
    assert updater._is_remote_newer("unknown", "1.0.52") is False


def test_agent_updater_blocks_server_downgrade() -> None:
    updater = _load(
        "agent_updater_test", ROOT / "pc_agent" / "commands" / "updater.py"
    )

    assert updater._is_remote_newer("1.0.55", "1.0.52") is True
    assert updater._is_remote_newer("1.0.50", "1.0.52") is False


def test_full_exit_confirmation_fails_closed(monkeypatch) -> None:
    tray = _load("tray_test", ROOT / "pc_agent" / "tray.py")
    monkeypatch.setattr(tray.sys, "platform", "win32")

    class BrokenWindll:
        @property
        def user32(self):
            raise RuntimeError("dialog unavailable")

    import ctypes

    monkeypatch.setattr(ctypes, "windll", BrokenWindll(), raising=False)
    assert tray.confirm_full_exit() is False


def test_full_exit_confirmation_handles_yes_and_no(monkeypatch) -> None:
    tray = _load("tray_yes_no_test", ROOT / "pc_agent" / "tray.py")
    monkeypatch.setattr(tray.sys, "platform", "win32")

    import ctypes

    yes_box = SimpleNamespace(MessageBoxW=lambda *_args: 6)
    monkeypatch.setattr(
        ctypes, "windll", SimpleNamespace(user32=yes_box), raising=False
    )
    assert tray.confirm_full_exit() is True

    no_box = SimpleNamespace(MessageBoxW=lambda *_args: 7)
    monkeypatch.setattr(
        ctypes, "windll", SimpleNamespace(user32=no_box), raising=False
    )
    assert tray.confirm_full_exit() is False


def test_launcher_has_single_redownload_guard_definition() -> None:
    source = (ROOT / "pc_agent" / "launcher.py").read_text(encoding="utf-8")
    assert source.count("def _can_redownload()") == 1
    assert source.count("def _record_redownload()") == 1
    assert "disable_watchdog_for_user_exit()" in source


def test_release_publish_is_main_only() -> None:
    workflow = (ROOT / ".github" / "workflows" / "build-pc-agent.yml").read_text(
        encoding="utf-8"
    )
    assert (
        "- name: Create/Update Release\n"
        "        if: github.ref == 'refs/heads/main'\n"
    ) in workflow
