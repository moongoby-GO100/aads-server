from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase, mock

from pc_agent import launcher


class PcAgentLauncherStartupTest(TestCase):
    def test_hidden_watchdog_vbs_uses_single_process_guard(self) -> None:
        script = launcher._build_hidden_watchdog_vbs(
            r"C:\Users\rkvs3\Downloads\AADS-PC-Agent-Setup-1.0.52.exe"
        )

        self.assertIn("WScript.Shell", script)
        self.assertIn("Win32_Process", script)
        self.assertIn("AADS-PC-Agent-Setup-1.0.52.exe", script)
        self.assertIn("shell.Run Chr(34) & agentExe & Chr(34), 0, False", script)
        self.assertIn("shell.RegDelete", script)
        self.assertIn("schtasks.exe /Create /TN KakaoBotWatchdog", script)
        self.assertIn("/SC ONLOGON", script)
        self.assertNotIn("/SC MINUTE", script)
        self.assertIn("WScript.Sleep 30000", script)

    def test_watchdog_task_is_logon_only_and_console_free(self) -> None:
        calls: list[list[str]] = []

        def fake_run(args, **_kwargs):  # noqa: ANN001
            calls.append(args)
            return SimpleNamespace(returncode=0, stderr="")

        with tempfile.TemporaryDirectory() as temp_dir:
            with mock.patch.object(launcher.sys, "platform", "win32"), \
                 mock.patch.object(launcher.sys, "frozen", True, create=True), \
                 mock.patch.object(
                     launcher.sys,
                     "executable",
                     r"C:\Users\rkvs3\Downloads\AADS-PC-Agent-Setup-1.0.52.exe",
                 ), \
                 mock.patch.object(launcher.subprocess, "run", side_effect=fake_run), \
                 mock.patch.object(launcher, "INSTALL_DIR", Path(temp_dir)):
                launcher.register_watchdog_task()
                watchdog = Path(temp_dir) / "aads_pc_agent_watchdog.vbs"
                self.assertIn(
                    "AADS-PC-Agent-Setup-1.0.52.exe",
                    watchdog.read_text(encoding="utf-8-sig"),
                )

        self.assertEqual(len(calls), 1)
        command = calls[0]
        self.assertEqual(command[command.index("/SC") + 1], "ONLOGON")
        self.assertEqual(command[command.index("/RL") + 1], "LIMITED")
        self.assertNotIn("MINUTE", command)
        self.assertNotIn("HIGHEST", command)
        self.assertTrue(command[command.index("/TR") + 1].startswith("wscript.exe "))
