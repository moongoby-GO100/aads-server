"""배달앱 자동수집 게이트: PC Agent 오프라인·쿨다운·중복 실행·사용자 조치 상태 전이."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.services import delivery_collection_gate as gate

KST = timezone(timedelta(hours=9))
NOW = datetime(2026, 10, 14, 13, 0, tzinfo=KST)
ROOT = Path(__file__).resolve().parents[2]


def _row(service="baemin", status="failed", code="", minutes_ago=10, business="b1", branch="강남"):
    return {
        "service": service,
        "business_id": business,
        "branch": branch,
        "status": status,
        "error_code": code,
        "updated_at": (NOW - timedelta(minutes=minutes_ago)).isoformat(),
    }


def test_agent_offline_skips_without_looking_at_rows():
    decision = gate.evaluate_service("baemin", [_row(status="succeeded")], agent_online=False, now=NOW)
    assert decision["run"] is False
    assert decision["state"] == gate.GATE_STATE_AGENT_OFFLINE


def test_pause_switch_wins_over_agent_state():
    decision = gate.evaluate_service("baemin", [], agent_online=True, paused=True, pause_reason="ceo", now=NOW)
    assert decision["state"] == gate.GATE_STATE_PAUSED
    assert decision["reason"] == "ceo"


def test_no_history_is_runnable():
    assert gate.evaluate_service("yogiyo", [], agent_online=True, now=NOW)["run"] is True


def test_failure_cooldown_blocks_then_releases():
    fresh = gate.evaluate_service("baemin", [_row(code="ATTEMPT_TIMEOUT", minutes_ago=10)], agent_online=True, now=NOW)
    assert fresh["run"] is False and fresh["state"] == gate.GATE_STATE_COOLDOWN
    assert fresh["cooldown_until"]
    aged = gate.evaluate_service("baemin", [_row(code="ATTEMPT_TIMEOUT", minutes_ago=61)], agent_online=True, now=NOW)
    assert aged["run"] is True


def test_duplicate_run_blocked_while_running_but_stale_running_is_released():
    running = gate.evaluate_service("baemin", [_row(status="running", minutes_ago=3)], agent_online=True, now=NOW)
    assert running["run"] is False and running["state"] == gate.GATE_STATE_ALREADY_RUNNING
    queued = gate.evaluate_service("baemin", [_row(status="queued", minutes_ago=1)], agent_online=True, now=NOW)
    assert queued["state"] == gate.GATE_STATE_ALREADY_RUNNING
    stale = gate.evaluate_service("baemin", [_row(status="running", minutes_ago=40)], agent_online=True, now=NOW)
    assert stale["run"] is True


def test_user_action_codes_never_expire_and_are_not_retried():
    for code, kind in (
        ("PC_AGENT_LOGIN_REQUIRED", "login_required"),
        ("DDANGYO_NUMERIC_CAPTCHA_REQUIRED", "captcha"),
        ("PORTAL_BLOCKED", "portal_blocked"),
    ):
        old = gate.evaluate_service(
            "ddangyo", [_row(service="ddangyo", status="action_required", code=code, minutes_ago=60 * 24 * 7)],
            agent_online=True, now=NOW,
        )
        assert old["run"] is False
        assert old["state"] == gate.GATE_STATE_USER_ACTION
        assert old["user_action"]["kind"] == kind
        assert old["user_action"]["message"]


def test_user_action_clears_after_newer_success():
    rows = [
        _row(status="action_required", code="PC_AGENT_LOGIN_REQUIRED", minutes_ago=500),
        _row(status="succeeded", minutes_ago=5),
    ]
    assert gate.evaluate_service("baemin", rows, agent_online=True, now=NOW)["run"] is True


def test_state_transitions_failed_to_user_action_to_ready():
    key = dict(business="b1", branch="강남")
    seq = [
        (_row(status="failed", code="ATTEMPT_TIMEOUT", minutes_ago=5, **key), gate.GATE_STATE_COOLDOWN),
        (_row(status="action_required", code="PC_AGENT_SESSION_REQUIRED", minutes_ago=4, **key), gate.GATE_STATE_USER_ACTION),
        (_row(status="succeeded", minutes_ago=1, **key), gate.GATE_STATE_READY),
    ]
    for row, expected in seq:
        assert gate.evaluate_service("baemin", [row], agent_online=True, now=NOW)["state"] == expected


def test_one_blocked_store_does_not_block_the_other_stores():
    rows = [
        _row(status="action_required", code="PORTAL_BLOCKED", branch="강남"),
        _row(status="succeeded", branch="홍대", business="b2"),
    ]
    decision = gate.evaluate_service("baemin", rows, agent_online=True, now=NOW)
    assert decision["run"] is True and decision["blocked_scopes"] == 1


def test_scope_gate_targets_a_single_store():
    rows = [
        _row(status="action_required", code="PORTAL_BLOCKED", branch="강남"),
        _row(status="succeeded", branch="홍대", business="b2"),
    ]
    assert gate.scope_gate(rows, "baemin", "b1", "강남", now=NOW)["state"] == gate.GATE_STATE_USER_ACTION
    assert gate.scope_gate(rows, "baemin", "b2", "홍대", now=NOW)["run"] is True


def test_user_actions_listing_for_screen_alert():
    rows = [
        _row(service="coupangeats", status="action_required", code="PORTAL_BLOCKED"),
        _row(service="yogiyo", status="succeeded"),
    ]
    actions = gate.user_actions_from_statuses(rows, now=NOW)
    assert [a["service"] for a in actions] == ["coupangeats"]
    assert actions[0]["csv_fallback"] is True


def test_record_gate_state_throttles_and_survives_db_failure(monkeypatch):
    gate._last_written.clear()
    calls = []

    class Pool:
        async def execute(self, *args):
            calls.append(args)

    import app.core.db_pool as db_pool

    monkeypatch.setattr(db_pool, "get_pool", lambda: Pool())
    decision = {"run": False, "state": gate.GATE_STATE_AGENT_OFFLINE, "reason": "PC_AGENT_OFFLINE"}
    assert asyncio.run(gate.record_gate_state("baemin", decision)) is True
    assert asyncio.run(gate.record_gate_state("baemin", decision)) is False
    assert len(calls) == 1

    gate._last_written.clear()

    def boom():
        raise RuntimeError("no pool")

    monkeypatch.setattr(db_pool, "get_pool", boom)
    assert asyncio.run(gate.record_gate_state("baemin", decision)) is False


def test_scheduler_wiring_contract():
    main_src = (ROOT / "app/main.py").read_text(encoding="utf-8")
    assert "delivery_collection_gate as gate_mod" in main_src
    assert "delivery_auto_collect_skip: collection_gate" in main_src
    assert "delivery_auto_collect_skip: user_action_required" in main_src
    script_src = (ROOT / "scripts/yeoljeong_auto_collect.py").read_text(encoding="utf-8")
    assert "_delivery_claim_gate(item, user)" in script_src
    ui_src = (ROOT / "app/static/apps/obys/index.html").read_text(encoding="utf-8")
    assert "deliveryUserActionBanner" in ui_src
    assert "정산 CSV 업로드" in ui_src
