from __future__ import annotations

import importlib.util
import os
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
    for name in (
        "KAKAOBOT_INSTALL_DIR",
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
    assert "--onefile" in cmd
    assert "--windowed" in cmd
    assert "AADS-Shinhan-Collector-Setup" in cmd
    assert str(ROOT / "pc_agent" / "shinhan_collector_launcher.py") == cmd[-1]
    assert any("agent.py" in item for item in cmd)
    assert any("commands" in item for item in cmd)
