"""기한이 지난 승인 대기 카드를 'expired' 상태로 닫는다.

만료는 지금까지 `expires_at > now()` 조회 필터로만 표현됐다. 화면에서는 숨겨지지만
행은 `decision='pending'` 으로 남아 통계·감사에서 "대기" 로 읽혔다
(2026-10-15 실측: 기한 지난 pending 124건, expired 0건).

`decision` 컬럼에는 CHECK 제약도 enum 도 없다(migrations/122) — 값 추가에 스키마
변경이 필요 없다.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

EXPIRED_REASON = "expired_without_decision"
EXPIRED_DECIDED_BY = "system:approval_expiry"

# goal_workflow 카드는 제외한다. 그 결정 경로(goal_workflow_approval.decide_*)는
# expires_at 을 보지 않고 `decision='pending'` 인 행만 갱신한다. 카드 기본 만료가
# 10분이라, 여기서 expired 로 닫으면 뒤늦은 승인이 카드 행을 갱신하지 못하고
# change_set 상태만 바뀌어 둘이 어긋난다.
_EXPIRE_SQL = """
UPDATE agent_permission_requests
   SET decision = 'expired',
       reason = $1,
       decided_by = $2,
       decided_at = now(),
       updated_at = now()
 WHERE decision = 'pending'
   AND expires_at < now()
   AND gate_source <> 'goal_workflow'
RETURNING gate_source
"""


async def expire_stale_pending_approvals(pool: Optional[Any] = None) -> int:
    """기한 지난 pending 카드를 expired 로 바꾸고 건수를 돌려준다."""
    if pool is None:
        from app.core.db_pool import get_pool

        pool = get_pool()
    rows = await pool.fetch(_EXPIRE_SQL, EXPIRED_REASON, EXPIRED_DECIDED_BY)
    if rows:
        by_source: dict[str, int] = {}
        for r in rows:
            key = str(r["gate_source"] or "")
            by_source[key] = by_source.get(key, 0) + 1
        logger.info("approval_expiry_closed count=%s by_gate_source=%s", len(rows), by_source)
    return len(rows)
