"""Persisted owned-tab registry: restart recovery, pid proof and scope isolation."""
from __future__ import annotations

import asyncio
import json
import os
import sys
from functools import wraps

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "pc_agent"))
from commands import browser_auto as cdp
from commands import browser_reclaim as reclaim

BROWSER_WS = "ws://127.0.0.1:9222/devtools/browser/browser-1"
USER_TAB = {"id": "user-1", "type": "page", "url": "https://example.org/",
            "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/user-1"}


def run_async(func):
    @wraps(func)
    def wrapped(*args, **kwargs):
        return asyncio.run(func(*args, **kwargs))
    return wrapped


def _reset_memory():
    reclaim._OWNED.clear()
    reclaim._USED.clear()
    reclaim._QUARANTINED.clear()
    reclaim._PENDING.clear()
    reclaim._DISCARDED.clear()
    reclaim._CREATE_ATTEMPTS = 0
    cdp.CDPSessionManager._sessions.clear()
    cdp._OWNED_TAB_LOCKS.clear()


class FakeBrowser:
    def __init__(self):
        self.pages = [dict(USER_TAB)]
        self.pid_start = 1234.5

    async def version(self, _port):
        return {"webSocketDebuggerUrl": BROWSER_WS}

    async def list_pages(self, _port):
        return self.pages

    async def send(self, _ws, method, _params, **_kwargs):
        if method == "Target.createTarget":
            self.pages.append({"id": "own-1", "type": "page", "url": "about:blank",
                               "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/own-1"})
            return {"targetId": "own-1"}
        if method == "Target.getTargetInfo":
            return {"targetInfo": {"targetId": "own-1", "type": "page", "attached": False}}
        raise AssertionError(method)


@pytest.fixture
def env(monkeypatch, tmp_path):
    path = tmp_path / "owned_tabs.json"
    monkeypatch.setenv("AADS_OWNED_TABS_REGISTRY", str(path))
    _reset_memory()
    browser = FakeBrowser()
    monkeypatch.setattr(cdp, "_is_managed_profile_dir", lambda _p: True)
    monkeypatch.setattr(cdp, "_probe_cdp_version", browser.version)
    monkeypatch.setattr(cdp, "_list_cdp_targets", browser.list_pages)
    monkeypatch.setattr(cdp, "_send_cdp", browser.send)
    monkeypatch.setattr(reclaim, "_process_proof",
                        lambda pid, port: browser.pid_start if pid == 42 and port == 9222 else None)
    yield browser, path
    _reset_memory()


def _restart():
    """Simulate an agent restart: memory is gone, the file stays."""
    _reset_memory()
    return reclaim.load_registry()


async def _create(work_key="chat-pc-a", **extra):
    cdp.CDPSessionManager.register(work_key, 9222, "/managed/profile", pid=42)
    created = await reclaim.create({"work_key": work_key, "lease_seconds": 60, **extra})
    assert created["status"] == "success", created
    return created["data"]["owner_token"]


@run_async
async def test_saved_registry_is_recovered_after_restart(env):
    _browser, path = env
    token = await _create(tenant_id="t-1", chat_id="c-1")
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert [r["token"] for r in saved["records"]] == [token]
    assert set(saved["records"][0]) == {"token", "work_key", "port", "pid", "process_started", "target_id",
                                        "tenant_id", "chat_id", "created_at", "observed"}
    assert not [f for f in os.listdir(path.parent) if f.endswith(".tmp")]

    assert _restart() == 1 and not reclaim._OWNED
    cdp.CDPSessionManager.register("chat-pc-a", 9222, "/managed/profile", pid=42)
    result = await reclaim.recover_pending()
    assert result == {"adopted": 1, "discarded": 0, "deferred": 0}
    record = reclaim._OWNED[token]
    assert (record.target_id, record.work_key, record.tenant_id, record.chat_id) == ("own-1", "chat-pc-a", "t-1", "c-1")
    assert record.session_generation == cdp.CDPSessionManager.get_session("chat-pc-a").generation
    found = await reclaim.lookup({"work_key": "chat-pc-a", "tenant_id": "t-1", "chat_id": "c-1", "owner_token": token})
    assert found["status"] == "success" and found["data"]["target_id"] == "own-1"


@run_async
async def test_pid_mismatch_is_discarded_not_adopted(env):
    browser, path = env
    token = await _create()
    _restart()
    browser.pid_start = 9999.0  # pid reused by a different process / browser relaunched
    result = await reclaim.recover_pending()
    assert result["adopted"] == 0 and result["discarded"] == 1
    assert token not in reclaim._OWNED and not reclaim._PENDING
    assert reclaim._DISCARDED[-1]["reason"] == "process_mismatch"
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["records"] == [] and saved["discarded"][-1]["token"] == token


@run_async
async def test_vanished_target_is_discarded(env):
    browser, _path = env
    await _create()
    _restart()
    browser.pages = [dict(USER_TAB)]
    result = await reclaim.recover_pending()
    assert result["discarded"] == 1 and not reclaim._OWNED
    assert reclaim._DISCARDED[-1]["reason"] == "target_missing"


@run_async
async def test_other_work_key_tenant_or_chat_is_rejected_explicitly(env):
    token = await _create(tenant_id="t-1", chat_id="c-1")
    base = {"owner_token": token, "work_key": "chat-pc-a", "tenant_id": "t-1", "chat_id": "c-1"}
    assert (await reclaim.lookup(base))["status"] == "success"
    for override in ({"work_key": "chat-pc-b"}, {"tenant_id": "t-2"}, {"chat_id": "c-2"}, {"chat_id": ""}):
        outcome = await reclaim.lookup({**base, **override})
        assert outcome == {"status": "error", "data": {"reason": "owner_scope_mismatch"}}
    by_target = await reclaim.lookup({"work_key": "chat-pc-b", "target_id": "own-1"})
    assert by_target["status"] == "error" and by_target["data"]["reason"] == "owner_scope_mismatch"
    cross = await reclaim.reclaim({"owner_token": token, "work_key": "chat-pc-b"})
    assert cross == {"status": "error", "data": {"reason": "owner_scope_mismatch"}}
    missing = await reclaim.lookup({"work_key": "chat-pc-a"})
    assert missing["data"]["reason"] == "scope_and_owner_required"


@run_async
async def test_user_tab_is_never_adopted(env):
    browser, path = env
    await _create()
    _restart()
    # A forged/stale file entry pointing at the user's tab must still not be trusted via listing alone.
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert all(r["target_id"] != "user-1" for r in saved["records"])
    await reclaim.recover_pending()
    assert {r.target_id for r in reclaim._OWNED.values()} == {"own-1"}
    outcome = await reclaim.lookup({"work_key": "chat-pc-a", "target_id": "user-1"})
    assert outcome == {"status": "error", "data": {"reason": "target_not_owned"}}
    browser.pages = [dict(USER_TAB)]
    _restart()
    await reclaim.recover_pending()
    assert not reclaim._OWNED


@run_async
async def test_corrupt_registry_is_handled_safely(env):
    _browser, path = env
    path.write_text("{not json", encoding="utf-8")
    assert reclaim.load_registry() == 0
    assert not reclaim._PENDING and (path.parent / (path.name + ".corrupt")).exists()
    assert await reclaim.recover_pending() == {"adopted": 0, "discarded": 0, "deferred": 0}
    token = await _create()
    assert json.loads(path.read_text(encoding="utf-8"))["records"][0]["token"] == token


@run_async
async def test_invalid_entries_are_dropped_and_secrets_not_loaded(env):
    _browser, path = env
    good = {"token": "a" * 8, "work_key": "chat-pc-a", "port": 9222, "pid": 42, "process_started": 1234.5,
            "target_id": "own-1", "tenant_id": "", "chat_id": "", "created_at": 1.0, "cookie": "SECRET"}
    bad = [dict(good, token="c", port=True), dict(good, token="d", pid=-1),
           dict(good, token="e", target_id=""), "junk"]
    path.write_text(json.dumps({"version": 1, "records": [good, *bad]}), encoding="utf-8")
    assert reclaim.load_registry() == 1
    assert "cookie" not in reclaim._PENDING["a" * 8]
    await reclaim.recover_pending()
    assert "SECRET" not in path.read_text(encoding="utf-8")


@run_async
async def test_forget_and_observed_state_are_persisted(env):
    _browser, path = env
    token = await _create()
    reclaim.mark_observed("own-1")
    assert json.loads(path.read_text(encoding="utf-8"))["records"][0]["observed"] is True
    _restart()
    await reclaim.recover_pending()
    assert "own-1" in reclaim._USED
    reclaim._forget(reclaim._OWNED[token])
    assert json.loads(path.read_text(encoding="utf-8"))["records"] == []


@run_async
async def test_cdp_outage_defers_without_discarding(env, monkeypatch):
    await _create()
    _restart()

    async def down(_port):
        raise OSError("cdp down")

    monkeypatch.setattr(cdp, "_probe_cdp_version", down)
    assert await reclaim.recover_pending() == {"adopted": 0, "discarded": 0, "deferred": 1}
    assert len(reclaim._PENDING) == 1 and not reclaim._DISCARDED


@run_async
async def test_reclaim_without_token_only_sees_the_callers_scope(env):
    token = await _create(tenant_id="t-1", chat_id="c-1")
    for other in ({"work_key": "chat-pc-b"}, {"work_key": "chat-pc-a"},
                  {"work_key": "chat-pc-a", "tenant_id": "t-2", "chat_id": "c-1"},
                  {"work_key": "chat-pc-a", "tenant_id": "t-1", "chat_id": "c-2"}):
        outcome = await reclaim.reclaim(other)
        assert outcome["data"]["processed"] == 0 and token not in json.dumps(outcome)
    for unscoped in ({}, {"tenant_id": "t-1", "chat_id": "c-1"}, {"dry_run": False}):
        assert await reclaim.reclaim(unscoped) == {"status": "error", "data": {"reason": "scope_and_owner_required"}}
    own = await reclaim.reclaim({"work_key": "chat-pc-a", "tenant_id": "t-1", "chat_id": "c-1"})
    assert [r["owner_token"] for r in own["data"]["results"]] == [token]
    omitted = await reclaim.reclaim({"owner_token": token, "work_key": "chat-pc-a"})
    assert omitted == {"status": "error", "data": {"reason": "owner_scope_mismatch"}}


@run_async
async def test_create_rejects_work_keys_that_restore_would_drop(env):
    for bad in ("", None, "k" * 257, 7):
        cdp.CDPSessionManager.register(bad if isinstance(bad, str) else "x", 9222, "/managed/profile", pid=42)
        created = await reclaim.create({"work_key": bad, "lease_seconds": 60})
        assert created == {"status": "error", "data": {"reason": "invalid_scope"}}
    assert not reclaim._OWNED
    token = await _create("k" * 256)
    assert _restart() == 1
    assert token in reclaim._PENDING


@run_async
async def test_unverifiable_process_defers_instead_of_discarding(env, monkeypatch):
    browser, _path = env
    token = await _create()
    _restart()
    monkeypatch.setattr(reclaim, "_process_proof", lambda _pid, _port: reclaim._PROOF_UNAVAILABLE)
    assert await reclaim.recover_pending() == {"adopted": 0, "discarded": 0, "deferred": 1}
    assert token in reclaim._PENDING and not reclaim._DISCARDED
    monkeypatch.setattr(reclaim, "_process_proof", lambda pid, port: browser.pid_start)
    assert await reclaim.recover_pending() == {"adopted": 1, "discarded": 0, "deferred": 0}
    assert token in reclaim._OWNED


@run_async
async def test_create_refuses_when_process_proof_is_unavailable(env, monkeypatch):
    browser, _path = env
    calls = []
    original_send = browser.send

    async def spy(ws, method, params, **kwargs):
        calls.append(method)
        return await original_send(ws, method, params, **kwargs)

    monkeypatch.setattr(cdp, "_send_cdp", spy)
    monkeypatch.setattr(reclaim, "_process_proof", lambda _pid, _port: reclaim._PROOF_UNAVAILABLE)
    cdp.CDPSessionManager.register("chat-pc-a", 9222, "/managed/profile", pid=42)
    before = reclaim._CREATE_ATTEMPTS
    created = await reclaim.create({"work_key": "chat-pc-a", "lease_seconds": 60})
    assert created == {"status": "error", "data": {"reason": "process_proof_unavailable"}}
    assert "Target.createTarget" not in calls
    assert reclaim._CREATE_ATTEMPTS == before
    assert not reclaim._OWNED and not reclaim._QUARANTINED
    assert [p["id"] for p in browser.pages] == ["user-1"]


def test_process_proof_separates_lookup_failure_from_mismatch(monkeypatch):
    import types

    class Error(Exception):
        pass

    class NoSuchProcess(Error):
        pass

    class AccessDenied(Error):
        pass

    failure = {"exc": AccessDenied}

    def process(_pid):
        raise failure["exc"]()

    fake = types.SimpleNamespace(Error=Error, NoSuchProcess=NoSuchProcess, Process=process, CONN_LISTEN="LISTEN")
    monkeypatch.setattr(reclaim, "psutil", fake)
    assert reclaim._process_proof(1, 9222) is reclaim._PROOF_UNAVAILABLE
    failure["exc"] = NoSuchProcess
    assert reclaim._process_proof(1, 9222) is None
    assert reclaim._process_proof(0, 9222) is None
    monkeypatch.setattr(reclaim, "psutil", None)
    assert reclaim._process_proof(1, 9222) is reclaim._PROOF_UNAVAILABLE


@run_async
async def test_observation_before_recovery_survives_restart(env):
    _browser, path = env
    token = await _create()
    _restart()
    reclaim.mark_observed("own-1")
    assert json.loads(path.read_text(encoding="utf-8"))["records"][0]["observed"] is True
    await reclaim.recover_pending()
    assert reclaim._OWNED[token].target_id in reclaim._USED
    assert json.loads(path.read_text(encoding="utf-8"))["records"][0]["observed"] is True


@run_async
async def test_lookup_rejects_owned_record_whose_proof_is_gone(env):
    browser, _path = env
    token = await _create()
    query = {"work_key": "chat-pc-a", "owner_token": token}
    assert (await reclaim.lookup(query))["status"] == "success"
    browser.pid_start = 9999.0
    assert (await reclaim.lookup(query)) == {"status": "error", "data": {"reason": "process_changed"}}
    browser.pid_start = 1234.5
    browser.pages = [dict(USER_TAB)]
    gone = await reclaim.lookup(query)
    assert gone["status"] == "error" and "own-1" not in json.dumps(gone)
