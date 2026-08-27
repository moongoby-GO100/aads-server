from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase, mock

from pc_agent import agent as pc_agent_module
from pc_agent import launcher


class PcAgentLauncherStartupTest(TestCase):
    def test_install_ticket_is_extracted_from_downloaded_exe_filename(self) -> None:
        ticket = "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789abcdEFGH"
        exe = rf"C:\Users\CEO\Downloads\AADS-PC-Agent-Setup-1.0.57--ticket-{ticket}.exe"

        with mock.patch.object(launcher.sys, "argv", [exe]), \
             mock.patch.object(launcher.sys, "executable", exe):
            self.assertEqual(launcher._extract_install_ticket(), ticket)

    def test_install_ticket_exchange_returns_config(self) -> None:
        ticket = "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789abcdEFGH"

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):  # noqa: ANN002
                return False

            def read(self) -> bytes:
                return (
                    b'{"agent_token":"tok_123",'
                    b'"server_url":"wss://example.test/api/v1/pc-agent/ws"}'
                )

        with mock.patch("urllib.request.urlopen", return_value=FakeResponse()):
            cfg = launcher._exchange_install_ticket(ticket)

        self.assertEqual(cfg["agent_token"], "tok_123")
        self.assertEqual(cfg["server_url"], "wss://example.test/api/v1/pc-agent/ws")
        self.assertEqual(cfg["setup_method"], "install_ticket")

    def test_hidden_watchdog_vbs_uses_single_process_guard(self) -> None:
        script = launcher._build_hidden_watchdog_vbs(
            r"C:\Users\rkvs3\Downloads\AADS-PC-Agent-Setup-1.0.53.exe"
        )

        self.assertIn("WScript.Shell", script)
        self.assertIn("Win32_Process", script)
        self.assertIn("shell.Run Chr(34) & agentExe & Chr(34), 0, False", script)
        self.assertIn("/SC ONLOGON", script)
        self.assertNotIn("/SC MINUTE", script)

    def test_watchdog_task_is_logon_only_and_console_free(self) -> None:
        calls: list[tuple[list[str], dict]] = []

        def fake_run(args, **kwargs):  # noqa: ANN001
            calls.append((args, kwargs))
            return SimpleNamespace(returncode=0, stderr="")

        with tempfile.TemporaryDirectory() as temp_dir:
            with mock.patch.object(launcher.sys, "platform", "win32"), \
                 mock.patch.object(launcher.subprocess, "CREATE_NO_WINDOW", 0x08000000, create=True), \
                 mock.patch.object(launcher.sys, "frozen", True, create=True), \
                 mock.patch.object(launcher.sys, "executable", r"C:\AADS-PC-Agent-Setup-1.0.53.exe"), \
                 mock.patch.object(launcher.subprocess, "run", side_effect=fake_run), \
                 mock.patch.object(launcher, "INSTALL_DIR", Path(temp_dir)):
                launcher.register_watchdog_task()

        command, kwargs = calls[0]
        self.assertEqual(command[command.index("/SC") + 1], "ONLOGON")
        self.assertEqual(command[command.index("/RL") + 1], "LIMITED")
        self.assertEqual(kwargs["creationflags"], 0x08000000)

    def test_status_queries_are_console_free(self) -> None:
        launcher_calls: list[dict] = []
        agent_calls: list[dict] = []

        def fake_launcher_run(_args, **kwargs):  # noqa: ANN001
            launcher_calls.append(kwargs)
            return SimpleNamespace(returncode=0, stdout="ready", stderr="")

        def fake_agent_run(_args, **kwargs):  # noqa: ANN001
            agent_calls.append(kwargs)
            return SimpleNamespace(returncode=0, stdout="ready", stderr="")

        with mock.patch.object(launcher.sys, "platform", "win32"), \
             mock.patch.object(launcher.subprocess, "CREATE_NO_WINDOW", 0x08000000, create=True), \
             mock.patch.object(launcher.subprocess, "run", side_effect=fake_launcher_run):
            self.assertTrue(launcher._watchdog_task_status()["registered"])

        fake_agent = object.__new__(pc_agent_module.PCAgent)
        fake_agent._telemetry_cache = {}
        fake_agent._telemetry_cached_at = 0.0
        fake_agent._started_at = 0.0
        fake_agent._agent_start_count = 1
        with mock.patch.object(pc_agent_module.sys, "platform", "win32"), \
             mock.patch.object(pc_agent_module.subprocess, "CREATE_NO_WINDOW", 0x08000000, create=True), \
             mock.patch.object(pc_agent_module.subprocess, "run", side_effect=fake_agent_run):
            self.assertTrue(fake_agent._runtime_telemetry()["watchdog_task"]["registered"])

        self.assertEqual(launcher_calls[0]["creationflags"], 0x08000000)
        self.assertEqual(agent_calls[0]["creationflags"], 0x08000000)


class PcAgentSelfUpdateTest(IsolatedAsyncioTestCase):
    async def test_auto_update_loop_closes_worker_for_launcher_download(self) -> None:
        class FakeWebSocket:
            closed = False
            state = "OPEN"
            close_code = None
            close_reason = None

            async def close(self, *, code: int, reason: str) -> None:
                self.closed = True
                self.state = "CLOSED"
                self.close_code = code
                self.close_reason = reason

        async def fake_sleep(_seconds):  # noqa: ANN001
            return None

        async def fake_check_for_updates() -> bool:
            return True

        fake_agent = object.__new__(pc_agent_module.PCAgent)
        fake_agent._exit_for_update = False
        fake_agent._running = True
        fake_agent.is_connected = True
        ws = FakeWebSocket()

        fake_updater = SimpleNamespace(check_for_updates=fake_check_for_updates)
        with mock.patch.object(pc_agent_module.asyncio, "sleep", fake_sleep), \
             mock.patch.object(pc_agent_module, "updater", fake_updater):
            await fake_agent._auto_update_loop(ws)

        self.assertTrue(fake_agent._exit_for_update)
        self.assertFalse(fake_agent._running)
        self.assertEqual(ws.close_code, 1000)
        self.assertEqual(ws.close_reason, "auto_update")

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
