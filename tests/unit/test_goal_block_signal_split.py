from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

from app.services import goal_dispatch, goal_report


ROOT = Path(__file__).resolve().parents[2]


class _Conn:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []

    async def execute(self, query: str, *args):
        self.calls.append((query, args))
        return "UPDATE 1"


def test_report_uses_explicit_block_signal_only() -> None:
    source = inspect.getsource(goal_report.report_goal_events)
    predicate = source[source.index("WHERE ($1::text"):source.index("ORDER BY m.updated_at")]

    assert "m.dispatch_blocked_at IS NOT NULL" in predicate
    assert "m.dispatch_note IS NOT NULL" not in predicate


def test_board_uses_explicit_block_signal_only() -> None:
    source = (ROOT / "app" / "routers" / "goals.py").read_text(encoding="utf-8")

    assert 'elif r["dispatch_blocked_at"]:' in source
    assert 'elif r["dispatch_note"]:' not in source


def test_answer_clears_note_and_block_signal_together() -> None:
    conn = _Conn()

    asyncio.run(goal_dispatch._clear_answered_block(conn, "milestone-id"))

    assert len(conn.calls) == 1
    query, args = conn.calls[0]
    assert "dispatch_blocked_at = NULL" in query
    assert "dispatch_note = NULL" in query
    assert args == ("milestone-id",)


def test_answer_is_checked_before_retry_limit_blocks() -> None:
    source = inspect.getsource(goal_dispatch.dispatch_pending_milestones)

    assert source.index("if answered:") < source.index("if count >= _MAX_DISPATCH:")
    answered = source[source.index("if answered:"):source.index("if count >= _MAX_DISPATCH:")]
    assert "_clear_answered_block" in answered


def test_blocked_goal_with_in_progress_milestone_is_dispatched() -> None:
    source = inspect.getsource(goal_dispatch.dispatch_pending_milestones)

    assert "m.status = 'in_progress'" in source
    assert "g.status IN ('active', 'blocked')" in source


def test_migration_is_idempotent_and_non_destructive() -> None:
    sql = (ROOT / "scripts" / "sql" / "20260918_milestone_dispatch_blocked_at.sql").read_text(
        encoding="utf-8"
    ).upper()

    assert "ADD COLUMN IF NOT EXISTS DISPATCH_BLOCKED_AT" in " ".join(sql.split())
    assert "DROP " not in sql
    assert "DELETE " not in sql
