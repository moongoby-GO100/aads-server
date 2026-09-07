"""AADS Shinhan EasyView Windows Collector launcher.

This is a product-specific PyInstaller entrypoint. It reuses the hardened
PC Agent launcher/worker, but isolates install state, mutexes, watchdog task,
role, and capabilities so it can run as a bank-only collector.
"""
from __future__ import annotations

import os
from pathlib import Path


def _default_local_appdata() -> str:
    return os.environ.get(
        "LOCALAPPDATA",
        str(Path.home() / "AppData" / "Local"),
    )


def configure_shinhan_collector_environment() -> None:
    """Set bank-only defaults before importing the shared launcher module."""
    os.environ.setdefault(
        "KAKAOBOT_INSTALL_DIR",
        os.path.join(_default_local_appdata(), "AADSShinhanCollector"),
    )
    os.environ.setdefault("AADS_PC_AGENT_APP_NAME", "AADS Shinhan Collector")
    os.environ.setdefault("AADS_PC_AGENT_APP_TITLE", "AADS Shinhan Collector")
    os.environ.setdefault("AADS_PC_AGENT_APP_SLUG", "AADSShinhanCollector")
    os.environ.setdefault(
        "AADS_PC_AGENT_LAUNCHER_MUTEX_NAME",
        "AADSShinhanCollector_Launcher_SingleInstance_v1",
    )
    os.environ.setdefault(
        "AADS_PC_AGENT_MUTEX_NAME",
        "AADSShinhanCollector_Agent_SingleInstance_v1",
    )
    os.environ.setdefault(
        "AADS_PC_AGENT_WATCHDOG_TASK_NAME",
        "AADSShinhanCollectorWatchdog",
    )
    os.environ.setdefault(
        "AADS_PC_AGENT_WATCHDOG_SCRIPT_NAME",
        "aads_shinhan_collector_watchdog.vbs",
    )
    os.environ.setdefault(
        "AADS_PC_AGENT_LEGACY_RUN_VALUE_NAME",
        "AADSShinhanCollector",
    )
    os.environ.setdefault(
        "AADS_PC_AGENT_LEGACY_STARTUP_CMD_NAME",
        "AADS-Shinhan-Collector-Watchdog.cmd",
    )
    os.environ.setdefault("AADS_PC_AGENT_NODE_ROLE", "bank_collector")

    capabilities = {
        "bank_collector",
        "financial_exclusive",
        "shinhan_easyview",
        "chrome_cdp",
        "interactive_browser",
    }
    existing = os.environ.get("AADS_PC_AGENT_CAPABILITIES", "")
    for item in existing.split(","):
        normalized = item.strip().lower().replace("-", "_")
        if normalized:
            capabilities.add(normalized)
    os.environ["AADS_PC_AGENT_CAPABILITIES"] = ",".join(sorted(capabilities))


def main() -> None:
    configure_shinhan_collector_environment()
    from launcher import main as launcher_main

    launcher_main()


if __name__ == "__main__":
    main()
