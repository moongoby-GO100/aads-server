"""PC 레인 browser_eval 시한 / late result 재사용 / 시한 뒤 명령 중단 / close 대상 검증."""
from __future__ import annotations

import asyncio
import time

import pytest

from app.browser_bridge import aads_adapter, pc_agent_budget
from app.browser_bridge.models import BrowserBridgeSession, BrowserEndpoint, BrowserEndpointKind, utcnow
from app.browser_bridge.service import BrowserBridgeService, _LocalAgentPage
from app.models.pc_agent import CommandResult
from app.services.pc_agent_manager import PCAgentManager, pc_agent_manager


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AADS_SERVICE_ROLE", raising=False)
    monkeypatch.delenv("AADS_PC_AGENT_ROUTE_ACTIVE_API_FIRST", raising=False)
    monkeypatch.delenv("PC_AGENT_DEFAULT_AGENT_ID", raising=False)
    monkeypatch.delenv("PC_AGENT_DEFAULT_HOSTNAME", raising=False)
    monkeypatch.delenv("PC_AGENT_DEFAULT_BROWSER_WORK_KEY", raising=False)


def _page() -> _LocalAgentPage:
    session = BrowserBridgeSession(
        session_id="sess-1",
        label="t",
        endpoint=BrowserEndpoint(
            kind=BrowserEndpointKind.LOCAL_AGENT,
            metadata={"agent_id": "pc-1", "port": "9222"},
        ),
        registered_at=utcnow(),
    )
    return _LocalAgentPage(session, BrowserBridgeService())


def _install_fake_manager(monkeypatch: pytest.MonkeyPatch, *, navigate_delay: float = 0.0):
    sent: list[tuple[str, dict]] = []

    async def fake_execute_routed_command(*, command_type, params, **_kw):
        sent.append((command_type, dict(params)))
        if command_type == "browser_navigate":
            await asyncio.sleep(navigate_delay)
        return {"status": "success", "result": {"data": {"value": "https://example.com/"}}}

    monkeypatch.setattr(pc_agent_manager, "execute_routed_command", fake_execute_routed_command)
    return sent


# ── 1) navigate 성공 + 잔여 시간 부족 → eval 미발송, navigate 성공 반환 ──────────────

@pytest.mark.asyncio
async def test_goto_skips_href_eval_when_remaining_below_estimate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(aads_adapter, "PC_AGENT_BROWSER_TOTAL_TIMEOUT_SECONDS", 0.6)
    monkeypatch.setattr(pc_agent_budget, "HREF_EVAL_ESTIMATE_SECONDS", 0.5)
    monkeypatch.setattr(pc_agent_budget, "BUDGET_RESERVE_SECONDS", 0.05)
    sent = _install_fake_manager(monkeypatch, navigate_delay=0.3)
    page = _page()

    async def tool() -> str:
        await page.goto("https://example.com/")
        return f"nav-ok eval_skipped={page.eval_skipped_reason}"

    result = await aads_adapter.run_with_pc_agent_deadline(tool(), stage="browser_navigate")

    assert result.startswith("nav-ok")
    assert "deadline_remaining" in result
    assert [c for c, _ in sent] == ["browser_navigate"]
    assert "pc_agent_browser_timeout" not in result


@pytest.mark.asyncio
async def test_goto_sends_href_eval_when_budget_is_ample(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(aads_adapter, "PC_AGENT_BROWSER_TOTAL_TIMEOUT_SECONDS", 30.0)
    sent = _install_fake_manager(monkeypatch)
    page = _page()

    async def tool() -> str:
        await page.goto("https://example.com/")
        return page.url

    result = await aads_adapter.run_with_pc_agent_deadline(tool(), stage="browser_navigate")

    assert result == "https://example.com/"
    assert [c for c, _ in sent] == ["browser_navigate", "browser_eval"]
    eval_params = sent[1][1]
    assert 0 < eval_params["upper_deadline_seconds"] <= 30.0
    assert "upper_deadline_seconds" not in sent[0][1] or sent[0][1]["upper_deadline_seconds"] > 0


@pytest.mark.asyncio
async def test_eval_timeout_is_clamped_to_remaining_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(aads_adapter, "PC_AGENT_BROWSER_TOTAL_TIMEOUT_SECONDS", 6.0)
    monkeypatch.setattr(pc_agent_budget, "BUDGET_RESERVE_SECONDS", 1.0)
    observed: dict = {}

    async def fake_execute_routed_command(*, command_type, params, command_timeout_seconds, queue_wait_timeout_seconds, **_kw):
        observed.update(
            command_timeout_seconds=command_timeout_seconds,
            queue_wait_timeout_seconds=queue_wait_timeout_seconds,
        )
        return {"status": "success", "result": {"data": {"value": "x"}}}

    monkeypatch.setattr(pc_agent_manager, "execute_routed_command", fake_execute_routed_command)
    page = _page()

    async def tool() -> None:
        await page._run_browser_command(
            "browser_eval", {"expression": "1"}, command_timeout_seconds=100.0, queue_wait_timeout_seconds=60.0
        )

    await aads_adapter.run_with_pc_agent_deadline(tool(), stage="x")

    assert observed["command_timeout_seconds"] + observed["queue_wait_timeout_seconds"] <= 5.01


# ── 3) 시한 뒤 후속 명령 중단 (정리용 close 만 예외) ───────────────────────────────

@pytest.mark.asyncio
async def test_no_eval_is_sent_after_deadline_even_if_cancel_is_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(aads_adapter, "PC_AGENT_BROWSER_TOTAL_TIMEOUT_SECONDS", 0.2)
    sent = _install_fake_manager(monkeypatch)
    page = _page()
    captured: list[BaseException] = []

    async def tool() -> str:
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            pass
        try:
            await page._run_browser_command("browser_eval", {"expression": "document.title"})
        except Exception as exc:
            captured.append(exc)
        return "done"

    await aads_adapter.run_with_pc_agent_deadline(tool(), stage="browser_navigate")

    assert len(captured) == 1
    assert isinstance(captured[0], pc_agent_budget.PcAgentDeadlineExceeded)
    assert sent == []


@pytest.mark.asyncio
async def test_cleanup_close_is_the_only_command_allowed_after_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    budget, token = pc_agent_budget.begin_call_budget(10.0, "t")
    try:
        budget.cancel()
        with pytest.raises(pc_agent_budget.PcAgentDeadlineExceeded):
            pc_agent_budget.ensure_open("browser_eval")
        with pytest.raises(pc_agent_budget.PcAgentDeadlineExceeded):
            pc_agent_budget.ensure_open("browser_navigate")
        assert pc_agent_budget.ensure_open("browser_close_session") is not None
    finally:
        pc_agent_budget.end_call_budget(token)
    assert pc_agent_budget.ensure_open("browser_eval") is None


@pytest.mark.asyncio
async def test_manager_refuses_browser_command_after_deadline() -> None:
    manager = PCAgentManager()
    budget, token = pc_agent_budget.begin_call_budget(10.0, "t")
    try:
        budget.cancel()
        with pytest.raises(pc_agent_budget.PcAgentDeadlineExceeded):
            await manager.execute_routed_command(command_type="browser_eval", params={"expression": "1"})
    finally:
        pc_agent_budget.end_call_budget(token)


# ── 2) late result 재사용 ────────────────────────────────────────────────────────

class _WS:
    async def send_json(self, _payload) -> None:
        return None


async def _manager_with_command(*, upper_deadline_seconds: float | None) -> tuple[PCAgentManager, str]:
    manager = PCAgentManager()
    manager.register_agent("pc-1", _WS(), {"hostname": "h", "capabilities": ["interactive_browser"]})  # type: ignore[arg-type]
    command_id = await manager.send_command("pc-1", "browser_eval", {"expression": "1"})
    if upper_deadline_seconds is not None:
        manager._command_upper_deadlines[command_id] = time.monotonic() + upper_deadline_seconds
    return manager, command_id


@pytest.mark.asyncio
async def test_late_result_within_upper_deadline_is_reused_while_waiting() -> None:
    manager, command_id = await _manager_with_command(upper_deadline_seconds=10.0)

    async def deliver() -> None:
        await asyncio.sleep(0.15)  # 명령 시한(0.05s) 뒤, 상위 시한 안
        manager.receive_result(command_id, {"status": "success", "data": {"value": "late-ok"}})

    asyncio.create_task(deliver())
    result = await manager.get_result(command_id, timeout=0.05)

    assert result.status == "success"
    assert result.result == {"value": "late-ok"}


@pytest.mark.asyncio
async def test_late_result_after_command_timeout_is_promoted_for_same_command_id() -> None:
    manager, command_id = await _manager_with_command(upper_deadline_seconds=0.0)
    manager._command_upper_deadlines[command_id] = time.monotonic() + 10.0
    manager._late_result_reuse_seconds = 0.0  # 대기 연장 없이 timeout 으로 먼저 끝난다

    timed_out = await manager.get_result(command_id, timeout=0.01)
    assert timed_out.status == "timeout"

    manager.receive_result(command_id, {"status": "success", "data": {"value": "late-ok"}})
    reused = await manager.get_result(command_id, timeout=0.01)

    assert reused.status == "success"
    assert reused.result == {"value": "late-ok"}


@pytest.mark.asyncio
async def test_late_result_after_upper_deadline_is_discarded_and_logged(caplog: pytest.LogCaptureFixture) -> None:
    manager, command_id = await _manager_with_command(upper_deadline_seconds=0.01)

    timed_out = await manager.get_result(command_id, timeout=0.01)
    assert timed_out.status == "timeout"
    await asyncio.sleep(0.05)
    with caplog.at_level("WARNING"):
        manager.receive_result(command_id, {"status": "success", "data": {"value": "too-late"}})

    stored = await manager.get_result(command_id, timeout=0.01)
    assert stored.status == "timeout"
    assert stored.result["late_result"] is True
    messages = [r.getMessage() for r in caplog.records]
    assert any(
        "pc_agent_late_result_discarded" in m and command_id in m and "delay_ms=" in m for m in messages
    )


@pytest.mark.asyncio
async def test_late_result_without_upper_deadline_keeps_previous_behaviour() -> None:
    manager, command_id = await _manager_with_command(upper_deadline_seconds=None)

    timed_out = await manager.get_result(command_id, timeout=0.01)
    assert timed_out.status == "timeout"
    manager.receive_result(command_id, {"status": "success", "data": {"v": 1}})

    stored = await manager.get_result(command_id, timeout=0.01)
    assert stored.status == "timeout"
    assert stored.result == {"late_result": True, "late_status": "success", "late_data": {"v": 1}}


# ── 4) close_session 대상 검증 ────────────────────────────────────────────────────

async def _run_timeout_cleanup(monkeypatch: pytest.MonkeyPatch, params: dict) -> list[tuple[str, dict]]:
    manager = PCAgentManager()
    manager.register_agent(
        "ceo-pc", _WS(), {"hostname": "ceo", "capabilities": ["chrome_cdp", "interactive_browser"]}  # type: ignore[arg-type]
    )
    sent: list[tuple[str, dict]] = []

    async def fake_send_command(_agent_id: str, command_type: str, p: dict) -> str:
        sent.append((command_type, dict(p)))
        return f"cmd-{len(sent)}"

    async def fake_get_result(command_id: str, timeout: float = 30.0) -> CommandResult:
        status = "timeout" if command_id == "cmd-1" else "success"
        return CommandResult(
            command_id=command_id,
            agent_id="ceo-pc",
            status=status,
            result=None if status == "timeout" else {"session_released": True},
        )

    monkeypatch.setattr(manager, "send_command", fake_send_command)
    monkeypatch.setattr(manager, "get_result", fake_get_result)
    await manager.execute_routed_command(
        command_type="browser_eval",
        params={"expression": "location.href", **params},
        command_timeout_seconds=5.0,
        lease_ttl_seconds=35,
    )
    return sent


@pytest.mark.asyncio
async def test_timeout_cleanup_never_closes_ceo_browser_work_key(monkeypatch: pytest.MonkeyPatch) -> None:
    sent = await _run_timeout_cleanup(monkeypatch, {"work_key": "aads-ceo-browser"})
    assert [c for c, _ in sent] == ["browser_eval"]


@pytest.mark.asyncio
async def test_timeout_cleanup_skips_env_configured_default_work_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PC_AGENT_DEFAULT_BROWSER_WORK_KEY", "Custom-Default")
    sent = await _run_timeout_cleanup(monkeypatch, {"work_key": "custom-default"})
    assert [c for c, _ in sent] == ["browser_eval"]


@pytest.mark.asyncio
async def test_timeout_cleanup_closes_ceo_browser_only_with_explicit_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    sent = await _run_timeout_cleanup(
        monkeypatch, {"work_key": "aads-ceo-browser", "allow_default_work_key_close": True}
    )
    assert [c for c, _ in sent] == ["browser_eval", "browser_close_session"]


@pytest.mark.asyncio
async def test_timeout_cleanup_closes_own_work_key(monkeypatch: pytest.MonkeyPatch) -> None:
    sent = await _run_timeout_cleanup(monkeypatch, {"work_key": "my-own-session", "owned_work_key": "my-own-session"})
    assert [c for c, _ in sent] == ["browser_eval", "browser_close_session"]
    assert sent[1][1]["work_key"] == "my-own-session"


@pytest.mark.asyncio
async def test_timeout_cleanup_skips_when_work_key_is_not_the_one_this_call_opened(monkeypatch: pytest.MonkeyPatch) -> None:
    sent = await _run_timeout_cleanup(monkeypatch, {"work_key": "someone-else", "owned_work_key": "my-own-session"})
    assert [c for c, _ in sent] == ["browser_eval"]


@pytest.mark.asyncio
async def test_completion_cleanup_skips_ceo_browser_work_key() -> None:
    manager = PCAgentManager()
    result = await manager._cleanup_browser_session_on_completion(
        agent_id="ceo-pc",
        command_type="browser_eval",
        params={"work_key": "aads-ceo-browser", "close_on_complete": True},
        timeout_seconds=5.0,
    )
    assert result == {"status": "skipped", "reason": "default_ceo_browser_work_key_protected"}
