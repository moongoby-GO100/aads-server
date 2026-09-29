"""PC CDP work_key ownership and recovery regressions."""
import asyncio
import importlib
import sys
from types import SimpleNamespace

import pytest

cdp = importlib.import_module("pc_agent.commands.browser_auto")


@pytest.fixture(autouse=True)
def isolated_registry(monkeypatch, tmp_path):
    monkeypatch.setenv("AADS_CDP_SESSION_STATE_FILE", str(tmp_path / "sessions.json"))
    monkeypatch.setattr(cdp, "_default_profile_root", lambda: str(tmp_path))
    cdp.CDPSessionManager._sessions.clear()
    cdp.CDPSessionManager._loaded = True
    yield
    cdp.CDPSessionManager._sessions.clear()
    cdp.CDPSessionManager._loaded = True


def test_port_only_finds_owner_even_when_general_is_registered(tmp_path):
    cdp.CDPSessionManager.register("general", 9222, str(tmp_path / "general"))
    cdp.CDPSessionManager.register("bank", 9333, str(tmp_path / "bank"))
    assert cdp._work_key_from_params({"port": 9333}) == "bank"
    assert cdp._effective_port({"port": 9333}) == 9333


def test_port_only_close_uses_registered_owner(monkeypatch, tmp_path):
    cdp.CDPSessionManager.register("general", 9222, str(tmp_path / "general"))
    cdp.CDPSessionManager.register("bank", 9333, str(tmp_path / "bank"), pid=3)
    monkeypatch.setattr(cdp, "_owned_browser_process", lambda session: (3, 100.0))
    result = asyncio.run(cdp.browser_close_session({"port": 9333, "close_tabs": False, "close_browser": False}))
    assert result["status"] == "success"
    assert result["data"]["work_key"] == "bank"
    assert cdp.CDPSessionManager.get_session("general") is not None


def test_port_only_launch_reuses_bank_owner_with_general_registered(monkeypatch, tmp_path):
    cdp.CDPSessionManager.register("general", 9222, str(tmp_path / "general"), pid=2)
    cdp.CDPSessionManager.register("bank", 9333, str(tmp_path / "bank"), pid=3)
    monkeypatch.setattr(cdp, "_owned_browser_process", lambda session: (session.pid, 100.0))

    async def ready(port):
        return {"webSocketDebuggerUrl": "ws://localhost/browser"}

    monkeypatch.setattr(cdp, "_probe_cdp_version", ready)
    result = asyncio.run(cdp.browser_launch({"port": 9333, "url": "about:blank",
                                             "user_data_dir": str(tmp_path / "bank")}))
    assert result["status"] == "success"
    assert result["data"]["port"] == 9333
    assert cdp.CDPSessionManager.get_session("general").port == 9222


@pytest.mark.parametrize("case, stored_pid, found_pid", [
    ("restart", 101, 202),
    ("pid_reuse", 101, 303),
    ("upgrade", 0, 404),
])
def test_verified_recovery_rebinds_process(monkeypatch, tmp_path, case, stored_pid, found_pid):
    session = cdp.CDPSessionManager.register("bank", 9333, str(tmp_path / "bank"), pid=stored_pid)
    session.process_started_at = 10.0 if case == "pid_reuse" else 0.0
    monkeypatch.setattr(cdp, "_owned_browser_process", lambda current: (found_pid, 20.0))
    assert cdp._adopt_session_process(session, {})
    assert (session.pid, session.process_started_at) == (found_pid, 20.0)


def test_unverified_owner_requires_explicit_bookkeeping_cleanup(monkeypatch, tmp_path):
    cdp.CDPSessionManager.register("bank", 9333, str(tmp_path / "bank"), pid=101,
                                   tenant_id="tenant-a", chat_session_id="chat-a")
    monkeypatch.setattr(cdp, "_owned_browser_process", lambda session: (0, 0.0))
    denied = asyncio.run(cdp.browser_close_session({"work_key": "bank", "tenant_id": "tenant-b",
                                                    "chat_session_id": "chat-a", "force_cleanup": True}))
    assert denied["data"]["error_code"] == "CDP_SCOPE_MISMATCH"
    assert cdp.CDPSessionManager.get_session("bank") is not None
    recovered = asyncio.run(cdp.browser_close_session({"work_key": "bank", "tenant_id": "tenant-a",
                                                       "chat_session_id": "chat-a", "force_cleanup": True}))
    assert recovered["data"]["session_released"] is True
    assert recovered["data"]["process"]["attempted"] is False
    assert cdp.CDPSessionManager.get_session("bank") is None


def test_legacy_scope_requires_explicit_transition(monkeypatch, tmp_path):
    session = cdp.CDPSessionManager.register("bank", 9333, str(tmp_path / "bank"), pid=101)
    params = {"tenant_id": "tenant-a", "chat_session_id": "chat-a"}
    assert cdp._session_scope_error(session, params) == "CDP_LEGACY_SCOPE_REQUIRED"
    monkeypatch.setattr(cdp, "_owned_browser_process", lambda current: (101, 10.0))
    assert cdp._adopt_session_process(session, {**params, "adopt_legacy_scope": True})
    assert cdp._session_scope_error(session, params) == "CDP_LEGACY_SCOPE_REQUIRED"
    assert cdp.bind_legacy_session_scope("bank", profile_dir=session.profile_dir, **params)
    assert cdp._session_scope_error(session, params) == ""


def test_registry_survives_agent_restart(tmp_path):
    cdp.CDPSessionManager.register("bank", 9333, str(tmp_path / "bank"), pid=101,
                                   tenant_id="tenant-a", chat_session_id="chat-a")
    cdp.CDPSessionManager._sessions.clear()
    cdp.CDPSessionManager._loaded = False
    recovered = cdp.CDPSessionManager.get_session("bank")
    assert recovered is not None
    assert (recovered.port, recovered.tenant_id, recovered.chat_session_id) == (9333, "tenant-a", "chat-a")


def test_reused_pid_is_rejected_and_restarted_chrome_is_found(monkeypatch, tmp_path):
    session = cdp.CDPSessionManager.register("bank", 9333, str(tmp_path / "bank"), pid=101)
    session.process_started_at = 10.0

    def process(pid, started):
        return SimpleNamespace(pid=pid, info={"name": "chrome.exe", "create_time": started,
            "cmdline": ["chrome.exe", "--remote-debugging-port=9333",
                        f"--user-data-dir={tmp_path / 'bank'}"]})

    monkeypatch.setitem(sys.modules, "psutil", SimpleNamespace(
        process_iter=lambda attrs: [process(101, 20.0), process(202, 30.0)]))
    assert cdp._owned_browser_process(session) == (202, 30.0)


def test_unrelated_cdp_profile_cannot_be_claimed_by_url(monkeypatch, tmp_path):
    cdp.CDPSessionManager.register("bank", 9333, str(tmp_path / "bank"), pid=3)

    async def ready(port):
        return {"webSocketDebuggerUrl": "ws://localhost/browser"}

    monkeypatch.setattr(cdp, "_probe_cdp_version", ready)
    result = asyncio.run(cdp.browser_launch({"work_key": "bank", "port": 9333,
        "user_data_dir": str(tmp_path / "other"), "url": "https://bank.example/"}))
    assert result["data"]["error_code"] == "CDP_PROFILE_MISMATCH"


@pytest.mark.parametrize("command", [
    "browser_navigate", "browser_click", "browser_fill", "browser_press_key",
    "browser_select_option", "browser_check", "browser_file_upload",
    "browser_download", "browser_screenshot", "browser_get_text", "browser_eval",
    "browser_tabs", "browser_health", "browser_close_tab", "browser_close_session",
])
@pytest.mark.parametrize("access", ["work_key", "port", "wrong_work_key"])
def test_every_operation_denies_cross_scope_without_io(monkeypatch, tmp_path, command, access):
    cdp.CDPSessionManager.register("bank", 9333, str(tmp_path / "bank"),
        tenant_id="tenant-a", chat_session_id="chat-a")
    params = {"tenant_id": "tenant-b", "chat_session_id": "chat-a"}
    params.update({"port": 9333} if access == "port" else {"work_key": "bank"})
    if access == "wrong_work_key":
        params.update(work_key="unknown", port=9333)
    result = asyncio.run(getattr(cdp, command)(params))
    assert result["status"] == "error"
    assert result["data"]["error_code"] in {"CDP_SCOPE_MISMATCH", "CDP_PORT_MISMATCH"}


def test_remote_adoption_cannot_change_legacy_scope(monkeypatch, tmp_path):
    session = cdp.CDPSessionManager.register("bank", 9333, str(tmp_path / "bank"))
    monkeypatch.setattr(cdp, "_owned_browser_process", lambda _: (101, 10.0))
    for command in (cdp.browser_launch, cdp.browser_close_session, cdp.browser_tabs):
        result = asyncio.run(command({"work_key": "bank", "tenant_id": "foreign",
            "chat_session_id": "foreign", "adopt_legacy_scope": True}))
        assert result["data"]["error_code"] == "CDP_LEGACY_SCOPE_REQUIRED"
    assert session.tenant_id == session.chat_session_id == ""


def test_registered_profile_reused_without_explicit_profile(monkeypatch, tmp_path):
    session = cdp.CDPSessionManager.register("bank", 9333, str(tmp_path / "custom"), pid=101)
    monkeypatch.setattr(cdp, "_owned_browser_process", lambda _: (101, 10.0))
    async def probe(port):
        return {"webSocketDebuggerUrl": "ws://127.0.0.1:9333/devtools/browser/x"}
    monkeypatch.setattr(cdp, "_probe_cdp_version", probe)
    result = asyncio.run(cdp.browser_launch({"port": 9333}))
    assert result["status"] == "success"
    assert result["data"]["user_data_dir"] == session.profile_dir


def test_process_inspection_failure_fails_closed(monkeypatch, tmp_path):
    session = cdp.CDPSessionManager.register("bank", 9333, str(tmp_path / "bank"))
    def denied(*args):
        raise PermissionError("process is unavailable")
    monkeypatch.setitem(sys.modules, "psutil", SimpleNamespace(process_iter=denied))
    assert cdp._owned_browser_process(session) == (0, 0.0)
