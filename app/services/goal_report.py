"""목표 진행 보고 — 대표님께 언제 어떻게 알릴 것인가.

## 지금까지 없던 것

골 오케스트레이션이 담당에게 일을 시켜도, **그 결과가 대표님께 가는 길이
없었다.** 대표님이 `/goals` 화면을 직접 열거나 세션을 하나씩 확인해야 했다.

## 두 갈래로 보낸다

**주도 채팅창** — 대표님 지시: "해당 목표는 주도책임 채팅창과 수시로
보고받고 나의 의견과 방향 설정 승인결정등을 할 수 있게". 목표에 묶인
주도 세션에 남긴다. 대표님이 그 자리에서 바로 의견을 주실 수 있다.

**텔레그램** — 대표님이 지금 보셔야 하는 것만. 알림이 잦으면 안 보게 된다.

## 무엇을 보낼 것인가 — 시각이 아니라 **사건**으로

정해진 시각에 요약을 보내면 아무 일도 없는 날에도 알림이 온다. 상태가
실제로 바뀔 때만 보낸다.

    마일스톤 완료        주도 채팅창
    마일스톤 막힘        주도 채팅창 + 텔레그램   ← 대표님 판단이 필요하다
    목표 달성/실패       주도 채팅창 + 텔레그램

"막힘" 은 `goal_dispatch` 가 재시도 한도까지 보냈는데도 답이 확인되지 않은
경우다. **조용히 포기하지 않는다** 는 규칙의 마지막 고리다 — 기계가 못
하면 사람에게 넘긴다.

## 같은 것을 두 번 보내지 않는다

보낸 사건은 `goal_report_log` 에 남긴다. 스케줄러가 2~3분마다 도는데
기록이 없으면 같은 완료 소식을 하루 종일 보낸다.
"""
from __future__ import annotations

import os
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

_ENABLED = os.getenv("GOAL_REPORT_ENABLED", "true").lower() in ("1", "true", "yes")
_MAX_PER_CYCLE = int(os.getenv("GOAL_REPORT_MAX_PER_CYCLE", "3"))


async def _telegram(text: str) -> bool:
    """대표님께 알린다. **이름은 남기되 오비스 알림으로 보낸다.**

    2026-09-14 대표님 지시: "텔레그램 알림 사용안해 그냥 오비스알림으로
    보내줘". 부르는 자리가 여럿이라 이름을 바꾸지 않고 안을 바꿨다 —
    호출부를 일괄 수정하면 한 곳을 빠뜨린다.
    """
    from app.services.ohvis_alert import WARNING, notify

    head, _, body = text.partition("\n")
    return await notify(
        head.strip()[:200] or "목표 알림",
        body.strip(),
        severity=WARNING,
        category="goal",
        dedupe_minutes=30,
    )


async def _post_to_lead(conn: Any, goal_id: str, text: str) -> bool:
    """주도 세션에 남긴다. 답을 요구하지 않는다 — 보고지 질문이 아니다."""
    sid = await conn.fetchval(
        """
        SELECT s.id::text
        FROM goal_task_links l
        JOIN chat_sessions s ON s.id = l.task_id::uuid
        WHERE l.goal_id = $1::uuid AND l.task_type = 'chat_session'
          AND s.role_key LIKE '%Lead'
        ORDER BY l.created_at LIMIT 1
        """,
        goal_id,
    )
    if not sid:
        return False
    await conn.execute(
        "INSERT INTO chat_messages (session_id, role, content, intent) "
        "VALUES ($1::uuid, 'assistant', $2, 'goal_report')",
        sid, text,
    )
    return True


async def report_goal_events(project: str | None = None) -> dict[str, int]:
    """아직 알리지 않은 사건을 찾아 보낸다."""
    if not _ENABLED:
        return {"reported": 0}

    from app.core.db_pool import get_pool

    pool = get_pool()
    reported = 0

    async with pool.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS goal_report_log (
                id          bigserial PRIMARY KEY,
                goal_id     uuid NOT NULL,
                subject_id  uuid,
                event       text NOT NULL,
                created_at  timestamptz NOT NULL DEFAULT NOW(),
                UNIQUE (goal_id, subject_id, event)
            )
            """
        )

        rows = await conn.fetch(
            """
            SELECT m.id::text AS subject_id, m.title, m.status,
                   m.dispatch_count, m.dispatch_note,
                   g.id::text AS goal_id, g.title AS goal_title, g.project,
                   CASE WHEN m.status = 'completed' THEN 'milestone_completed'
                        ELSE 'milestone_stuck' END AS event
            FROM milestones m
            JOIN goals g ON g.id = m.goal_id
            WHERE ($1::text IS NULL OR g.project = $1)
              AND (m.status = 'completed'
                   OR (m.status = 'in_progress' AND m.dispatch_note IS NOT NULL))
              AND NOT EXISTS (
                  SELECT 1 FROM goal_report_log r
                  WHERE r.goal_id = g.id AND r.subject_id = m.id
                    AND r.event = CASE WHEN m.status = 'completed'
                                       THEN 'milestone_completed' ELSE 'milestone_stuck' END
              )
            ORDER BY m.updated_at DESC
            LIMIT 20
            """,
            project,
        )

        for row in rows:
            if reported >= _MAX_PER_CYCLE:
                break
            if row["event"] == "milestone_completed":
                text = (
                    f"✅ 마일스톤 완료 — {row['goal_title']}\n\n"
                    f"**{row['title']}**\n\n"
                    "다음 마일스톤으로 넘어갑니다."
                )
                await _post_to_lead(conn, row["goal_id"], text)
            else:
                text = (
                    f"🚧 마일스톤이 막혔습니다 — {row['goal_title']}\n\n"
                    f"**{row['title']}**\n"
                    f"{row['dispatch_note']}\n\n"
                    "담당이 응답하지 않아 자동 진행이 멈췄습니다. "
                    "대표님 확인이 필요합니다."
                )
                await _post_to_lead(conn, row["goal_id"], text)
                await _telegram(
                    f"🚧 [{row['project']}] 마일스톤 막힘\n"
                    f"{row['goal_title']}\n{row['title']}\n{row['dispatch_note']}"
                )

            await conn.execute(
                "INSERT INTO goal_report_log (goal_id, subject_id, event) "
                "VALUES ($1::uuid, $2::uuid, $3) ON CONFLICT DO NOTHING",
                row["goal_id"], row["subject_id"], row["event"],
            )
            reported += 1
            logger.info(
                "goal_report_sent",
                event=row["event"], goal=row["goal_title"][:30], project=row["project"],
            )

    return {"reported": reported}
