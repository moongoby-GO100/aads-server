"""Chrome shaped CDP responses and fail-closed owned-tab reclamation."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import types
from dataclasses import replace
from functools import wraps

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "pc_agent"))
from commands import browser_auto as cdp
from commands import browser_reclaim as reclaim
from commands import browser_tab

_real_process_proof = reclaim._process_proof


def run_async(func):
    @wraps(func)
    def wrapped(*args, **kwargs):
        return asyncio.run(func(*args, **kwargs))
    return wrapped


@pytest.fixture(autouse=True)
def reset(monkeypatch):
    cdp.CDPSessionManager._sessions.clear()
    cdp._OWNED_TAB_LOCKS.clear()
    reclaim._OWNED.clear()
    reclaim._USED.clear()
    reclaim._QUARANTINED.clear()
    reclaim._SWEEP_CURSOR = 0
    reclaim._SWEEP_WINDOW = 0
    reclaim._SWEEP_COUNT = 0
    reclaim._CREATE_ATTEMPTS = 0
    browser_tab._SESSIONS.clear()
    monkeypatch.setattr(cdp, "_is_managed_profile_dir", lambda _path: True)
    monkeypatch.setattr(reclaim, "_process_proof", lambda pid, port: 1234.5 if pid == 42 and port == 9222 else None)
    monkeypatch.setenv("AADS_SAFE_TAB_RECLAIM_ENABLED", "1")
    yield
    cdp.CDPSessionManager._sessions.clear()
    cdp._OWNED_TAB_LOCKS.clear()
    reclaim._OWNED.clear()
    reclaim._USED.clear()
    reclaim._QUARANTINED.clear()
    reclaim._SWEEP_CURSOR = 0
    reclaim._SWEEP_WINDOW = 0
    reclaim._SWEEP_COUNT = 0
    reclaim._CREATE_ATTEMPTS = 0
    browser_tab._SESSIONS.clear()


@run_async
async def test_chrome_json_without_attached_field_registers_and_reclaims(monkeypatch):
    browser_ws = "ws://127.0.0.1:9222/devtools/browser/browser-1"
    target_ws = "ws://127.0.0.1:9222/devtools/page/new-1"
    # Chrome's /json page listing has no attached field.
    pages = [{"id": "new-1", "type": "page", "url": "about:blank",
              "webSocketDebuggerUrl": target_ws}]
    calls = []

    async def version(_port):
        return {"webSocketDebuggerUrl": browser_ws}

    async def list_pages(_port):
        return pages

    async def send(_ws, method, _params, **_kwargs):
        calls.append(method)
        if method == "Target.createTarget":
            assert _params == {"url": "about:blank", "background": True}
            return {"targetId": "new-1"}
        if method == "Target.getTargetInfo":
            return {"targetInfo": {"targetId": "new-1", "type": "page", "attached": False}}
        if method == "Runtime.evaluate":
            return {"result": {"type": "object", "value": {
                "visible": False, "focused": False, "editing": False,
                "downloading": False, "url": "about:blank"}}}
        if method == "Target.closeTarget":
            return {"success": True}
        raise AssertionError(method)

    monkeypatch.setattr(cdp, "_probe_cdp_version", version)
    monkeypatch.setattr(cdp, "_list_cdp_targets", list_pages)
    monkeypatch.setattr(cdp, "_send_cdp", send)
    async def guarded(_record):
        calls.append("Target.closeTarget")
        return "closed"
    monkeypatch.setattr(reclaim, "_guarded_close", guarded)
    session = cdp.CDPSessionManager.register("job-1", 9222, "/managed/profile", pid=42)
    created = await reclaim.create({"work_key": "job-1", "lease_seconds": 60})
    assert created["status"] == "success"
    token = created["data"]["owner_token"]
    reclaim._OWNED[token] = replace(reclaim._OWNED[token], lease_until=0)
    session.last_heartbeat_at = 0
    outcome = await reclaim.reclaim({"owner_token": token, "dry_run": False})
    assert outcome["data"]["results"][0]["result"] == "closed"
    assert token not in reclaim._OWNED
    assert calls.count("Target.getTargetInfo") == 2
    assert calls.count("Target.closeTarget") == 1


@run_async
async def test_user_active_tab_is_never_closed(monkeypatch):
    browser_ws = "ws://127.0.0.1:9222/devtools/browser/browser-1"
    target_ws = "ws://127.0.0.1:9222/devtools/page/new-1"
    calls = []
    navigated = False

    async def version(_port):
        return {"webSocketDebuggerUrl": browser_ws}

    async def list_pages(_port):
        return [{"id": "new-1", "type": "page", "url": "https://example.org/form" if navigated else "about:blank",
                 "webSocketDebuggerUrl": target_ws}]

    async def send(_ws, method, params, **_kwargs):
        calls.append(method)
        if method == "Target.createTarget":
            return {"targetId": "new-1"}
        if method == "Target.getTargetInfo":
            return {"targetInfo": {"targetId": "new-1", "type": "page", "attached": False}}
        if method == "Runtime.evaluate":
            assert "document.hasFocus()" in params["expression"]
            assert "[contenteditable]:focus" in params["expression"]
            return {"result": {"type": "object", "value": {
                "visible": True, "focused": True, "editing": True,
                "downloading": False, "url": "https://example.org/form"}}}
        if method == "Target.closeTarget":
            raise AssertionError("active user tab must not close")
        raise AssertionError(method)

    monkeypatch.setattr(cdp, "_probe_cdp_version", version)
    monkeypatch.setattr(cdp, "_list_cdp_targets", list_pages)
    monkeypatch.setattr(cdp, "_send_cdp", send)
    session = cdp.CDPSessionManager.register("job-1", 9222, "/managed/profile", pid=42)
    created = await reclaim.create({"work_key": "job-1"})
    assert created["status"] == "success"
    navigated = True
    token = created["data"]["owner_token"]
    session.last_heartbeat_at = 0
    reclaim._OWNED[token] = replace(reclaim._OWNED[token], lease_until=0)
    outcome = await reclaim.reclaim({"owner_token": token, "dry_run": False})
    assert outcome["data"]["results"][0]["result"] == "user_active_or_unknown"
    assert token in reclaim._OWNED
    assert "Target.closeTarget" not in calls


@run_async
async def test_close_must_confirm_success_and_pass_is_capped(monkeypatch):
    browser_ws = "ws://127.0.0.1:9222/devtools/browser/browser-1"
    target_ws = "ws://127.0.0.1:9222/devtools/page/new-1"
    for index in range(3):
        token = f"owner-{index}"
        reclaim._OWNED[token] = reclaim.OwnedTab(
            token, f"epoch-{index}", "job-1", 9222, 42, 1234.5,
            browser_ws, f"new-{index}", target_ws, 0, 0,
        )

    async def eligible(_record):
        return "eligible"

    async def close(_ws, method, _params, **_kwargs):
        assert method == "Target.closeTarget"
        return {"success": False}

    monkeypatch.setattr(reclaim, "_reclaim_reason", eligible)
    async def unconfirmed(_record):
        assert (await close(None, "Target.closeTarget", {}))["success"] is False
        return "close_not_confirmed"
    monkeypatch.setattr(reclaim, "_guarded_close", unconfirmed)
    result = await reclaim.reclaim({"dry_run": False})
    assert result["data"]["processed"] == 2
    assert [entry["result"] for entry in result["data"]["results"]] == ["close_not_confirmed"] * 2
    assert len(reclaim._OWNED) == 3
    assert (await reclaim.reclaim({"work_key": "other", "dry_run": False}))["data"]["processed"] == 0


@run_async
async def test_default_gates_and_observed_tab(monkeypatch):
    actual_reason = reclaim._reclaim_reason
    token = "owner-1"
    reclaim._OWNED[token] = reclaim.OwnedTab(
        token, "epoch-1", "job-1", 9222, 42, 1234.5,
        "ws://127.0.0.1:9222/devtools/browser/one", "tab-1",
        "ws://127.0.0.1:9222/devtools/page/tab-1", 0, 0,
    )
    async def eligible(_record):
        return "eligible"
    async def should_not_close(*_args, **_kwargs):
        raise AssertionError("close must be gated")
    monkeypatch.setattr(reclaim, "_reclaim_reason", eligible)
    monkeypatch.setattr(cdp, "_send_cdp", should_not_close)
    monkeypatch.delenv("AADS_SAFE_TAB_RECLAIM_ENABLED")
    assert (await reclaim.reclaim({"dry_run": False}))["data"]["results"][0]["result"] == "disabled"
    monkeypatch.setenv("AADS_SAFE_TAB_RECLAIM_ENABLED", "1")
    assert (await reclaim.reclaim({}))["data"]["results"][0]["result"] == "dry_run"
    reclaim.mark_observed("tab-1")
    monkeypatch.setattr(reclaim, "_reclaim_reason", actual_reason)
    reclaim._SWEEP_WINDOW = 0  # next sweep window
    assert (await reclaim.reclaim({"dry_run": False}))["data"]["results"][0]["result"] == "user_observed"


@run_async
async def test_invalid_lease_and_failed_proof_cleanup(monkeypatch):
    browser_ws = "ws://127.0.0.1:9222/devtools/browser/one"
    cdp.CDPSessionManager.register("job-1", 9222, "/managed/profile", pid=42)
    calls = []
    async def version(_port):
        return {"webSocketDebuggerUrl": browser_ws}
    async def pages(_port):
        return []
    async def send(_ws, method, _params, **_kwargs):
        calls.append(method)
        return {"targetId": "new-1"} if method == "Target.createTarget" else {"success": True}
    monkeypatch.setattr(cdp, "_probe_cdp_version", version)
    monkeypatch.setattr(cdp, "_list_cdp_targets", pages)
    monkeypatch.setattr(cdp, "_send_cdp", send)
    assert (await reclaim.create({"work_key": "job-1", "lease_seconds": "bad"}))["data"]["reason"] == "invalid_lease"
    assert calls == []
    result = await reclaim.create({"work_key": "job-1"})
    assert result["data"]["reason"] == "creation_proof_failed"
    assert result["data"]["cleanup"] == "quarantined"
    assert reclaim._QUARANTINED[(9222, "new-1")] == "creation_proof_failed"
    assert calls == ["Target.createTarget"]
    assert not reclaim._OWNED


@run_async
async def test_command_failure_does_not_mark_session_healthy(monkeypatch):
    session = cdp.CDPSessionManager.register("job-1", 9222, "/managed/profile", pid=42)
    session.last_heartbeat_at = 1
    session.last_error_code = "prior_error"
    async def fail(*_args, **_kwargs):
        raise TimeoutError("slow command")
    monkeypatch.setattr(cdp, "_send_cdp_command_unlocked", fail)
    with pytest.raises(TimeoutError):
        await cdp._send_cdp_command(9222, "Page.navigate", timeout_seconds=1)
    assert session.last_heartbeat_at == 1
    assert session.last_error_code == "prior_error"


@run_async
async def test_different_ports_do_not_block_each_other(monkeypatch):
    entered = asyncio.Event()
    release = asyncio.Event()
    async def send(port, *_args, **_kwargs):
        if port == 9222:
            entered.set()
            await release.wait()
        return {"port": port}
    monkeypatch.setattr(cdp, "_send_cdp_command_unlocked", send)
    slow = asyncio.create_task(cdp._send_cdp_command(9222, "Page.navigate", timeout_seconds=5))
    await entered.wait()
    try:
        other = await asyncio.wait_for(cdp._send_cdp_command(9333, "Page.navigate", timeout_seconds=1), 1)
        assert other["port"] == 9333
    finally:
        release.set()
        await slow


@run_async
async def test_tab_open_waits_for_reclaim_final_check(monkeypatch):
    monkeypatch.setitem(sys.modules, "websockets", types.ModuleType("websockets"))
    cdp.CDPSessionManager.register("chat-pc-one", 9222, "/managed/profile", pid=42)
    token = "owner-1"
    reclaim._OWNED[token] = reclaim.OwnedTab(
        token, "epoch-1", "chat-pc-one", 9222, 42, 1234.5,
        "ws://127.0.0.1:9222/devtools/browser/one", "tab-1",
        "ws://127.0.0.1:9222/devtools/page/tab-1", 0, 0,
    )
    entered = asyncio.Event()
    release = asyncio.Event()
    async def check(_record):
        entered.set()
        await release.wait()
        return "eligible"
    async def pages(_port):
        return [{"id": "tab-1", "type": "page", "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/tab-1"}]
    class Ws:
        async def close(self):
            pass
    async def connect(*_args, **_kwargs):
        return Ws()
    monkeypatch.setattr(reclaim, "_reclaim_reason", check)
    monkeypatch.setattr(cdp, "_list_cdp_targets", pages)
    monkeypatch.setattr(cdp, "_connect_cdp_ws", connect)
    sweep = asyncio.create_task(reclaim.reclaim({}))
    await entered.wait()
    opening = asyncio.create_task(browser_tab._open({"port": 9222, "work_key": "chat-pc-one"}))
    await asyncio.sleep(0)
    assert not opening.done()
    release.set()
    await sweep
    result = await asyncio.wait_for(opening, 1)
    assert result["target_id"] == "tab-1"
    assert "tab-1" in reclaim._USED


def test_process_proof_supports_psutil_before_v6(monkeypatch):
    class Listener:
        status = "LISTEN"
        laddr = types.SimpleNamespace(port=9222, ip="127.0.0.1")
    class Process:
        def __init__(self, pid):
            assert pid == 42
        def connections(self, *, kind):
            assert kind == "tcp"
            return [Listener()]
        def create_time(self):
            return 1234.5
    monkeypatch.setattr(reclaim, "psutil", types.SimpleNamespace(Process=Process, CONN_LISTEN="LISTEN", Error=OSError))
    assert _real_process_proof(42, 9222) == 1234.5


@run_async
async def test_missing_session_activity_fields_fail_closed_per_record(monkeypatch):
    class OldSession:
        port = 9222
        pid = 42
        profile_dir = "/managed/profile"
    token = "owner-1"
    reclaim._OWNED[token] = reclaim.OwnedTab(
        token, "epoch-1", "job-1", 9222, 42, 1234.5,
        "ws://127.0.0.1:9222/devtools/browser/one", "tab-1",
        "ws://127.0.0.1:9222/devtools/page/tab-1", 0, 0,
    )
    monkeypatch.setattr(cdp.CDPSessionManager, "get_session", lambda _key: OldSession())
    result = await reclaim.reclaim({"dry_run": False})
    assert result["data"]["results"][0]["result"] == "session_or_command_active"
    assert token in reclaim._OWNED


@run_async
async def test_command_marks_owned_target_before_releasing_port_lock(monkeypatch):
    session = cdp.CDPSessionManager.register("job-1", 9222, "/managed/profile", pid=42)
    session.last_heartbeat_at = 0
    record = reclaim.OwnedTab(
        "owner-1", "epoch-1", "job-1", 9222, 42, 1234.5,
        "ws://127.0.0.1:9222/devtools/browser/one", "tab-1",
        "ws://127.0.0.1:9222/devtools/page/tab-1", 0, 0,
    )
    reclaim._OWNED[record.token] = record
    entered = asyncio.Event()
    release = asyncio.Event()

    async def command(*_args, **_kwargs):
        entered.set()
        await release.wait()
        return {"_target": {"id": "tab-1", "url": "about:blank"}}

    async def close(*_args, **_kwargs):
        raise AssertionError("a just used target must not close")

    monkeypatch.setattr(cdp, "_send_cdp_command_unlocked", command)
    monkeypatch.setattr(cdp, "_send_cdp", close)
    running = asyncio.create_task(cdp._send_cdp_command(9222, "Runtime.evaluate", timeout_seconds=1))
    await entered.wait()
    sweeping = asyncio.create_task(reclaim.reclaim({"owner_token": record.token, "dry_run": False}))
    await asyncio.sleep(0)
    assert not sweeping.done()
    release.set()
    await running
    result = await sweeping
    assert result["data"]["results"][0]["result"] == "user_observed"
    assert session.last_target_id == "tab-1"
    assert session.last_heartbeat_at > 0


@run_async
async def test_prior_used_tab_stays_protected_after_last_target_changes(monkeypatch):
    session = cdp.CDPSessionManager.register("job-1", 9222, "/managed/profile", pid=42)
    record = reclaim.OwnedTab(
        "owner-1", "epoch-1", "job-1", 9222, 42, 1234.5,
        "ws://127.0.0.1:9222/devtools/browser/one", "tab-1",
        "ws://127.0.0.1:9222/devtools/page/tab-1", 0, 0,
    )
    reclaim._OWNED[record.token] = record

    async def command(_port, _method, _params, *, target_id, **_kwargs):
        return {"_target": {"id": target_id, "url": "about:blank"}}

    monkeypatch.setattr(cdp, "_send_cdp_command_unlocked", command)
    await cdp._send_cdp_command(9222, "Page.captureScreenshot", timeout_seconds=1, target_id="tab-1")
    await cdp._send_cdp_command(9222, "Page.captureScreenshot", timeout_seconds=1, target_id="tab-2")
    assert session.last_target_id == "tab-2"
    session.last_heartbeat_at = 0
    result = await reclaim.reclaim({"owner_token": record.token, "dry_run": False})
    assert result["data"]["results"][0]["result"] == "user_observed"
    assert record.token in reclaim._OWNED


@run_async
async def test_closed_records_pruned_and_uncertain_inventory_hits_cap(monkeypatch):
    browser_ws = "ws://127.0.0.1:9222/devtools/browser/one"
    target_ws = "ws://127.0.0.1:9222/devtools/page/new-1"
    cdp.CDPSessionManager.register("job-1", 9222, "/managed/profile", pid=42)
    for index in range(reclaim._MAX_OWNED):
        token = f"owner-{index}"
        reclaim._OWNED[token] = reclaim.OwnedTab(
            token, f"epoch-{index}", "job-1", 9222, 42, 1234.5,
            browser_ws, f"closed-{index}", target_ws, 0, 0,
        )

    async def version(_port):
        return {"webSocketDebuggerUrl": browser_ws}

    async def pages(_port):
        return [{"id": "new-1", "type": "page", "url": "about:blank",
                 "webSocketDebuggerUrl": target_ws}]

    async def send(_ws, method, _params, **_kwargs):
        if method == "Target.getTargets":
            return {"targetInfos": [{"targetId": "new-1", "type": "page"}]}
        if method == "Target.createTarget":
            return {"targetId": "new-1"}
        if method == "Target.getTargetInfo":
            return {"targetInfo": {"targetId": "new-1", "type": "page", "attached": False}}
        raise AssertionError(method)

    monkeypatch.setattr(cdp, "_probe_cdp_version", version)
    monkeypatch.setattr(cdp, "_list_cdp_targets", pages)
    monkeypatch.setattr(cdp, "_send_cdp", send)
    created = await reclaim.create({"work_key": "job-1"})
    assert created["status"] == "success"
    assert len(reclaim._OWNED) == 1

    async def uncertain(*_args, **_kwargs):
        raise TimeoutError("inventory unavailable")

    for index in range(reclaim._MAX_OWNED - 1):
        token = f"owner-new-{index}"
        reclaim._OWNED[token] = replace(next(iter(reclaim._OWNED.values())), token=token)
    monkeypatch.setattr(cdp, "_send_cdp", uncertain)
    blocked = await reclaim.create({"work_key": "job-1"})
    assert blocked["data"]["reason"] == "ownership_capacity_reached"
    assert len(reclaim._OWNED) == reclaim._MAX_OWNED


class GuardedSocket:
    def __init__(self, *, change_at="", before_close=None):
        self.pending = []
        self.methods = []
        self.change_at = change_at
        self.before_close = before_close

    async def send(self, raw):
        message = json.loads(raw)
        method = message["method"]
        self.methods.append(method)
        if method == self.change_at:
            self.pending.append(json.dumps({"method": "Page.frameStartedNavigating", "sessionId": "s1"}))
        if method == "Target.closeTarget" and self.before_close:
            self.before_close()
        result = {
            "Target.attachToTarget": {"sessionId": "s1"},
            "Runtime.evaluate": {"result": {"type": "object", "value": {
                "visible": False, "focused": False, "editing": False,
                "downloading": False, "url": "about:blank"}}},
            "Target.getTargetInfo": {"targetInfo": {
                "targetId": "tab-1", "type": "page", "url": "about:blank"}},
            "Target.closeTarget": {"success": True},
        }.get(method, {})
        self.pending.append(json.dumps({"id": message["id"], "result": result}))

    async def recv(self):
        return self.pending.pop(0)

    async def close(self):
        pass


def guarded_record():
    session = cdp.CDPSessionManager.register("job-1", 9222, "/managed/profile", pid=42)
    session.last_heartbeat_at = 0
    record = reclaim.OwnedTab(
        "owner-1", "epoch-1", "job-1", 9222, 42, 1234.5,
        "ws://127.0.0.1:9222/devtools/browser/one", "tab-1",
        "ws://127.0.0.1:9222/devtools/page/tab-1", 0, 0, session.generation,
    )
    reclaim._OWNED[record.token] = record
    return record


@run_async
async def test_navigation_between_final_check_and_close_aborts(monkeypatch):
    record = guarded_record()
    socket = GuardedSocket(change_at="Target.getTargetInfo")
    monkeypatch.setitem(sys.modules, "websockets", types.ModuleType("websockets"))
    async def connect(*_args, **_kwargs):
        return socket
    monkeypatch.setattr(cdp, "_connect_cdp_ws", connect)
    assert await reclaim._guarded_close(record) == "target_changed"
    assert "Target.closeTarget" not in socket.methods
    assert record.token in reclaim._OWNED


@run_async
async def test_navigation_during_close_response_is_not_confirmed(monkeypatch):
    record = guarded_record()
    socket = GuardedSocket(change_at="Target.closeTarget")
    monkeypatch.setitem(sys.modules, "websockets", types.ModuleType("websockets"))
    async def connect(*_args, **_kwargs):
        return socket
    monkeypatch.setattr(cdp, "_connect_cdp_ws", connect)
    assert await reclaim._guarded_close(record) == "changed_during_close"
    assert socket.methods.count("Target.closeTarget") == 1
    assert record.token in reclaim._OWNED


@run_async
async def test_creation_capacity_recovers_after_confirmed_cleanup(monkeypatch):
    browser_ws = "ws://127.0.0.1:9222/devtools/browser/one"
    target_ws = "ws://127.0.0.1:9222/devtools/page/new-1"
    cdp.CDPSessionManager.register("job-1", 9222, "/managed/profile", pid=42)
    current = {"id": ""}
    async def version(_port):
        return {"webSocketDebuggerUrl": browser_ws}
    async def pages(_port):
        return ([{"id": current["id"], "type": "page", "url": "about:blank",
                  "webSocketDebuggerUrl": target_ws}] if current["id"] else [])
    async def send(_ws, method, _params, **_kwargs):
        if method == "Target.getTargets":
            return {"targetInfos": ([{"targetId": current["id"], "type": "page"}]
                                    if current["id"] else [])}
        if method == "Target.createTarget":
            current["id"] = "new-1"
            return {"targetId": current["id"]}
        if method == "Target.getTargetInfo":
            return {"targetInfo": {"targetId": current["id"], "type": "page", "attached": False}}
        raise AssertionError(method)
    monkeypatch.setattr(cdp, "_probe_cdp_version", version)
    monkeypatch.setattr(cdp, "_list_cdp_targets", pages)
    monkeypatch.setattr(cdp, "_send_cdp", send)
    for _ in range(reclaim._MAX_OWNED + 1):
        created = await reclaim.create({"work_key": "job-1"})
        assert created["status"] == "success"
        current["id"] = ""
    assert reclaim._CREATE_ATTEMPTS == 1
    assert len(reclaim._OWNED) == 1


@run_async
async def test_quarantine_capacity_recovers_only_after_target_disappears(monkeypatch):
    browser_ws = "ws://127.0.0.1:9222/devtools/browser/one"
    cdp.CDPSessionManager.register("job-1", 9222, "/managed/profile", pid=42)
    reclaim._QUARANTINED[(9222, "lost-tab")] = "creation_proof_failed"
    reclaim._CREATE_ATTEMPTS = reclaim._MAX_OWNED
    async def version(_port):
        return {"webSocketDebuggerUrl": browser_ws}
    async def pages(_port):
        return []
    async def send(_ws, method, _params, **_kwargs):
        if method == "Target.getTargets":
            return {"targetInfos": []}
        if method == "Target.createTarget":
            return {"targetId": "new-1"}
        raise AssertionError(method)
    monkeypatch.setattr(cdp, "_probe_cdp_version", version)
    monkeypatch.setattr(cdp, "_list_cdp_targets", pages)
    monkeypatch.setattr(cdp, "_send_cdp", send)
    result = await reclaim.create({"work_key": "job-1"})
    assert result["data"]["reason"] == "creation_proof_failed"
    assert (9222, "lost-tab") not in reclaim._QUARANTINED
    assert reclaim._CREATE_ATTEMPTS == reclaim._MAX_OWNED


@run_async
async def test_session_generation_changes_before_close_aborts(monkeypatch):
    record = guarded_record()
    socket = GuardedSocket()
    monkeypatch.setitem(sys.modules, "websockets", types.ModuleType("websockets"))
    async def connect(*_args, **_kwargs):
        return socket
    original_send = socket.send
    async def send(raw):
        message = json.loads(raw)
        await original_send(raw)
        if message["method"] == "Target.getTargetInfo":
            cdp.CDPSessionManager.register("job-1", 9222, "/managed/profile", pid=42)
    socket.send = send
    monkeypatch.setattr(cdp, "_connect_cdp_ws", connect)
    assert await reclaim._guarded_close(record) == "ownership_or_session_changed"
    assert "Target.closeTarget" not in socket.methods
    assert record.token in reclaim._OWNED
