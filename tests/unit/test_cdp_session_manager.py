"""CDPSessionManager 단위 테스트 — PC Agent 멀티서비스 세션 격리."""
import sys
import os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "pc_agent", "commands"))

from browser_auto import (
    CDPCommandGuardManager,
    CDPSessionManager,
    browser_launch,
    browser_navigate,
    browser_close_session,
    _bank_work_key_port_matches_url,
    _default_profile_root,
    _effective_port,
)


class TestCDPSessionManager:
    def setup_method(self):
        CDPSessionManager._sessions.clear()

    def test_allocate_port_new(self):
        port = CDPSessionManager.allocate_port("vvic-sourcing")
        assert port == 9222

    def test_allocate_port_existing(self):
        CDPSessionManager.register("vvic-sourcing", 9222, "/tmp/vvic")
        port = CDPSessionManager.allocate_port("vvic-sourcing")
        assert port == 9222

    def test_allocate_port_second(self):
        CDPSessionManager.register("vvic-sourcing", 9222, "/tmp/vvic")
        port = CDPSessionManager.allocate_port("sinsang-collect")
        assert port == 9333

    def test_allocate_port_honors_candidates_without_collision(self):
        CDPSessionManager.register("vvic-sourcing", 9222, "/tmp/vvic")
        port = CDPSessionManager.allocate_port("sinsang-collect", preferred=9222, candidates=[9222, 9333])
        assert port == 9333

    def test_register_and_get(self):
        CDPSessionManager.register("vvic-sourcing", 9222, "/tmp/vvic", pid=1234)
        session = CDPSessionManager.get_session("vvic-sourcing")
        assert session is not None
        assert session.port == 9222
        assert session.pid == 1234
        assert session.work_key == "vvic-sourcing"

    def test_get_by_port(self):
        CDPSessionManager.register("vvic-sourcing", 9222, "/tmp/vvic", pid=1234)
        session = CDPSessionManager.get_by_port(9222)
        assert session is not None
        assert session.work_key == "vvic-sourcing"

    def test_normalize_work_key_uses_general_default(self):
        assert CDPSessionManager.normalize_work_key("") == "general"
        assert CDPSessionManager.normalize_work_key("  ") == "general"

    def test_effective_port_uses_general_session_without_work_key(self):
        CDPSessionManager.register("general", 9333, "/tmp/general")
        assert _effective_port({}) == 9333

    def test_effective_port_uses_work_key_session(self):
        CDPSessionManager.register("ntv2-china-sourcing-admin", 9444, "/tmp/china")
        assert _effective_port({"work_key": "ntv2-china-sourcing-admin"}) == 9444

    def test_release(self):
        CDPSessionManager.register("vvic-sourcing", 9222, "/tmp/vvic")
        CDPSessionManager.release("vvic-sourcing")
        assert CDPSessionManager.get_session("vvic-sourcing") is None

    def test_port_pool_exhaustion_uses_os_free_port(self):
        for i, port in enumerate([9222, 9333, 9444, 9555, 9666, 9777]):
            CDPSessionManager.register(f"svc-{i}", port, f"/tmp/svc-{i}")
        port = CDPSessionManager.allocate_port("svc-overflow")
        assert port not in {9222, 9333, 9444, 9555, 9666, 9777}
        assert 1 <= port <= 65535

    def test_get_all(self):
        CDPSessionManager.register("a", 9222, "/tmp/a")
        CDPSessionManager.register("b", 9333, "/tmp/b")
        all_sessions = CDPSessionManager.get_all()
        assert len(all_sessions) == 2
        assert "a" in all_sessions
        assert "b" in all_sessions

    def test_get_session_nonexistent(self):
        assert CDPSessionManager.get_session("nonexistent") is None

    def test_release_nonexistent(self):
        CDPSessionManager.release("nonexistent")


@pytest.mark.asyncio
async def test_browser_close_session_releases_session_and_guard(monkeypatch):
    CDPSessionManager._sessions.clear()
    CDPCommandGuardManager._guards.clear()
    CDPSessionManager.register("aads-ceo-browser", 9444, os.path.join(_default_profile_root(), "isolated-aads-ceo-browser"), pid=1234)

    async def fake_list_targets(_port):
        return [{"id": "target-1", "type": "page", "webSocketDebuggerUrl": "ws://target", "url": "https://aads.newtalk.kr"}]

    async def fake_browser_ws(_port):
        return "ws://browser"

    async def fake_send_cdp(_ws_url, _method, _params, timeout_seconds=5):
        return {"result": True}

    monkeypatch.setattr("browser_auto._list_cdp_targets", fake_list_targets)
    monkeypatch.setattr("browser_auto._get_browser_ws_url", fake_browser_ws)
    monkeypatch.setattr("browser_auto._send_cdp", fake_send_cdp)
    monkeypatch.setattr("browser_auto._terminate_browser_process", lambda pid: {"attempted": True, "pid": pid, "success": True})

    result = await browser_close_session({"work_key": "aads-ceo-browser", "close_browser": True})

    assert result["status"] == "success"
    assert result["data"]["closed_tabs"] == 1
    assert result["data"]["session_released"] is True
    assert result["data"]["process"]["pid"] == 1234
    assert CDPSessionManager.get_session("aads-ceo-browser") is None


@pytest.mark.asyncio
async def test_browser_close_session_refuses_unmanaged_user_profile(monkeypatch):
    CDPSessionManager._sessions.clear()
    CDPCommandGuardManager._guards.clear()
    CDPSessionManager.register("aads-ceo-browser", 9444, "/tmp/user-chrome-profile", pid=1234)

    async def fail_list_targets(_port):
        raise AssertionError("unmanaged profile tabs must not be touched")

    monkeypatch.setattr("browser_auto._list_cdp_targets", fail_list_targets)
    monkeypatch.setattr("browser_auto._terminate_browser_process", lambda pid: {"attempted": True, "pid": pid, "success": True})

    result = await browser_close_session({"work_key": "aads-ceo-browser", "close_browser": True})

    assert result["status"] == "error"
    assert result["data"]["error_code"] == "UNMANAGED_BROWSER_PROFILE"
    assert result["data"]["process"]["attempted"] is False
    assert CDPSessionManager.get_session("aads-ceo-browser") is None


@pytest.mark.asyncio
async def test_browser_launch_navigates_existing_work_key_session(monkeypatch):
    CDPSessionManager._sessions.clear()
    CDPSessionManager.register(
        "yeoljeong-delivery-coupangeats-biz-junghwa-test",
        9444,
        os.path.join(_default_profile_root(), "isolated-coupang"),
        pid=1234,
    )

    async def fake_probe(_port):
        return {"webSocketDebuggerUrl": "ws://127.0.0.1:9444/devtools/browser/test"}

    navigations: list[dict] = []

    async def fake_browser_navigate(params):
        navigations.append(dict(params))
        return {"status": "success", "data": {"url": params["url"]}}

    monkeypatch.setattr("browser_auto._probe_cdp_version", fake_probe)
    monkeypatch.setattr("browser_auto.browser_navigate", fake_browser_navigate)

    result = await browser_launch(
        {
            "work_key": "yeoljeong-delivery-coupangeats-biz-junghwa-test",
            "url": "https://store.coupangeats.com/merchant/",
        }
    )

    assert result["status"] == "success"
    assert result["data"]["navigated"] is True
    assert navigations[0]["url"] == "https://store.coupangeats.com/merchant/"
    assert navigations[0]["reuse_tab"] is False


@pytest.mark.asyncio
async def test_bank_work_key_port_rejects_other_portal_targets(monkeypatch):
    async def fake_list_targets(_port):
        return [
            {
                "id": "target-coupang",
                "type": "page",
                "url": "https://store.coupangeats.com/merchant/login",
                "webSocketDebuggerUrl": "ws://target",
            }
        ]

    monkeypatch.setattr("browser_auto._list_cdp_targets", fake_list_targets)

    assert await _bank_work_key_port_matches_url(
        9222,
        "https://bank.shinhan.com/rib/easy/index.jsp",
    ) is False


@pytest.mark.asyncio
async def test_bank_work_key_port_accepts_requested_bank_target(monkeypatch):
    async def fake_list_targets(_port):
        return [
            {
                "id": "target-shinhan",
                "type": "page",
                "url": "https://bank.shinhan.com/rib/easy/index.jsp#210000000000",
                "webSocketDebuggerUrl": "ws://target",
            }
        ]

    monkeypatch.setattr("browser_auto._list_cdp_targets", fake_list_targets)

    assert await _bank_work_key_port_matches_url(
        9222,
        "https://bank.shinhan.com/rib/easy/index.jsp",
    ) is True


@pytest.mark.asyncio
async def test_browser_launch_registers_ownerless_matching_bank_port(monkeypatch):
    CDPSessionManager._sessions.clear()

    async def fake_probe(port):
        if port == 9222:
            return {"webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/browser/test"}
        return None

    async def fake_list_targets(_port):
        return [
            {
                "id": "target-shinhan",
                "type": "page",
                "url": "https://bank.shinhan.com/rib/easy/index.jsp#210000000000",
                "webSocketDebuggerUrl": "ws://target",
            }
        ]

    navigations: list[dict] = []

    async def fake_browser_navigate(params):
        navigations.append(dict(params))
        return {"status": "success", "data": {"url": params["url"]}}

    monkeypatch.setattr("browser_auto._probe_cdp_version", fake_probe)
    monkeypatch.setattr("browser_auto._list_cdp_targets", fake_list_targets)
    monkeypatch.setattr("browser_auto.browser_navigate", fake_browser_navigate)

    result = await browser_launch(
        {
            "work_key": "yeoljeong-bank-shinhan-individual-abc",
            "url": "https://bank.shinhan.com/rib/easy/index.jsp",
            "isolated_profile": True,
        }
    )

    assert result["status"] == "success"
    assert result["data"]["port"] == 9222
    assert "재등록" in result["data"]["message"]
    assert CDPSessionManager.get_session("yeoljeong-bank-shinhan-individual-abc").port == 9222
    assert navigations[0]["reuse_tab"] is False


@pytest.mark.asyncio
async def test_browser_navigate_uses_existing_work_key_target(monkeypatch):
    CDPSessionManager._sessions.clear()
    CDPSessionManager.register(
        "yeoljeong-delivery-coupangeats-biz-junghwa-test",
        9444,
        os.path.join(_default_profile_root(), "isolated-coupang"),
        pid=1234,
    )
    CDPSessionManager.mark_healthy(
        "yeoljeong-delivery-coupangeats-biz-junghwa-test",
        target_id="target-coupang",
        target_url="https://self.baemin.com/",
    )
    calls: list[dict] = []

    async def fake_send_cdp_command(port, method, params, *, timeout_seconds, target_id="", target_idx=0):
        calls.append(
            {
                "port": port,
                "method": method,
                "params": dict(params or {}),
                "target_id": target_id,
                "target_idx": target_idx,
            }
        )
        return {"frameId": "frame-1", "_target": {"id": target_id, "url": "https://self.baemin.com/"}}

    monkeypatch.setattr("browser_auto._send_cdp_command", fake_send_cdp_command)

    result = await browser_navigate(
        {
            "work_key": "yeoljeong-delivery-coupangeats-biz-junghwa-test",
            "port": 9444,
            "url": "https://store.coupangeats.com/merchant/",
            "reuse_tab": False,
        }
    )

    session = CDPSessionManager.get_session("yeoljeong-delivery-coupangeats-biz-junghwa-test")
    assert result["status"] == "success"
    assert calls[0]["target_id"] == "target-coupang"
    assert result["data"]["target_url"] == "https://store.coupangeats.com/merchant/"
    assert session is not None
    assert session.last_target_id == "target-coupang"
    assert session.last_target_url == "https://store.coupangeats.com/merchant/"
