"""개입 — 대표님이 전체에 한 번에 닿는 길.

## 왜 필요한가

지금은 창을 하나씩 열어 말을 거는 것 말고는 방법이 없다. 주도가 이미
담당 셋에게 지시를 뿌렸다면 **그들에게는 닿지 않는다.** 잘못된 방향으로
도는 작업을 한 번에 멈출 수단도 없다.

그리고 대표님 지시와 주도 지시가 충돌하면 담당은 **나중에 온 것**을
따른다. 우선순위 개념이 없다.

## 세 가지

    halt()      전체 정지 — 변경과 지시를 막는다. 조사·조회는 계속된다.
    direct()    방향 지시 — 주도와 담당 전원에게 동시에. 기존 지시보다 우선.
    rewind()    되돌리기 — 마일스톤을 pending 으로, 발송 기록 초기화.

`halt()` 는 플래그만 세운다. 실제로 막는 것은 `direction_guard` 다 —
막는 곳이 한 군데여야 우회할 틈이 안 생긴다.
"""
from __future__ import annotations

import os
from typing import Any, Optional

import structlog

logger = structlog.get_logger(__name__)

_MAX_FANOUT = int(os.getenv("GOAL_INTERVENE_MAX_FANOUT", "10"))

_PRIORITY_NOTE = (
    "\n\n— 이것은 **대표님 지시**입니다. 주도 지시와 어긋나면 대표님 지시를 "
    "따르고, 주도에게 어긋난 점을 알리세요. 멈추지 마세요."
)


async def halt(on: bool, reason: str = "") -> dict[str, Any]:
    """전체 정지를 걸거나 푼다."""
    from app.core.db_pool import get_pool
    from app.services.direction_guard import HALT_KEY

    pool = get_pool()
    # 유니크 키는 (category, key) 이고 value 는 jsonb 다. 처음에 (key) 로
    # 썼다가 실측에서 잡았다 — 스키마를 보지 않고 짐작한 결과다.
    await pool.execute(
        """
        INSERT INTO system_memory (category, key, value, updated_at, updated_by)
        VALUES ('goal_orchestration', $1, $2::jsonb, NOW(), 'ceo')
        ON CONFLICT (category, key) DO UPDATE
           SET value = EXCLUDED.value, updated_at = NOW(), updated_by = 'ceo'
        """,
        HALT_KEY, "true" if on else "false",
    )
    logger.warning("goal_orchestration_halt", on=on, reason=reason[:200])
    return {"halted": on, "reason": reason}


async def _owner_sessions(conn: Any, goal_id: str) -> list[dict]:
    rows = await conn.fetch(
        """
        SELECT s.id::text AS id, COALESCE(s.role_key, '') AS role_key, s.title
        FROM goal_task_links l
        JOIN chat_sessions s ON s.id = l.task_id::uuid
        WHERE l.goal_id = $1::uuid AND l.task_type = 'chat_session'
          AND COALESCE(l.link_state, 'active') = 'active'
        ORDER BY (s.role_key LIKE '%Lead') DESC, s.role_key
        """,
        goal_id,
    )
    return [dict(r) for r in rows]


async def direct(goal_id: str, message: str, roles: Optional[list[str]] = None) -> dict[str, Any]:
    """목표에 묶인 담당들에게 대표님 지시를 동시에 넣는다.

    주도를 거치지 않는다. 주도가 이미 뿌린 지시와 어긋날 수 있는데, 그때
    **대표님 지시가 이긴다**는 것을 메시지에 같이 적는다.
    """
    from app.core.db_pool import get_pool
    from app.services import chat_service as cs

    pool = get_pool()
    async with pool.acquire() as conn:
        targets = await _owner_sessions(conn, goal_id)

    if roles:
        want = {r.strip() for r in roles if r.strip()}
        targets = [t for t in targets if t["role_key"] in want]

    if len(targets) > _MAX_FANOUT:
        # 한 번에 열 개 넘는 세션을 깨우면 LLM 호출이 한꺼번에 몰린다.
        targets = targets[:_MAX_FANOUT]

    body = message.rstrip() + _PRIORITY_NOTE
    sent, failed = [], []
    for t in targets:
        try:
            async for _c in cs.send_message_stream(
                session_id=t["id"], content=body,
                intent_override="system_trigger", response_mode="quality",
            ):
                pass
            sent.append(t["role_key"] or t["id"][:8])
        except Exception as exc:
            logger.warning(
                "goal_direct_failed", role=t["role_key"], error=str(exc)[:160]
            )
            failed.append(t["role_key"] or t["id"][:8])

    logger.info("goal_direct", goal=goal_id[:8], sent=len(sent), failed=len(failed))
    return {"sent": sent, "failed": failed}


async def rewind(milestone_id: str, reason: str = "") -> dict[str, Any]:
    """마일스톤을 되돌린다. 발송 기록도 지워야 다시 지시가 나간다.

    기록을 안 지우면 `dispatch_count` 가 한도에 걸린 채로 남아서 되돌려도
    아무 일이 일어나지 않는다 — 조용히 멈춘다.
    """
    from app.core.db_pool import get_pool

    pool = get_pool()
    # 지우기 **전** 값을 같이 돌려받는다. CTE `prev` 는 UPDATE 전 스냅샷을
    # 들고 있으므로 `RETURNING` 에서 참조하면 옛 값이 나온다 — 발송 기록이
    # 언제 것이었는지를 남겨야 사이클 로그와 맞춰 볼 수 있다.
    row = await pool.fetchrow(
        """
        WITH prev AS (
            SELECT id, dispatched_at, dispatch_count
              FROM milestones WHERE id = $1::uuid
        )
        UPDATE milestones m
           SET status = 'pending', started_at = NULL, completed_at = NULL,
               dispatched_at = NULL, dispatched_session_id = NULL,
               dispatch_count = 0,
               dispatch_note = NULLIF($2, ''),
               updated_at = NOW()
          FROM prev
         WHERE m.id = prev.id
        RETURNING m.title, m.goal_id::text AS goal_id,
                  prev.dispatched_at AS prev_dispatched_at,
                  prev.dispatch_count AS prev_dispatch_count
        """,
        milestone_id, reason,
    )
    if not row:
        return {"error": "milestone_not_found"}

    # 발송 기록을 되돌렸다는 자취. 없으면 다음 사이클이 같은 마일스톤을
    # 다시 집었을 때 "왜 또 나갔는가" 를 로그만으로 설명할 수 없다.
    from app.services.goal_dispatch import note_record_reset

    note_record_reset(
        milestone_id,
        reason=f"rewind(pending 으로 되돌림) — {reason}".strip(" —"),
        where="app/services/goal_intervene.py:rewind",
        prev_dispatched_at=row["prev_dispatched_at"],
        prev_dispatch_count=row["prev_dispatch_count"],
    )

    # 같은 사건을 다시 보고할 수 있게 기록도 지운다.
    await pool.execute(
        "DELETE FROM goal_report_log WHERE subject_id = $1::uuid", milestone_id
    )
    logger.info("goal_rewind", milestone=milestone_id[:8], reason=reason[:120])
    return {"milestone": row["title"], "goal_id": row["goal_id"], "status": "pending"}
