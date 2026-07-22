from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase, mock

from pc_agent import agent as pc_agent_module
from pc_agent import launcher


class PcAgentLauncherStartupTest(TestCase):
    def test_agent_telemetry_query_is_console_free(self) -> None:
        fake_agent = object.__new__(pc_agent_module.PCAgent)
        fake_agent._telemetry_cache = {}
        fake_agent._telemetry_cached_at = 0.0
        fake_agent._started_at = 0.0
        fake_agent._agent_start_count = 1
        calls: list[tuple[list[str], dict]] = []

        def fake_run(args, **kwargs):  # noqa: ANN001
            calls.append((args, kwargs))
            return SimpleNamespace(returncode=0, stdout="ready", stderr="")

        with mock.patch.object(pc_agent_module.sys, "platform", "win32"), \
             mock.patch.object(pc_agent_module.subprocess, "CREATE_NO_WINDOW", 0x08000000, create=True), \
             mock.patch.object(pc_agent_module.subprocess, "run", side_effect=fake_run):
            telemetry = fake_agent._runtime_telemetry()

        self.assertTrue(telemetry["watchdog_task"]["registered"])
        self.assertEqual(calls[0][1]["creationflags"], 0x08000000)


class PcAgentSelfUpdateTest(IsolatedAsyncioTestCase):
    async def test_self_update_closes_worker_after_sending_result(self) -> None:
        sent: list[dict] = []

        class FakeWebSocket:
            close_code = None
            close_reason = None

            async def send(self, raw: str) -> None:
                import json

                sent.append(json.loads(raw))

            async def close(self, *, code: int, reason: str) -> None:
                self.close_code = code
                self.close_reason = reason

        async def fake_update(_params):  # noqa: ANN001
            return {
                "status": "ok",
                "data": {"updated": True, "restart_requested": True},
            }

        fake_agent = object.__new__(pc_agent_module.PCAgent)
        fake_agent._exit_for_update = False
        fake_agent._running = True
        ws = FakeWebSocket()
        with mock.patch.dict(pc_agent_module.COMMAND_HANDLERS, {"self_update": fake_update}):
            await fake_agent._handle_command(
                ws,
                {"id": "update-1", "payload": {"command_type": "self_update", "params": {}}},
            )

        self.assertEqual(sent[0]["id"], "update-1")
        self.assertTrue(fake_agent._exit_for_update)
        self.assertFalse(fake_agent._running)
        self.assertEqual(ws.close_code, 1000)
        self.assertEqual(ws.close_reason, "self_update")

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
        calls: list[tuple[list[str], dict]] = []

        def fake_run(args, **kwargs):  # noqa: ANN001
            calls.append((args, kwargs))
            return SimpleNamespace(returncode=0, stderr="")

        with tempfile.TemporaryDirectory() as temp_dir:
            with mock.patch.object(launcher.sys, "platform", "win32"), \
                 mock.patch.object(launcher.subprocess, "CREATE_NO_WINDOW", 0x08000000, create=True), \
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
        command, kwargs = calls[0]
        self.assertEqual(command[command.index("/SC") + 1], "ONLOGON")
        self.assertEqual(command[command.index("/RL") + 1], "LIMITED")
        self.assertNotIn("MINUTE", command)
        self.assertNotIn("HIGHEST", command)
        self.assertTrue(command[command.index("/TR") + 1].startswith("wscript.exe "))
        self.assertEqual(kwargs["creationflags"], 0x08000000)

    def test_watchdog_status_query_is_console_free(self) -> None:
        calls: list[tuple[list[str], dict]] = []

        def fake_run(args, **kwargs):  # noqa: ANN001
            calls.append((args, kwargs))
            return SimpleNamespace(returncode=0, stdout="ready", stderr="")

        with mock.patch.object(launcher.sys, "platform", "win32"), \
             mock.patch.object(launcher.subprocess, "CREATE_NO_WINDOW", 0x08000000, create=True), \
             mock.patch.object(launcher.subprocess, "run", side_effect=fake_run):
            result = launcher._watchdog_task_status()

        self.assertTrue(result["registered"])
        self.assertEqual(calls[0][1]["creationflags"], 0x08000000)
