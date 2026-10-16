"""extai-* work_key 는 고정 프로필 레인, 그 외 work_key 는 기존 isolated 프로필 유지."""
from __future__ import annotations

import pytest

from app.browser_bridge.registry import PairingManager, SessionRegistry
from app.browser_bridge.service import (
    EXTAI_AGENT_WORK_KEY,
    BrowserBridgeError,
    BrowserBridgeService,
    is_extai_work_key,
    pick_extai_tab,
)
from app.browser_bridge.storage_state import StorageStateManager


def _new_service(tmp_path) -> BrowserBridgeService:
    return BrowserBridgeService(
        pairings=PairingManager(default_ttl_seconds=60),
        sessions=SessionRegistry(tmp_path / "sessions"),
        storage_states=StorageStateManager(tmp_path),
    )


def _make_service(tmp_path, monkeypatch) -> tuple[BrowserBridgeService, list[dict]]:
    service = _new_service(tmp_path)
    calls: list[dict] = []

    async def fake_ensure_pc_agent_cdp_session(**kwargs):
        calls.append(kwargs)
        metadata = {
            "agent_id": "pc-1",
            "port": "9222" if kwargs.get("agent_work_key") else str(9300 + len(calls)),
            "endpoint_kind": "local_agent",
            "profile_dir": "C:/KakaoBot/cdp-profile"
            if not kwargs["isolated_profile"]
            else f"C:/KakaoBot/cdp-profile/isolated-{kwargs['isolation_id']}",
        }
        if kwargs.get("profile_lane"):
            metadata["profile_lane"] = kwargs["profile_lane"]
        return service.register_trusted_session(
            label=kwargs["label"],
            endpoint_kind="local_agent",
            metadata=metadata,
            work_key=kwargs["work_key"],
            protected=kwargs["protected"],
            activate=kwargs["activate"],
        )

    monkeypatch.setattr(service, "ensure_pc_agent_cdp_session", fake_ensure_pc_agent_cdp_session)
    monkeypatch.setattr(service, "_local_agent_online", lambda _session: True)
    return service, calls


@pytest.mark.parametrize(
    ("work_key", "expected"),
    [
        ("extai-chatgpt", True),
        ("extai:claude", True),
        ("EXTAI-Gemini", True),
        ("  extai-grok ", True),
        ("extai", False),
        ("bank-shinhan", False),
        ("yeoljeong-bank-kb", False),
        ("ntv2-sinsang-registration", False),
        ("my-extai-chatgpt", False),
        ("", False),
    ],
)
def test_is_extai_work_key(work_key: str, expected: bool) -> None:
    assert is_extai_work_key(work_key) is expected


@pytest.mark.asyncio
@pytest.mark.parametrize("work_key", ["extai-chatgpt", "extai:claude"])
async def test_extai_work_key_uses_fixed_profile(work_key, monkeypatch, tmp_path) -> None:
    service, calls = _make_service(tmp_path, monkeypatch)

    session = await service.ensure_work_session(work_key=work_key, agent_id="pc-1")

    assert len(calls) == 1
    assert calls[0]["isolated_profile"] is False
    assert calls[0]["isolation_id"] == ""
    assert calls[0]["work_key"] == work_key
    assert calls[0]["profile_lane"] == "extai_fixed"
    assert calls[0]["agent_work_key"] == EXTAI_AGENT_WORK_KEY
    assert calls[0]["protected"] is False
    assert session.protected is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "work_key",
    ["bank-shinhan", "yeoljeong-bank-kb", "ntv2-sinsang-registration", "ntv2-vvic-scrape", "arbitrary-key"],
)
async def test_non_extai_work_key_keeps_isolated_profile(work_key, monkeypatch, tmp_path) -> None:
    service, calls = _make_service(tmp_path, monkeypatch)

    await service.ensure_work_session(work_key=work_key, agent_id="pc-1")

    assert len(calls) == 1
    assert calls[0]["isolated_profile"] is True
    assert calls[0]["isolation_id"] == work_key
    assert calls[0]["work_key"] == work_key
    assert calls[0]["profile_lane"] == "isolated"
    assert calls[0]["agent_work_key"] == ""


@pytest.mark.asyncio
async def test_sinsang_remains_protected_and_extai_is_not(monkeypatch, tmp_path) -> None:
    service, calls = _make_service(tmp_path, monkeypatch)

    await service.ensure_work_session(work_key="ntv2-sinsang-registration")
    await service.ensure_work_session(work_key="extai-chatgpt")

    assert calls[0]["protected"] is True
    assert calls[1]["protected"] is False


@pytest.mark.asyncio
async def test_existing_isolated_extai_session_is_migrated_not_reused(monkeypatch, tmp_path) -> None:
    """레인 전환 이전에 만든 isolated-extai-* 세션이 reuse 로 그대로 반환되면 안 된다."""
    service, calls = _make_service(tmp_path, monkeypatch)
    legacy = service.register_trusted_session(
        label="legacy",
        endpoint_kind="local_agent",
        metadata={
            "agent_id": "pc-1",
            "port": "9333",
            "endpoint_kind": "local_agent",
            "profile_dir": "C:/KakaoBot/cdp-profile/isolated-extai-chatgpt",
        },
        work_key="extai-chatgpt",
        protected=False,
        activate=False,
    )

    migrated = await service.ensure_work_session(work_key="extai-chatgpt", agent_id="pc-1")

    assert len(calls) == 1
    assert calls[0]["isolated_profile"] is False
    assert migrated.session_id != legacy.session_id
    assert "isolated-" not in migrated.endpoint.metadata["profile_dir"]
    stale = service.sessions.get(legacy.session_id)
    assert stale is not None
    assert stale.work_key == ""
    assert stale.endpoint.metadata["stale"] is True
    assert stale.endpoint.metadata["stale_reason"] == "profile_lane_migration"

    again = await service.ensure_work_session(work_key="extai-chatgpt", agent_id="pc-1")
    assert again.session_id == migrated.session_id
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_existing_extai_session_with_isolated_profile_dir_is_migrated(monkeypatch, tmp_path) -> None:
    service, calls = _make_service(tmp_path, monkeypatch)
    service.register_trusted_session(
        label="mislabeled",
        endpoint_kind="local_agent",
        metadata={
            "agent_id": "pc-1",
            "port": "9444",
            "endpoint_kind": "local_agent",
            "profile_lane": "extai_fixed",
            "profile_dir": "C:/KakaoBot/cdp-profile/isolated-extai-claude",
        },
        work_key="extai-claude",
        protected=False,
        activate=False,
    )

    await service.ensure_work_session(work_key="extai-claude", agent_id="pc-1")

    assert len(calls) == 1


@pytest.mark.asyncio
async def test_existing_non_extai_isolated_session_still_reused(monkeypatch, tmp_path) -> None:
    service, calls = _make_service(tmp_path, monkeypatch)

    first = await service.ensure_work_session(work_key="bank-shinhan", agent_id="pc-1")
    second = await service.ensure_work_session(work_key="bank-shinhan", agent_id="pc-1")

    assert len(calls) == 1
    assert second.session_id == first.session_id


@pytest.mark.asyncio
async def test_extai_keys_share_one_pc_agent_owner(monkeypatch, tmp_path) -> None:
    service, calls = _make_service(tmp_path, monkeypatch)

    await service.ensure_work_session(work_key="extai-chatgpt", agent_id="pc-1")
    await service.ensure_work_session(work_key="extai-claude", agent_id="pc-1")

    assert [c["agent_work_key"] for c in calls] == [EXTAI_AGENT_WORK_KEY, EXTAI_AGENT_WORK_KEY]
    assert [c["work_key"] for c in calls] == ["extai-chatgpt", "extai-claude"]


class FakePc:
    """PC Agent 한 대: Chrome 한 개(탭 목록)를 공유하고 보낸 명령을 기록한다."""

    def __init__(self, tabs=None, *, launch_error="", profile="C:/KakaoBot/cdp-profile"):
        self.tabs = [dict(t) for t in (tabs if tabs is not None else [{"id": "t1", "url": "about:blank", "type": "page"}])]
        self.commands: list[dict] = []
        self.launch_error = launch_error
        self.profile = profile

    async def execute(self, **cmd):
        self.commands.append(cmd)
        kind, params = cmd["command_type"], cmd["params"]
        if kind == "browser_launch":
            if self.launch_error:
                return {"status": "error", "error_code": self.launch_error, "message": "launch failed"}
            data = {"port": 9222, "user_data_dir": self.profile}
        elif kind == "browser_tabs":
            data = {"tabs": [dict(t) for t in self.tabs], "count": len(self.tabs)}
        elif kind == "browser_navigate":
            for tab in self.tabs:
                if tab["id"] == params["target_id"]:
                    tab["url"] = params["url"]
            data = {"url": params["url"], "target_id": params["target_id"]}
        elif kind == "browser_close_tab":
            before = len(self.tabs)
            self.tabs = [t for t in self.tabs if t["id"] != params["target_id"]]
            data = {"closed": before - len(self.tabs)}
        else:
            data = {}
        return {"status": "success", "lease": {"agent_id": "pc-1"}, "result": {"result": data}}

    def types(self) -> list[str]:
        return [c["command_type"] for c in self.commands]

    def of(self, kind: str) -> list[dict]:
        return [c for c in self.commands if c["command_type"] == kind]


def _real_launch_service(tmp_path, monkeypatch, pc: FakePc) -> BrowserBridgeService:
    from app.services import pc_agent_manager as manager_module

    service = _new_service(tmp_path)
    monkeypatch.setattr(manager_module.pc_agent_manager, "execute_routed_command", pc.execute)
    monkeypatch.setattr(service, "_route_pc_agent_via_active_api_first", lambda: False)
    monkeypatch.setattr(service, "_local_agent_online", lambda _session: True)
    return service


CHATGPT = "https://chatgpt.com/"
CLAUDE = "https://claude.ai/new"


@pytest.mark.asyncio
async def test_launch_params_use_shared_agent_key_and_no_isolation_id(monkeypatch, tmp_path) -> None:
    pc = FakePc()
    service = _real_launch_service(tmp_path, monkeypatch, pc)

    session = await service.ensure_pc_agent_cdp_session(
        agent_id="pc-1",
        work_key="extai-chatgpt",
        isolated_profile=False,
        isolation_id="",
        profile_lane="extai_fixed",
        agent_work_key=EXTAI_AGENT_WORK_KEY,
    )

    params = pc.of("browser_launch")[0]["params"]
    assert params["isolated_profile"] is False
    assert "isolation_id" not in params
    assert params["work_key"] == EXTAI_AGENT_WORK_KEY
    assert params["work_key"] != "general"
    assert session.work_key == "extai-chatgpt"
    assert session.endpoint.metadata["work_key"] == "extai-chatgpt"
    assert session.endpoint.metadata["profile_lane"] == "extai_fixed"
    assert "isolated-" not in session.endpoint.metadata["profile_dir"]


@pytest.mark.asyncio
async def test_launch_params_for_isolated_lane_unchanged(monkeypatch, tmp_path) -> None:
    pc = FakePc()
    service = _real_launch_service(tmp_path, monkeypatch, pc)

    await service.ensure_pc_agent_cdp_session(
        agent_id="pc-1",
        work_key="bank-shinhan",
        isolated_profile=True,
        isolation_id="bank-shinhan",
        url="https://bank.example/login",
    )

    params = pc.of("browser_launch")[0]["params"]
    assert params["isolated_profile"] is True
    assert params["isolation_id"] == "bank-shinhan"
    assert params["work_key"] == "bank-shinhan"
    assert params["url"] == "https://bank.example/login"
    assert pc.types() == ["browser_launch"]


@pytest.mark.asyncio
async def test_force_recreate_does_not_close_shared_fixed_profile_browser(monkeypatch, tmp_path) -> None:
    pc = FakePc()
    service = _real_launch_service(tmp_path, monkeypatch, pc)

    await service.ensure_pc_agent_cdp_session(
        agent_id="pc-1",
        work_key="extai-chatgpt",
        isolated_profile=False,
        profile_lane="extai_fixed",
        agent_work_key=EXTAI_AGENT_WORK_KEY,
        force_recreate=True,
    )

    assert pc.types() == ["browser_launch"]


@pytest.mark.asyncio
async def test_force_recreate_still_closes_isolated_lane_browser(monkeypatch, tmp_path) -> None:
    pc = FakePc()
    service = _real_launch_service(tmp_path, monkeypatch, pc)

    await service.ensure_pc_agent_cdp_session(
        agent_id="pc-1",
        work_key="bank-shinhan",
        isolated_profile=True,
        isolation_id="bank-shinhan",
        force_recreate=True,
    )

    assert pc.types() == ["browser_close_session", "browser_launch"]
    assert pc.commands[0]["params"]["work_key"] == "bank-shinhan"


def test_pick_extai_tab_order() -> None:
    chatgpt_tab = {"id": "a", "url": "https://chatgpt.com/c/1"}
    blank_tab = {"id": "b", "url": "about:blank"}
    user_tab = {"id": "c", "url": "https://mail.example.com/"}

    assert pick_extai_tab([blank_tab, chatgpt_tab], CHATGPT, set()) == (chatgpt_tab, "reuse")
    assert pick_extai_tab([user_tab, blank_tab], CLAUDE, {"chatgpt.com"}) == (blank_tab, "blank")
    assert pick_extai_tab([user_tab, chatgpt_tab], CLAUDE, {"chatgpt.com"}) == (chatgpt_tab, "takeover")
    assert pick_extai_tab([user_tab], CLAUDE, {"chatgpt.com"}) == (None, "")
    assert pick_extai_tab([], CLAUDE, set()) == (None, "")


@pytest.mark.asyncio
async def test_second_provider_does_not_navigate_first_providers_tab(monkeypatch, tmp_path) -> None:
    """리뷰 지적 1: ChatGPT 다음 Claude 를 ensure 해도 ChatGPT 탭을 덮어쓰지 않는다."""
    pc = FakePc(
        tabs=[
            {"id": "t1", "url": "about:blank", "type": "page"},
            {"id": "t2", "url": "about:blank", "type": "page"},
        ]
    )
    service = _real_launch_service(tmp_path, monkeypatch, pc)

    chatgpt = await service.ensure_work_session(work_key="extai-chatgpt", agent_id="pc-1", url=CHATGPT)
    claude = await service.ensure_work_session(work_key="extai-claude", agent_id="pc-1", url=CLAUDE)

    assert all(c["params"]["url"] == "about:blank" for c in pc.of("browser_launch"))
    navigations = [(c["params"]["target_id"], c["params"]["url"]) for c in pc.of("browser_navigate")]
    assert navigations == [("t1", CHATGPT), ("t2", CLAUDE)]
    assert {t["id"]: t["url"] for t in pc.tabs} == {"t1": CHATGPT, "t2": CLAUDE}
    assert chatgpt.endpoint.metadata["extai_target_id"] == "t1"
    assert claude.endpoint.metadata["extai_target_id"] == "t2"
    assert chatgpt.work_key == "extai-chatgpt"
    assert service.sessions.find_by_work_key("extai-chatgpt").session_id == chatgpt.session_id


@pytest.mark.asyncio
async def test_takeover_invalidates_previous_provider_session(monkeypatch, tmp_path) -> None:
    """빈 탭이 없으면 기존 provider 탭을 넘겨받되, 그 세션 기록은 폐기해 잘못 재사용되지 않게 한다."""
    pc = FakePc()
    service = _real_launch_service(tmp_path, monkeypatch, pc)

    chatgpt = await service.ensure_work_session(work_key="extai-chatgpt", agent_id="pc-1", url=CHATGPT)
    claude = await service.ensure_work_session(work_key="extai-claude", agent_id="pc-1", url=CLAUDE)

    assert pc.tabs[0]["url"] == CLAUDE
    stale = service.sessions.get(chatgpt.session_id)
    assert stale.work_key == ""
    assert stale.endpoint.metadata["stale"] is True
    assert service.sessions.find_by_work_key("extai-chatgpt") is None

    chatgpt_again = await service.ensure_work_session(work_key="extai-chatgpt", agent_id="pc-1", url=CHATGPT)
    assert chatgpt_again.session_id != chatgpt.session_id
    assert pc.tabs[0]["url"] == CHATGPT
    assert service.sessions.find_by_work_key("extai-claude") is None
    assert claude.session_id != chatgpt_again.session_id


@pytest.mark.asyncio
async def test_extai_never_navigates_user_tab(monkeypatch, tmp_path) -> None:
    pc = FakePc(tabs=[{"id": "u1", "url": "https://mail.example.com/", "type": "page"}])
    service = _real_launch_service(tmp_path, monkeypatch, pc)

    with pytest.raises(BrowserBridgeError) as exc_info:
        await service.ensure_work_session(work_key="extai-chatgpt", agent_id="pc-1", url=CHATGPT)

    assert exc_info.value.error_code == "EXTAI_TAB_UNAVAILABLE"
    assert pc.of("browser_navigate") == []
    assert service.sessions.find_by_work_key("extai-chatgpt") is None


@pytest.mark.asyncio
async def test_about_blank_ensure_does_not_touch_tabs(monkeypatch, tmp_path) -> None:
    pc = FakePc()
    service = _real_launch_service(tmp_path, monkeypatch, pc)

    await service.ensure_work_session(work_key="extai-chatgpt", agent_id="pc-1", url="about:blank")

    assert pc.types() == ["browser_launch"]


@pytest.mark.asyncio
async def test_unready_launch_fails_closed_without_adopting_port(monkeypatch, tmp_path) -> None:
    """리뷰 지적 2: 고정 프로필 launch 실패 시 포트의 정체불명 Chrome 을 health/tabs fallback 으로 채택하지 않는다."""
    pc = FakePc(launch_error="CDP_NOT_READY")
    service = _real_launch_service(tmp_path, monkeypatch, pc)

    with pytest.raises(BrowserBridgeError) as exc_info:
        await service.ensure_work_session(work_key="extai-chatgpt", agent_id="pc-1", url=CHATGPT)

    assert exc_info.value.error_code == "EXTAI_FIXED_PROFILE_UNVERIFIED"
    assert pc.types() == ["browser_launch"]
    assert service.sessions.find_by_work_key("extai-chatgpt") is None


@pytest.mark.asyncio
async def test_isolated_lane_keeps_health_fallback_on_unready_launch(monkeypatch, tmp_path) -> None:
    pc = FakePc(launch_error="CDP_NOT_READY")
    service = _real_launch_service(tmp_path, monkeypatch, pc)

    await service.ensure_work_session(work_key="bank-shinhan", agent_id="pc-1")

    assert pc.types()[:2] == ["browser_launch", "browser_health"]


@pytest.mark.asyncio
async def test_isolated_profile_reported_for_extai_is_rejected(monkeypatch, tmp_path) -> None:
    pc = FakePc(profile="C:/KakaoBot/cdp-profile/isolated-extai-chatgpt")
    service = _real_launch_service(tmp_path, monkeypatch, pc)

    with pytest.raises(BrowserBridgeError) as exc_info:
        await service.ensure_work_session(work_key="extai-chatgpt", agent_id="pc-1", url=CHATGPT)

    assert exc_info.value.error_code == "EXTAI_FIXED_PROFILE_UNVERIFIED"
    assert pc.of("browser_navigate") == []


@pytest.mark.asyncio
async def test_close_extai_session_closes_only_own_tab_not_browser(monkeypatch, tmp_path) -> None:
    """리뷰 지적 3: close 는 extai 업무 키로 session_not_found 를 받지 않고, 공유 Chrome 은 건드리지 않는다."""
    pc = FakePc(
        tabs=[
            {"id": "t1", "url": "about:blank", "type": "page"},
            {"id": "t2", "url": "about:blank", "type": "page"},
        ]
    )
    service = _real_launch_service(tmp_path, monkeypatch, pc)
    await service.ensure_work_session(work_key="extai-chatgpt", agent_id="pc-1", url=CHATGPT)
    claude = await service.ensure_work_session(work_key="extai-claude", agent_id="pc-1", url=CLAUDE)

    result = await service.close_work_session("extai-claude", close_browser=True, close_tabs=True)

    assert "browser_close_session" not in pc.types()
    close_tab = pc.of("browser_close_tab")[0]["params"]
    assert close_tab["target_id"] == "t2"
    assert close_tab["work_key"] == EXTAI_AGENT_WORK_KEY
    assert result["status"] == "retired"
    assert result["close_browser"] is False
    assert result["close_browser_skipped"] is True
    assert result["closed_tab"] is True
    assert [t["id"] for t in pc.tabs] == ["t1"]
    assert service.sessions.find_by_work_key("extai-claude") is None
    assert service.sessions.find_by_work_key("extai-chatgpt") is not None
    assert service.sessions.get(claude.session_id).endpoint.metadata["stale"] is True


@pytest.mark.asyncio
async def test_close_extai_session_without_close_tabs_sends_nothing(monkeypatch, tmp_path) -> None:
    pc = FakePc()
    service = _real_launch_service(tmp_path, monkeypatch, pc)
    await service.ensure_work_session(work_key="extai-chatgpt", agent_id="pc-1", url=CHATGPT)
    sent = len(pc.commands)

    result = await service.close_work_session("extai-chatgpt", close_tabs=False)

    assert len(pc.commands) == sent
    assert result["status"] == "retired"
    assert result["closed_tab"] is False


@pytest.mark.asyncio
async def test_close_isolated_session_still_sends_close_session(monkeypatch, tmp_path) -> None:
    pc = FakePc()
    service = _real_launch_service(tmp_path, monkeypatch, pc)
    await service.ensure_work_session(work_key="bank-shinhan", agent_id="pc-1")

    await service.close_work_session("bank-shinhan", close_browser=True)

    close = pc.of("browser_close_session")[0]["params"]
    assert close["work_key"] == "bank-shinhan"
    assert close["close_browser"] is True


def test_pc_agent_fixed_profile_has_no_isolated_dir() -> None:
    from pc_agent.commands.browser_auto import _default_profile_root, _resolve_profile_dir

    fixed = _resolve_profile_dir({"isolated_profile": False, "isolation_id": "extai"})
    assert fixed == _default_profile_root()
    assert "isolated-" not in fixed

    isolated = _resolve_profile_dir({"isolated_profile": True, "isolation_id": "bank-shinhan"})
    assert isolated.endswith("isolated-bank-shinhan")
