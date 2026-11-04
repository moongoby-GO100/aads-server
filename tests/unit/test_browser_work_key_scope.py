"""M2: chat-scoped (chat-pc-*) work_key 는 자기 세션의 브라우저에만 닿고, 기본 브라우저로 새지 않는다."""
import inspect
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "pc_agent", "commands"))

import browser_auto
from browser_auto import CDPSessionManager

CHAT_A = "chat-pc-" + "a" * 40
CHAT_B = "chat-pc-" + "b" * 40
REJECTION_CODES = {"WORK_KEY_SESSION_REQUIRED", "WORK_KEY_PORT_MISMATCH", "WORK_KEY_REQUIRED_FOR_OWNED_PORT", "INVALID_PORT"}


def _install_sessions(monkeypatch, sessions):
    """주어진 (work_key, port) 세션을 등록하고, CDP 로 새 나간 포트 접근을 touched 에 기록한다."""
    CDPSessionManager._sessions.clear()
    for pid, (work_key, port) in enumerate(sessions, start=10):
        CDPSessionManager.register(work_key, port, f"/tmp/{work_key}", pid=pid)
    touched: list[int] = []

    async def list_targets(port):
        touched.append(port)
        return [{"id": f"tab-{port}", "type": "page", "url": "about:blank", "title": str(port),
                 "webSocketDebuggerUrl": f"ws://127.0.0.1:{port}/devtools/page/x"}]

    async def forbidden(*_args, **_kwargs):
        touched.append(-1)
        raise AssertionError("CDP must not be reached")

    monkeypatch.setattr("browser_auto._list_cdp_targets", list_targets)
    for name in ("_send_cdp_command", "_connect_cdp_ws", "_http_get_json", "_probe_cdp_version"):
        monkeypatch.setattr(f"browser_auto.{name}", forbidden)
    return touched


@pytest.fixture
def cdp(monkeypatch):
    """chat A=9333, chat B=9444, 사용자 기본 브라우저 general=9222."""
    touched = _install_sessions(monkeypatch, [("general", 9222), (CHAT_A, 9333), (CHAT_B, 9444)])
    yield touched
    CDPSessionManager._sessions.clear()


@pytest.fixture
def cdp_chat_on_default(monkeypatch):
    """포트 할당기가 첫 채팅에 9222 를 준 상태: chat A=9222, chat B=9333, general 없음, bank=9555."""
    touched = _install_sessions(monkeypatch, [(CHAT_A, 9222), (CHAT_B, 9333), ("bank", 9555)])
    yield touched
    CDPSessionManager._sessions.clear()


def _error_code(result):
    assert result["status"] == "error"
    return result["data"]["error_code"]


@pytest.mark.asyncio
async def test_each_chat_sees_only_its_own_browser_tabs(cdp):
    a = await browser_auto.browser_tabs({"work_key": CHAT_A, "port": 9333})
    b = await browser_auto.browser_tabs({"work_key": CHAT_B})
    assert [t["id"] for t in a["data"]["tabs"]] == ["tab-9333"]
    assert [t["id"] for t in b["data"]["tabs"]] == ["tab-9444"]
    assert cdp == [9333, 9444]


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["browser_tabs", "browser_close_tab", "browser_navigate"])
@pytest.mark.parametrize("params", [
    {"work_key": CHAT_A, "port": 9444},
    {"work_key": CHAT_A, "port": 9222},
    {"work_key": CHAT_A, "port": 9999},
])
async def test_scoped_key_with_foreign_port_is_rejected_before_any_cdp_call(cdp, command, params):
    result = await getattr(browser_auto, command)({**params, "url": "https://example.com", "target_id": "tab-x"})
    assert _error_code(result) == "WORK_KEY_PORT_MISMATCH"
    assert cdp == []


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["browser_tabs", "browser_close_tab", "browser_navigate", "browser_eval"])
async def test_scoped_key_without_session_never_falls_back_to_default_browser(cdp, command):
    CDPSessionManager.release(CHAT_A)
    for params in ({"work_key": CHAT_A}, {"work_key": CHAT_A, "port": 9222}):
        result = await getattr(browser_auto, command)({**params, "url": "https://example.com", "expression": "1"})
        assert _error_code(result) == "WORK_KEY_SESSION_REQUIRED"
    assert cdp == []


@pytest.mark.asyncio
@pytest.mark.parametrize("params", [{"port": 9333}, {"work_key": "general", "port": 9333}, {"work_key": "other-lane", "port": 9444}])
async def test_port_owned_by_a_chat_session_requires_that_chat_key(cdp, params):
    result = await browser_auto.browser_tabs(params)
    assert _error_code(result) == "WORK_KEY_REQUIRED_FOR_OWNED_PORT"
    assert cdp == []


@pytest.mark.asyncio
async def test_non_scoped_lanes_keep_working_unchanged(cdp):
    CDPSessionManager.register("vvic-sourcing", 9555, "/tmp/vvic")
    assert (await browser_auto.browser_tabs({}))["status"] == "success"
    assert (await browser_auto.browser_tabs({"port": 9222}))["status"] == "success"
    assert (await browser_auto.browser_tabs({"work_key": "vvic-sourcing"}))["status"] == "success"
    assert (await browser_auto.browser_tabs({"work_key": "vvic-sourcing", "port": 9222}))["status"] == "success"
    assert cdp == [9222, 9222, 9555, 9222]


def test_every_port_resolving_command_is_scope_guarded():
    unguarded = [
        name for name, fn in vars(browser_auto).items()
        if name.startswith("browser_") and inspect.iscoroutinefunction(fn)
        and "_effective_port(" in inspect.getsource(inspect.unwrap(fn))
        and name not in browser_auto._SCOPED_COMMANDS
    ]
    assert unguarded == []
    assert all(hasattr(getattr(browser_auto, name), "__wrapped__") for name in browser_auto._SCOPED_COMMANDS)


@pytest.mark.asyncio
async def test_close_session_with_stale_pid_still_releases_the_chat_session(cdp, monkeypatch):
    """Chrome 재시작·업그레이드로 pid 가 사라져도 close_session 은 막히지 않는다(09/29 지적 b)."""
    async def no_browser_ws(_port):
        return "ws://127.0.0.1:9333/devtools/browser/x"

    async def closed(*_args, **_kwargs):
        return {"success": True}

    def gone(_pid, _sig):
        raise ProcessLookupError

    monkeypatch.setattr("browser_auto._get_browser_ws_url", no_browser_ws)
    monkeypatch.setattr("browser_auto._send_cdp", closed)
    monkeypatch.setattr("browser_auto.os.kill", gone)
    monkeypatch.setattr("browser_auto._is_managed_profile_dir", lambda _p: True)
    result = await browser_auto.browser_close_session({"work_key": CHAT_A, "close_browser": True})
    assert result["status"] == "success"
    assert result["data"]["session_released"] is True
    assert result["data"]["process"].get("already_exited") is True or sys.platform == "win32"
    assert CDPSessionManager.get_session(CHAT_A) is None
    assert CDPSessionManager.get_session(CHAT_B).port == 9444


# ── 기본 포트(9222) 를 채팅 세션이 소유할 때: 해석된 최종 포트로 소유권을 검사한다 ──
EXPECTED_SCOPED_COMMANDS = 14
REVIEWER_REPRODUCTIONS = [
    pytest.param({}, "WORK_KEY_REQUIRED_FOR_OWNED_PORT", id="work_key,port 모두 생략"),
    pytest.param({"work_key": "other-lane"}, "WORK_KEY_REQUIRED_FOR_OWNED_PORT", id="다른 work_key"),
    pytest.param({"port": None}, "INVALID_PORT", id="port=null"),
    pytest.param({"port": "bad"}, "INVALID_PORT", id="port=bad"),
]
EXTRA_REJECTIONS = [
    pytest.param({"work_key": "general"}, "WORK_KEY_REQUIRED_FOR_OWNED_PORT", id="general 명시"),
    pytest.param({"work_key": CHAT_B, "port": 9222}, "WORK_KEY_PORT_MISMATCH", id="다른 채팅 키+남의 9222"),
    pytest.param({"port": 9222}, "WORK_KEY_REQUIRED_FOR_OWNED_PORT", id="port=9222 만"),
    pytest.param({"work_key": "other-lane", "port": 9222}, "WORK_KEY_REQUIRED_FOR_OWNED_PORT", id="다른 키+9222"),
    pytest.param({"port": 0}, "INVALID_PORT", id="port=0"),
    pytest.param({"port": 70000}, "INVALID_PORT", id="port=70000"),
    pytest.param({"port": -5}, "INVALID_PORT", id="port=-5"),
    pytest.param({"port": True}, "INVALID_PORT", id="port=true"),
    pytest.param({"port": 1.5}, "INVALID_PORT", id="port=1.5"),
    pytest.param({"port": 9222.0}, "INVALID_PORT", id="port=9222.0"),
    pytest.param({"port": ""}, "INVALID_PORT", id="port 빈 문자열"),
    pytest.param({"port": [9222]}, "INVALID_PORT", id="port 리스트"),
    pytest.param({"work_key": CHAT_A, "port": None}, "INVALID_PORT", id="소유자 키+port null"),
    pytest.param({"work_key": CHAT_A, "port": "bad"}, "INVALID_PORT", id="소유자 키+port bad"),
    pytest.param({"work_key": CHAT_A, "port": 0}, "INVALID_PORT", id="소유자 키+port 0"),
    pytest.param({"work_key": CHAT_A, "port": 70000}, "INVALID_PORT", id="소유자 키+port 70000"),
    pytest.param({"work_key": CHAT_A, "port": True}, "INVALID_PORT", id="소유자 키+port true"),
    pytest.param({"work_key": CHAT_A, "port": 1.5}, "INVALID_PORT", id="소유자 키+port 1.5"),
    pytest.param({"work_key": CHAT_A, "port": 9333}, "WORK_KEY_PORT_MISMATCH", id="소유자 키+남의 포트"),
]


def test_scoped_command_list_matches_the_guard_scope():
    assert len(browser_auto._SCOPED_COMMANDS) == EXPECTED_SCOPED_COMMANDS


@pytest.mark.asyncio
@pytest.mark.parametrize("command", browser_auto._SCOPED_COMMANDS)
@pytest.mark.parametrize("params, code", REVIEWER_REPRODUCTIONS + EXTRA_REJECTIONS)
async def test_chat_owned_default_port_is_unreachable_without_the_owner_key(cdp_chat_on_default, command, params, code):
    result = await getattr(browser_auto, command)({**params, "url": "https://example.com", "target_id": "t", "expression": "1"})
    assert _error_code(result) == code
    assert code in REJECTION_CODES
    assert cdp_chat_on_default == []


@pytest.mark.asyncio
@pytest.mark.parametrize("params", [
    {"work_key": CHAT_A},
    {"work_key": CHAT_A, "port": 9222},
    {"work_key": CHAT_A, "port": "9222"},
])
async def test_owner_key_reaches_its_own_default_port(cdp_chat_on_default, params):
    result = await browser_auto.browser_tabs(params)
    assert result["status"] == "success"
    assert [t["id"] for t in result["data"]["tabs"]] == ["tab-9222"]
    assert cdp_chat_on_default == [9222]


@pytest.mark.asyncio
async def test_server_style_call_with_work_key_and_port_together_passes(cdp_chat_on_default):
    a = await browser_auto.browser_tabs({"work_key": CHAT_A, "port": 9222})
    b = await browser_auto.browser_tabs({"work_key": CHAT_B, "port": 9333})
    assert a["status"] == b["status"] == "success"
    assert cdp_chat_on_default == [9222, 9333]


@pytest.mark.asyncio
async def test_other_lanes_are_unchanged_when_chat_owns_the_default_port(cdp_chat_on_default):
    assert (await browser_auto.browser_tabs({"work_key": "bank"}))["status"] == "success"
    assert (await browser_auto.browser_tabs({"work_key": "bank", "port": 9555}))["status"] == "success"
    assert (await browser_auto.browser_tabs({"work_key": "ntv2", "port": 9777}))["status"] == "success"
    assert cdp_chat_on_default == [9555, 9555, 9777]


@pytest.mark.asyncio
@pytest.mark.parametrize("params", [{"work_key": "bank", "port": None}, {"work_key": "bank", "port": "bad"}, {"work_key": "bank", "port": 70000}])
async def test_invalid_port_is_rejected_even_for_non_scoped_lanes(cdp_chat_on_default, params):
    assert _error_code(await browser_auto.browser_tabs(params)) == "INVALID_PORT"
    assert cdp_chat_on_default == []


def test_guard_and_handlers_share_one_port_resolver():
    src = inspect.getsource(browser_auto._scope_violation)
    assert "_effective_port(params, strict=True)" in src
    assert "_coerce_port" not in src
