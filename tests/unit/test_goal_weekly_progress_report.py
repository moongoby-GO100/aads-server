from __future__ import annotations

from datetime import datetime
from inspect import getsource
from pathlib import Path
from zoneinfo import ZoneInfo

from app.services import goal_report


def test_weekly_event_key_uses_kst_iso_week() -> None:
    # Sunday UTC is already Monday in Korea, so the KST week must win.
    observed = datetime(2026, 9, 20, 15, 30, tzinfo=ZoneInfo("UTC"))
    assert goal_report._weekly_event_key(observed) == "weekly_progress:2026-W39"


def test_weekly_report_is_session_only_and_idempotent() -> None:
    source = getsource(goal_report.report_weekly_goal_progress)

    assert "INSERT INTO chat_messages" in source
    assert "goal_weekly_report" in source
    assert "ON CONFLICT (goal_id, subject_id, event) DO NOTHING" in source
    assert "_telegram(" not in source
    assert "notify(" not in source


def test_scheduler_registers_monday_kst_goal_report() -> None:
    source = Path("app/main.py").read_text(encoding="utf-8")

    assert 'id="weekly_goal_progress"' in source
    assert 'CronTrigger(day_of_week="mon", hour=0, minute=0, timezone="UTC")' in source
