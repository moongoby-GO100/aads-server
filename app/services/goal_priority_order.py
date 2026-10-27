"""목표 처리 순서(goals.priority_order) — 초기값 채우기와 순서 변경.

작을수록 먼저, 테넌트 단위 전체 순서다. 발송기는 등급(priority_rank) 다음에
이 값을 본다. NULL 은 맨 뒤.
"""
from __future__ import annotations

from typing import Any, Sequence

from app.services.goal_signals import priority_rank_case_sql

ORDERABLE_STATUSES = ("draft", "active", "blocked")


class ReorderError(Exception):
    """reorder 입력 오류. status_code 는 라우터가 HTTP 로 옮긴다."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


# 값이 이미 있으면 건드리지 않는다. 새로 채울 행은 테넌트별 기존 최댓값 뒤에 이어 붙여
# 재실행해도 기존 순서와 겹치지 않는다(처음 실행이면 최댓값이 0 이라 1..n).
BACKFILL_PRIORITY_ORDER_SQL = f"""
WITH base AS (
    SELECT tenant_id, COALESCE(MAX(priority_order), 0) AS max_order
    FROM goals
    GROUP BY tenant_id
),
ranked AS (
    SELECT g.id,
           b.max_order + ROW_NUMBER() OVER (
               PARTITION BY g.tenant_id
               ORDER BY {priority_rank_case_sql("g.priority")}, g.created_at, g.id
           ) AS new_order
    FROM goals g
    JOIN base b ON b.tenant_id IS NOT DISTINCT FROM g.tenant_id
    WHERE g.priority_order IS NULL
      AND g.status IN ('draft', 'active', 'blocked')
)
UPDATE goals g
SET priority_order = ranked.new_order
FROM ranked
WHERE g.id = ranked.id AND g.priority_order IS NULL
"""


async def backfill_priority_order(conn: Any) -> str:
    """priority_order 가 NULL 인 활성·초안·blocked 목표에만 초기값을 넣는다(멱등)."""
    return await conn.execute(BACKFILL_PRIORITY_ORDER_SQL)


async def set_goal_order(conn: Any, goal_id: str, tenant_id: str, priority_order: int) -> bool:
    """한 목표의 순서 값을 바꾼다. 테넌트가 다르거나 없으면 False."""
    row = await conn.fetchrow(
        "UPDATE goals SET priority_order = $3, updated_at = NOW() "
        "WHERE id = $1::uuid AND tenant_id = $2::uuid RETURNING id",
        goal_id, tenant_id, priority_order,
    )
    return row is not None


async def reorder_goals(conn: Any, tenant_id: str, goal_ids: Sequence[str]) -> int:
    """주어진 순서대로 1..n 을 부여한다. 한 트랜잭션, 전부 성공하거나 전부 실패.

    중복 id → 422, 없는 id·다른 테넌트 id → 404(존재 여부를 흘리지 않는다).
    """
    ids = [str(g).lower() for g in goal_ids]
    if not ids:
        raise ReorderError(422, "goal_ids_required")
    if len(set(ids)) != len(ids):
        raise ReorderError(422, "duplicate_goal_ids")
    async with conn.transaction():
        found = await conn.fetch(
            "SELECT id::text AS id FROM goals "
            "WHERE id = ANY($1::uuid[]) AND tenant_id = $2::uuid FOR UPDATE",
            ids, tenant_id,
        )
        if len(found) != len(ids):
            raise ReorderError(404, "goal_not_found")
        await conn.execute(
            "UPDATE goals g SET priority_order = o.ord, updated_at = NOW() "
            "FROM unnest($1::uuid[]) WITH ORDINALITY AS o(id, ord) "
            "WHERE g.id = o.id AND g.tenant_id = $2::uuid",
            ids, tenant_id,
        )
    return len(ids)
