"""착수 지시는 배포 시각보다 마일스톤 배열 순서를 먼저 따른다."""
from __future__ import annotations

import re


def _database_order(rows: list[dict], query: str) -> list[dict]:
    """테스트 DB의 ORDER BY 결과를 재현한다."""
    order = re.search(r"ORDER BY\s+(.+?)\s+LIMIT\s+20", query, re.S | re.I)
    assert order, "착수 조회에 ORDER BY 절이 없다"
    terms = [term.strip().lower() for term in order.group(1).split(",")]
    assert terms == ["m.sequence_order", "m.dispatched_at nulls first"]
    return sorted(
        rows,
        key=lambda row: (
            row["sequence_order"],
            row["dispatched_at"] is not None,
            row["dispatched_at"] or "",
        ),
    )


def test_sequence_order_precedes_dispatch_history() -> None:
    """앞 배열의 배포 이력 행이 뒤 배열의 미배포 행보다 먼저 조회된다."""
    query = """
        SELECT m.sequence_order, m.dispatched_at
        FROM milestones m JOIN goals g ON g.id = m.goal_id
        WHERE m.status = 'in_progress' AND g.status IN ('active', 'blocked')
        ORDER BY m.sequence_order, m.dispatched_at NULLS FIRST
        LIMIT 20
    """
    rows = [
        {"sequence_order": 2, "dispatched_at": None, "id": "later-unstarted"},
        {"sequence_order": 1, "dispatched_at": "2026-09-20T10:00:00", "id": "first-dispatched"},
    ]

    ordered = _database_order(rows, query)

    assert [row["id"] for row in ordered] == ["first-dispatched", "later-unstarted"]


# --- 목표 사이 라운드로빈 (AADS-GOALDISPATCH-CANDIDATE-STARVATION-20260929) ---

def _candidate_sql() -> str:
    import inspect

    from app.services import goal_dispatch

    source = inspect.getsource(goal_dispatch.dispatch_pending_milestones)
    source = re.sub(r"--[^\n]*", "", source)
    match = re.search(
        r"SELECT milestone_id, milestone_title.*?ORDER BY\s+goal_rn.*?LIMIT\s+\$3",
        source,
        re.S,
    )
    assert match, "착수 후보 조회 SQL 을 찾지 못했다"
    return match.group(0)


def _simulate_window(rows: list[dict], limit: int) -> list[dict]:
    """SQL 의 goal_rn 부여 + 바깥 ORDER BY + LIMIT 을 그대로 재현한다."""
    by_goal: dict[str, list[dict]] = {}
    for row in sorted(rows, key=lambda r: (r["sequence_order"], r["dispatched_at"] is not None, r["dispatched_at"] or "")):
        by_goal.setdefault(row["goal"], []).append(row)
    ranked = [dict(row, goal_rn=i) for goal_rows in by_goal.values() for i, row in enumerate(goal_rows, 1)]
    ranked.sort(
        key=lambda r: (
            r["goal_rn"],
            r["dispatched_at"] is not None,
            r["dispatched_at"] or "",
            r["sequence_order"],
        )
    )
    return ranked[:limit]


def test_candidate_sql_is_round_robin_over_goals() -> None:
    sql = _candidate_sql()
    assert re.search(r"PARTITION BY g\.id\s+ORDER BY m\.sequence_order, m\.dispatched_at NULLS FIRST", sql, re.I)
    assert re.search(r"\)\s*AS goal_rn", sql, re.I)
    outer = re.search(r"\)\s*c\s.*?ORDER BY\s+(.+?)\s+LIMIT\s+\$3", sql, re.S | re.I)
    assert outer, "바깥 ORDER BY 를 찾지 못했다"
    clause = re.sub(r"\s+", " ", outer.group(1)).lower()
    assert clause == (
        "goal_rn, coalesce(dispatched_at, load_deferred_since) asc nulls first, "
        "sequence_order"
    )
    for column in (
        "milestone_id", "milestone_title", "description", "completion_criteria",
        "dispatch_count", "dispatched_at", "dispatched_session_id",
        "load_deferred_since", "dispatch_blocked_at", "goal_title", "project",
        "goal_id", "owner_role_key", "goal_lead_session_id", "session_id",
    ):
        assert re.search(rf"\b{column}\b", sql.split("FROM (")[0]), f"바깥 SELECT 에 {column} 이 없다"


def test_candidate_limit_is_env_configurable() -> None:
    from app.services import goal_dispatch

    assert isinstance(goal_dispatch._CANDIDATE_LIMIT, int)
    assert goal_dispatch._CANDIDATE_LIMIT >= 47


def test_low_sequence_goal_does_not_starve_other_goal() -> None:
    """A 목표 seq 가 전부 작아도 B 목표의 선두가 창 앞쪽에 함께 들어온다."""
    rows = [
        {"id": "A1", "goal": "A", "sequence_order": 1, "dispatched_at": None},
        {"id": "A2", "goal": "A", "sequence_order": 2, "dispatched_at": None},
        {"id": "A3", "goal": "A", "sequence_order": 3, "dispatched_at": None},
        {"id": "B1", "goal": "B", "sequence_order": 10, "dispatched_at": None},
        {"id": "B2", "goal": "B", "sequence_order": 11, "dispatched_at": None},
        {"id": "B3", "goal": "B", "sequence_order": 12, "dispatched_at": None},
    ]

    window = _simulate_window(rows, limit=2)

    assert {r["id"] for r in window} == {"A1", "B1"}


def test_sequence_order_preserved_within_goal() -> None:
    rows = [
        {"id": "A3", "goal": "A", "sequence_order": 3, "dispatched_at": None},
        {"id": "A1", "goal": "A", "sequence_order": 1, "dispatched_at": "2026-09-20T10:00:00"},
        {"id": "A2", "goal": "A", "sequence_order": 2, "dispatched_at": None},
        {"id": "B1", "goal": "B", "sequence_order": 1, "dispatched_at": None},
    ]

    window = _simulate_window(rows, limit=10)

    assert [r["id"] for r in window if r["goal"] == "A"] == ["A1", "A2", "A3"]
    assert [r["id"] for r in window][:2] == ["B1", "A1"]
