"""오케스트레이션이 스스로를 망가뜨리지 않게 하는 상한들.

담당 8명이 동시에 돌고 A/B 로 갈래가 늘면 **곱해진다.** 그 곱셈이 두
군데를 친다 — 비용과 서버 부하다.

## 비용

CEO 절대 규칙: "LLM 15회/task, 비용 효율 최우선".

2026-09-14 실측: 어시스턴트 메시지 1건에 **1.06 USD**. 한 마일스톤에서
담당이 도구를 **127회** 부른 기록이 있다.

    80%   주도 채팅창에 경고
    100%  **새 지시를 멈춘다.** 진행 중인 것은 끝까지 둔다.

진행 중인 것을 끊지 않는 이유 — 끊으면 쓴 비용이 버려진다. 멈출 것은
**다음 지시**다.

## 부하

오늘 contabo14 부하가 **29** 였다(코어 8). 주범은 백테스트 4개 동시
실행이고, A/B 는 그것을 설계로 한다. **오케스트레이션이 수집기를 죽일
수 있다** — 오늘 실제로 일어난 일이다.

임계를 넘으면 지시를 **미룬다.** 취소가 아니므로 `dispatch_count` 를
올리지 않는다. 부하 때문에 미룬 것을 재시도로 세면 한도가 헛되이 닳는다.

부하 조회는 60초 캐시한다. 2~3분마다 도는 사이클이 매번 SSH 를 걸면
그 자체가 부하다.
"""
from __future__ import annotations

import os
import time
from typing import Optional, Tuple

import structlog

logger = structlog.get_logger(__name__)

_DEFAULT_COST_LIMIT = float(os.getenv("GOAL_COST_LIMIT_USD", "20"))
_MAX_LOAD_RATIO = float(os.getenv("GOAL_DISPATCH_MAX_LOAD_RATIO", "2.0"))
_LOAD_CACHE_TTL = float(os.getenv("GOAL_LOAD_CACHE_TTL", "60"))

_load_cache: dict[str, tuple[float, Optional[float]]] = {}


async def refresh_goal_cost(goal_id: str) -> float:
    """목표에 묶인 세션들의 오늘 비용을 합산해 기록한다."""
    from app.core.db_pool import get_pool

    pool = get_pool()
    # **오케스트레이션이 쓴 비용만 센다.**
    #
    # 처음에는 목표 생성 시점부터 담당 세션의 모든 비용을 셌는데, 실측에서
    # #310 이 곧바로 30.49 / 20.00 USD 로 나왔다. 그 세션들은 목표가 생기기
    # 전부터 대표님과 대화하던 창이고, **대표님이 직접 하신 대화까지 세고
    # 있었다.** 그건 오케스트레이션 비용이 아니다.
    #
    # 기준을 "첫 지시가 나간 시점" 으로 옮긴다. 그 전은 대표님 작업이고
    # 그 뒤가 기계가 돌린 것이다. 아직 지시가 안 나갔으면 0 이다.
    started = await pool.fetchval(
        "SELECT MIN(dispatched_at) FROM milestones WHERE goal_id = $1::uuid",
        goal_id,
    )
    if started is None:
        spent = 0
    else:
        spent = await pool.fetchval(
            """
            SELECT COALESCE(SUM(m.cost), 0)::numeric(12,4)
            FROM chat_messages m
            WHERE m.session_id IN (
                SELECT l.task_id::uuid FROM goal_task_links l
                WHERE l.goal_id = $1::uuid AND l.task_type = 'chat_session'
            )
              AND m.created_at >= $2
            """,
            goal_id, started,
        )
    spent = float(spent or 0)
    await pool.execute(
        "UPDATE goals SET cost_spent_usd = $2, updated_at = NOW() WHERE id = $1::uuid",
        goal_id, spent,
    )
    return spent


async def cost_gate(goal_id: str) -> Tuple[bool, str]:
    """(지시를 보내도 되는가, 사유)."""
    from app.core.db_pool import get_pool

    pool = get_pool()
    row = await pool.fetchrow(
        "SELECT title, COALESCE(cost_limit_usd, $2) AS lim, cost_spent_usd, "
        "       cost_warned_at, project FROM goals WHERE id = $1::uuid",
        goal_id, _DEFAULT_COST_LIMIT,
    )
    if not row:
        return True, ""

    spent = await refresh_goal_cost(goal_id)
    lim = float(row["lim"] or _DEFAULT_COST_LIMIT)
    if lim <= 0:
        return True, ""

    if spent >= lim:
        return False, f"비용 상한 도달 — {spent:.2f} / {lim:.2f} USD"

    if spent >= lim * 0.8 and row["cost_warned_at"] is None:
        try:
            from app.services.goal_report import _post_to_lead, _telegram

            msg = (
                f"💰 비용 80% — {row['title']}\n"
                f"{spent:.2f} / {lim:.2f} USD\n"
                "상한에 닿으면 새 지시가 멈춥니다. 진행 중인 것은 끝까지 둡니다."
            )
            async with pool.acquire() as conn:
                await _post_to_lead(conn, goal_id, msg)
            await _telegram(f"💰 [{row['project']}] {msg}")
            await pool.execute(
                "UPDATE goals SET cost_warned_at = NOW() WHERE id = $1::uuid", goal_id
            )
        except Exception as exc:
            logger.warning("cost_warn_failed", error=str(exc)[:160])

    return True, ""


async def _server_load(server_key: str) -> Optional[float]:
    """부하 / 코어. 못 읽으면 None — 못 읽는다고 진행을 막지 않는다."""
    now = time.time()
    hit = _load_cache.get(server_key)
    if hit and (now - hit[0]) < _LOAD_CACHE_TTL:
        return hit[1]

    ratio: Optional[float] = None
    try:
        from app.services.unified_healer import _execute_command

        res = await _execute_command(
            "cat /proc/loadavg; nproc", server_key,
        )
        if res.get("success"):
            parts = str(res.get("output") or "").split()
            if len(parts) >= 6:
                load1 = float(parts[0])
                cores = float(parts[-1]) or 1.0
                ratio = load1 / cores
    except Exception as exc:
        logger.debug("load_probe_failed", server=server_key, error=str(exc)[:120])

    _load_cache[server_key] = (now, ratio)
    return ratio


async def load_gate(project: Optional[str]) -> Tuple[bool, str]:
    """(지시를 보내도 되는가, 사유). 부하를 못 읽으면 통과시킨다."""
    if not project:
        return True, ""
    try:
        from app.core.project_config import PROJECT_MAP

        entry = PROJECT_MAP.get(project.upper())
        if not entry:
            return True, ""
        server_key = entry.get("server_name") or ""
    except Exception:
        return True, ""

    if not server_key:
        return True, ""

    ratio = await _server_load(server_key)
    if ratio is None:
        return True, ""
    if ratio > _MAX_LOAD_RATIO:
        return False, f"{server_key} 부하 {ratio:.1f}배 — 지시를 미룬다"
    return True, ""


async def owner_paused(goal_id: str, session_id: str) -> Tuple[bool, str]:
    """대표님이 이 담당을 멈춰 두셨는가."""
    from app.core.db_pool import get_pool

    row = await get_pool().fetchrow(
        "SELECT reason FROM owner_pause WHERE goal_id = $1::uuid AND session_id = $2::uuid",
        goal_id, session_id,
    )
    if not row:
        return False, ""
    return True, row["reason"] or "대표님이 멈춤"


async def check_deadlines(project: Optional[str] = None) -> int:
    """기한이 지난 것을 알린다. **멈추지는 않는다.**

    자동 중단은 대표님이 모르시는 사이에 목표를 죽인다. 알리고 대표님이
    정하신다.
    """
    from app.core.db_pool import get_pool

    pool = get_pool()
    notified = 0
    async with pool.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS goal_deadline_alerts (
                subject_id uuid PRIMARY KEY,
                alerted_at timestamptz NOT NULL DEFAULT NOW()
            )
            """
        )
        rows = await conn.fetch(
            """
            SELECT m.id::text AS sid, m.title, g.id::text AS goal_id,
                   g.title AS goal_title, g.project, m.due_date AS due
            FROM milestones m JOIN goals g ON g.id = m.goal_id
            WHERE m.due_date IS NOT NULL AND m.due_date < NOW()
              AND m.status IN ('pending', 'in_progress', 'review')
              AND ($1::text IS NULL OR g.project = $1)
              AND NOT EXISTS (SELECT 1 FROM goal_deadline_alerts a WHERE a.subject_id = m.id)
            LIMIT 5
            """,
            project,
        )
        for r in rows:
            try:
                from app.services.goal_report import _post_to_lead, _telegram

                msg = (
                    f"⏰ 기한 초과 — {r['goal_title']}\n"
                    f"{r['title']} (기한 {r['due']:%m-%d})\n"
                    "멈추지 않았습니다. 계속할지 대표님이 정하십시오."
                )
                await _post_to_lead(conn, r["goal_id"], msg)
                await _telegram(f"⏰ [{r['project']}] {msg}")
            except Exception as exc:
                logger.warning("deadline_alert_failed", error=str(exc)[:160])
            await conn.execute(
                "INSERT INTO goal_deadline_alerts (subject_id) VALUES ($1::uuid) "
                "ON CONFLICT DO NOTHING",
                r["sid"],
            )
            notified += 1
    return notified
