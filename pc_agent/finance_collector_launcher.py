"""AADS Finance Collector Windows launcher.

Unified entrypoint for bank transaction collection (Shinhan + IBK).
Reuses the hardened PC Agent launcher/worker, but isolates install state,
mutexes, watchdog task, role, and capabilities for bank-only collection.
"""
from __future__ import annotations

import os
from pathlib import Path


def _default_local_appdata() -> str:
    return os.environ.get(
        "LOCALAPPDATA",
        str(Path.home() / "AppData" / "Local"),
    )


def configure_finance_collector_environment() -> None:
    """Set bank-only defaults before importing the shared launcher module."""
    os.environ["KAKAOBOT_INSTALL_DIR"] = os.path.join(
        _default_local_appdata(), "AADSFinanceCollector"
    )
    os.environ["AADS_PC_AGENT_APP_NAME"] = "AADS Finance Collector"
    os.environ["AADS_PC_AGENT_APP_TITLE"] = "AADS Finance Collector"
    os.environ["AADS_PC_AGENT_APP_SLUG"] = "AADSFinanceCollector"
    os.environ["AADS_PC_AGENT_LAUNCHER_MUTEX_NAME"] = (
        "AADSFinanceCollector_Launcher_SingleInstance_v1"
    )
    os.environ["AADS_PC_AGENT_MUTEX_NAME"] = (
        "AADSFinanceCollector_Agent_SingleInstance_v1"
    )
    os.environ["AADS_PC_AGENT_WATCHDOG_TASK_NAME"] = "AADSFinanceCollectorWatchdog"
    os.environ["AADS_PC_AGENT_WATCHDOG_SCRIPT_NAME"] = (
        "aads_finance_collector_watchdog.vbs"
    )
    os.environ["AADS_PC_AGENT_LEGACY_RUN_VALUE_NAME"] = "AADSFinanceCollector"
    os.environ["AADS_PC_AGENT_LEGACY_STARTUP_CMD_NAME"] = (
        "AADS-Finance-Collector-Watchdog.cmd"
    )
    os.environ["AADS_PC_AGENT_NODE_ROLE"] = "bank_collector"

    capabilities = {
        "bank_collector",
        "financial_exclusive",
        "shinhan_easyview",
        "ibk_quick_service",
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
    configure_finance_collector_environment()
    from launcher import main as launcher_main

    launcher_main()


if __name__ == "__main__":
    main()
