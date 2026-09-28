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
