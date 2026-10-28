"""목표 하나의 실패가 전 프로젝트 목표 진행 주기를 멈추지 않는지 지킨다.

2026-10-28 실측. 목표 380c796f 가 `ck_goals_owner_required` 를 위반해
`advance_goal` 이 예외를 던졌고, 목록 컴프리헨션이 그 예외를 그대로 올려
75분간 매 주기 dispatch·triage 단계가 실행되지 않았다.
"""
from __future__ import annotations

import ast
import asyncio
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services import goal_manager as gm
from app.services.goal_manager import GoalStateMachine

ROOT = Path(__file__).resolve().parents[2]
MAIN = ROOT / "app" / "main.py"


class _CheckViolationError(Exception):
    """asyncpg.CheckViolationError 대역 — 실제 클래스와 같은 메시지 형태."""


class _Conn:
    def __init__(self, rows):
        self.rows = rows

    async def fetch(self, *_args):
        return self.rows


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *_exc):
                return False

        return _Ctx()


def _machine(monkeypatch, rows, advance_goal):
    machine = GoalStateMachine()
    pool = _Pool(_Conn(rows))
    monkeypatch.setattr(machine, "_pool", AsyncMock(return_value=pool))
    monkeypatch.setattr(machine, "advance_goal", advance_goal)
    traces: list[dict] = []

    async def _trace(run_type, **kwargs):
        traces.append({"run_type": run_type, **kwargs})

    monkeypatch.setattr(machine, "_trace", _trace)
    return machine, traces


def test_one_failing_goal_does_not_stop_the_others(monkeypatch, caplog):
    rows = [
        {"id": "g1", "project": "AADS"},
        {"id": "g2", "project": "AADS"},
        {"id": "g3", "project": "GO100"},
    ]
    seen: list[str] = []

    async def advance_goal(goal_id):
        seen.append(goal_id)
        if goal_id == "g2":
            raise _CheckViolationError(
                'new row for relation "goals" violates check constraint "ck_goals_owner_required"'
            )
        return {"goal_id": goal_id, "advanced": True}

    machine, traces = _machine(monkeypatch, rows, advance_goal)
    with caplog.at_level("WARNING", logger=gm.logger.name):
        result = asyncio.run(machine.advance_active_goals(None))

    assert seen == ["g1", "g2", "g3"]
    assert result["checked"] == 3
    assert result["advanced"] == 2
    assert result["failed"] == 1
    assert result["gated"] == {}
    failed = [r for r in result["results"] if r.get("error")]
    assert len(failed) == 1
    assert failed[0]["goal_id"] == "g2"
    assert failed[0]["advanced"] is False
    assert "ck_goals_owner_required" in failed[0]["error"]
    assert any("goal_advance_goal_failed" in r.getMessage() and "g2" in r.getMessage()
               for r in caplog.records)
    assert traces[-1]["metadata"]["failed"] == 1


def test_error_text_is_truncated_to_200_chars(monkeypatch):
    async def advance_goal(_goal_id):
        raise RuntimeError("x" * 500)

    machine, _ = _machine(monkeypatch, [{"id": "g1", "project": "AADS"}], advance_goal)
    result = asyncio.run(machine.advance_active_goals(None))

    assert result["failed"] == 1
    assert len(result["results"][0]["error"]) == 200


def test_all_goals_succeed_reports_zero_failed(monkeypatch):
    async def advance_goal(goal_id):
        return {"goal_id": goal_id, "advanced": False}

    machine, _ = _machine(monkeypatch, [{"id": "g1", "project": "AADS"}], advance_goal)
    result = asyncio.run(machine.advance_active_goals(None))

    assert result["failed"] == 0
    assert result["checked"] == 1


def _load_cycle(monkeypatch, advance_active_goals):
    """main.py 안쪽에 중첩된 `_run_goal_control_cycle` 을 꺼내 가짜 모듈 위에서 실행한다."""
    tree = ast.parse(MAIN.read_text(encoding="utf-8"))
    node = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "_run_goal_control_cycle"
    )
    module = ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[]))
    logger = MagicMock()
    namespace = {"logger": logger, "_GOAL_STAGE_TIMEOUT": 5.0}
    exec(compile(module, str(MAIN), "exec"), namespace)

    calls = {
        "dispatch": AsyncMock(return_value={"sent": 1, "gave_up": 0}),
        "triage": AsyncMock(return_value=None),
    }

    lock_conn = MagicMock()
    lock_conn.fetchval = AsyncMock(return_value=True)
    lock_conn.execute = AsyncMock()
    pool = MagicMock()
    pool.acquire = AsyncMock(return_value=lock_conn)
    pool.release = AsyncMock()

    def _fake(name, **attrs):
        mod = types.ModuleType(name)
        for key, value in attrs.items():
            setattr(mod, key, value)
        monkeypatch.setitem(sys.modules, name, mod)

    _fake("app.core.db_pool", get_pool=lambda: pool)
    _fake("app.services.goal_link_reconciler", reconcile=AsyncMock(return_value={"repaired": 0}))
    _fake("app.services.release_evidence",
          reconcile_release_links=AsyncMock(return_value={"completed": 0}))
    _fake("app.services.goal_manager",
          goal_state_machine=types.SimpleNamespace(advance_active_goals=advance_active_goals))
    _fake("app.services.goal_dispatch",
          dispatch_pending_milestones=calls["dispatch"],
          triage_unowned_milestones=calls["triage"])
    _fake("app.services.milestone_review",
          ask_pending_reviews=AsyncMock(return_value={"asked": 0, "escalated": 0}))
    _fake("app.services.orchestration_limits", check_deadlines=AsyncMock())
    _fake("app.services.goal_report", report_goal_events=AsyncMock(return_value={"reported": 0}))
    return namespace["_run_goal_control_cycle"], calls, logger


def test_cycle_still_dispatches_and_triages_when_advance_raises(monkeypatch):
    advance = AsyncMock(side_effect=_CheckViolationError("ck_goals_owner_required"))
    cycle, calls, logger = _load_cycle(monkeypatch, advance)

    asyncio.run(cycle())

    advance.assert_awaited_once()
    calls["dispatch"].assert_awaited_once()
    calls["triage"].assert_awaited_once()
    events = [c.args[0] for c in logger.warning.call_args_list]
    assert "goal_advance_failed" in events
    assert "goal_control_cycle_failed" not in events
    assert logger.info.call_args_list[-1].args[0] == "goal_control_cycle_done"


def test_cycle_reports_advance_failed_count_on_success_path(monkeypatch):
    advance = AsyncMock(return_value={"checked": 3, "advanced": 2, "failed": 1})
    cycle, calls, logger = _load_cycle(monkeypatch, advance)

    asyncio.run(cycle())

    calls["dispatch"].assert_awaited_once()
    calls["triage"].assert_awaited_once()
    done = logger.info.call_args_list[-1]
    assert done.args[0] == "goal_control_cycle_done"
    assert done.kwargs["advance_failed"] == 1
    assert done.kwargs["checked"] == 3


def test_source_keeps_stage_order_and_does_not_touch_constraint():
    source = MAIN.read_text(encoding="utf-8")
    assert (
        source.index("reconcile_release_links(")
        < source.index("advance_active_goals(None)")
        < source.index("dispatch_pending_milestones(None)")
        < source.index("triage_unowned_milestones(None)")
    )
    manager = (ROOT / "app" / "services" / "goal_manager.py").read_text(encoding="utf-8")
    assert "DROP CONSTRAINT" not in manager
    assert "NOT VALID" not in manager


@pytest.mark.parametrize("name", ["goal_advance_goal_failed"])
def test_failure_event_is_logged_by_name(name):
    manager = (ROOT / "app" / "services" / "goal_manager.py").read_text(encoding="utf-8")
    assert name in manager
