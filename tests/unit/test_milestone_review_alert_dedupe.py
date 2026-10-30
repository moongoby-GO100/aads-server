"""대표님 완료 확인 알림은 마일스톤별로 중복을 판정한다."""
import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from app.core import db_pool
from app.services import milestone_review, ohvis_alert, orchestration_limits


@pytest.mark.parametrize("milestone_title", ["D0 기반 정비", "긴 제목" * 100])
def test_ceo_alert_titles_are_distinct_and_stable(monkeypatch, milestone_title):
    rows = [
        {
            "milestone_id": milestone_id,
            "title": milestone_title,
            "goal_id": "goal",
            "goal_title": "목표명",
            "project": "AADS",
            "owner": "CTO",
            "owner_session": "lead",
            "review_ask_count": 0,
        }
        for milestone_id in (
            "447ba817-0000-0000-0000-000000000001",
            "557ba817-0000-0000-0000-000000000002",
        )
    ]
    monkeypatch.setattr(milestone_review, "lead_self_approval_evidence", AsyncMock(return_value={"ok": False, "jobs": [], "reason": "증거 부족"}))
    conn = AsyncMock()
    conn.fetch.return_value = rows
    conn.fetchval.return_value = "CTO"

    class Pool:
        @asynccontextmanager
        async def acquire(self):
            yield conn

    monkeypatch.setattr(db_pool, "get_pool", lambda: Pool())
    monkeypatch.setattr(
        orchestration_limits, "goal_paused", AsyncMock(return_value=(False, ""))
    )
    monkeypatch.setattr(
        milestone_review, "_lead_session", AsyncMock(return_value="lead")
    )
    monkeypatch.setattr(milestone_review, "_alt_reviewer", AsyncMock(return_value=None))
    notify = AsyncMock(return_value=True)
    monkeypatch.setattr(ohvis_alert, "notify", notify)

    assert asyncio.run(milestone_review.ask_pending_reviews("AADS")) == {
        "asked": 0, "escalated": 2,
    }
    assert notify.await_count == 2
    first_cycle = notify.await_args_list[:]
    titles = [call.args[0] for call in first_cycle]
    assert titles[0] != titles[1]
    for row, call in zip(rows, first_cycle):
        title, body = call.args
        assert len(title) <= 200
        assert title.startswith("✅ [AADS] 완료 확인 요청 · ")
        assert title.endswith(f"({row['milestone_id'][:8]})")
        assert body == (
            f"목표명\n{milestone_title}\n담당 CTO — 대표님 확인이 필요합니다"
        )
        assert call.kwargs == {
            "severity": ohvis_alert.WARNING, "category": "goal", "dedupe_minutes": 30,
        }

    asyncio.run(milestone_review.ask_pending_reviews("AADS"))
    assert notify.await_count == 4
    assert [call.args[0] for call in notify.await_args_list[2:]] == titles
    assert conn.execute.await_count == 4
