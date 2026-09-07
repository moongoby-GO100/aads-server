from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_shinhan_launcher_sets_bank_only_runtime_identity(monkeypatch) -> None:
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\CEO\AppData\Local")
    monkeypatch.setenv("KAKAOBOT_INSTALL_DIR", r"C:\Users\CEO\AppData\Local\KakaoBot")
    for name in (
        "AADS_PC_AGENT_APP_NAME",
        "AADS_PC_AGENT_APP_TITLE",
        "AADS_PC_AGENT_APP_SLUG",
        "AADS_PC_AGENT_LAUNCHER_MUTEX_NAME",
        "AADS_PC_AGENT_MUTEX_NAME",
        "AADS_PC_AGENT_WATCHDOG_TASK_NAME",
        "AADS_PC_AGENT_WATCHDOG_SCRIPT_NAME",
        "AADS_PC_AGENT_LEGACY_RUN_VALUE_NAME",
        "AADS_PC_AGENT_LEGACY_STARTUP_CMD_NAME",
        "AADS_PC_AGENT_NODE_ROLE",
        "AADS_PC_AGENT_CAPABILITIES",
    ):
        monkeypatch.delenv(name, raising=False)

    launcher = _load(
        "shinhan_collector_launcher_test",
        ROOT / "pc_agent" / "shinhan_collector_launcher.py",
    )
    launcher.configure_shinhan_collector_environment()

    assert os.environ["KAKAOBOT_INSTALL_DIR"].endswith("AADSShinhanCollector")
    assert os.environ["AADS_PC_AGENT_APP_NAME"] == "AADS Shinhan Collector"
    assert os.environ["AADS_PC_AGENT_NODE_ROLE"] == "bank_collector"
    assert os.environ["AADS_PC_AGENT_LAUNCHER_MUTEX_NAME"].startswith("AADSShinhanCollector")
    assert os.environ["AADS_PC_AGENT_MUTEX_NAME"].startswith("AADSShinhanCollector")
    assert os.environ["AADS_PC_AGENT_WATCHDOG_TASK_NAME"] == "AADSShinhanCollectorWatchdog"
    capabilities = set(os.environ["AADS_PC_AGENT_CAPABILITIES"].split(","))
    assert {"bank_collector", "financial_exclusive", "shinhan_easyview"} <= capabilities


def test_shinhan_pyinstaller_build_command_uses_product_entrypoint() -> None:
    build = _load(
        "build_shinhan_collector_exe_test",
        ROOT / "pc_agent" / "build_shinhan_collector_exe.py",
    )

    cmd = build.build_command()

    assert "PyInstaller" in cmd
    assert "--noconfirm" in cmd
    assert "--onefile" in cmd
    assert "--windowed" in cmd
    assert "AADS-Shinhan-Collector-Setup" in cmd
    assert str(ROOT / "pc_agent" / "shinhan_collector_launcher.py") == cmd[-1]
    assert any("agent.py" in item for item in cmd)
    assert any("commands" in item for item in cmd)


def test_launcher_bootstraps_bundled_agent_files(monkeypatch, tmp_path) -> None:
    install_dir = tmp_path / "install"
    bundle_dir = tmp_path / "bundle"
    commands_dir = bundle_dir / "commands"
    commands_dir.mkdir(parents=True)
    for name in ("agent.py", "updater.py", "tray.py", "VERSION", "__init__.py"):
        (bundle_dir / name).write_text(f"# {name}\n", encoding="utf-8")
    (commands_dir / "__init__.py").write_text("COMMAND_HANDLERS = {}\n", encoding="utf-8")

    monkeypatch.setenv("KAKAOBOT_INSTALL_DIR", str(install_dir))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle_dir), raising=False)

    launcher = _load(
        "pc_agent_launcher_bootstrap_test",
        ROOT / "pc_agent" / "launcher.py",
    )

    assert launcher.bootstrap_bundled_agent_if_needed() is True
    assert (install_dir / "agent" / "agent.py").exists()
    assert (install_dir / "agent" / "commands" / "__init__.py").exists()


def test_launcher_bootstrap_overwrites_existing_worker_files(monkeypatch, tmp_path) -> None:
    install_dir = tmp_path / "install"
    bundle_dir = tmp_path / "bundle"
    commands_dir = bundle_dir / "commands"
    installed_commands_dir = install_dir / "agent" / "commands"
    commands_dir.mkdir(parents=True)
    installed_commands_dir.mkdir(parents=True)
    (bundle_dir / "agent.py").write_text("# new agent\n", encoding="utf-8")
    (commands_dir / "__init__.py").write_text("# new commands\n", encoding="utf-8")
    (install_dir / "agent" / "agent.py").write_text("# old agent\n", encoding="utf-8")
    (installed_commands_dir / "__init__.py").write_text("# old commands\n", encoding="utf-8")

    monkeypatch.setenv("KAKAOBOT_INSTALL_DIR", str(install_dir))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle_dir), raising=False)

    launcher = _load(
        "pc_agent_launcher_bootstrap_overwrite_test",
        ROOT / "pc_agent" / "launcher.py",
    )

    assert launcher.bootstrap_bundled_agent_if_needed() is True
    assert (install_dir / "agent" / "agent.py").read_text(encoding="utf-8") == "# new agent\n"
    assert (installed_commands_dir / "__init__.py").read_text(encoding="utf-8") == "# new commands\n"


def test_launcher_load_config_accepts_windows_utf8_bom(monkeypatch, tmp_path) -> None:
    install_dir = tmp_path / "install"
    install_dir.mkdir()
    (install_dir / "config.json").write_text(
        '\ufeff{"agent_token":"token","agent_id":"shinhan-e98"}',
        encoding="utf-8",
    )
    monkeypatch.setenv("KAKAOBOT_INSTALL_DIR", str(install_dir))

    launcher = _load(
        "pc_agent_launcher_bom_config_test",
        ROOT / "pc_agent" / "launcher.py",
    )

    assert launcher.load_config() == {
        "agent_token": "token",
        "agent_id": "shinhan-e98",
    }


def test_agent_config_accepts_windows_utf8_bom(monkeypatch, tmp_path) -> None:
    install_dir = tmp_path / "install"
    install_dir.mkdir()
    (install_dir / "config.json").write_text(
        '\ufeff{"agent_token":"token","agent_id":"shinhan-e98"}',
        encoding="utf-8",
    )
    monkeypatch.setenv("KAKAOBOT_INSTALL_DIR", str(install_dir))

    agent = _load(
        "pc_agent_bom_config_test",
        ROOT / "pc_agent" / "agent.py",
    )

    assert agent._read_agent_config()["agent_id"] == "shinhan-e98"
    assert agent._get_persistent_agent_id() == "shinhan-e98"
