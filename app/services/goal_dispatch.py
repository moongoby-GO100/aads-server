"""마일스톤 착수 지시 — 골 오케스트레이션이 **담당에게 말을 걸게** 한다.

## 왜 필요한가

골 스케줄러는 상태만 옮기고 있었다. 마일스톤이 `in_progress` 로 바뀌어도
그 일을 할 세션은 아무것도 모른다. 대표님이나 주도가 손으로 말을 걸어야
움직였다 — 그건 오케스트레이션이 아니라 수작업이다.

## 끊기거나 실패하면 어떻게 되는가

이게 이 모듈의 핵심이다. 보낸 것과 **답이 온 것**은 다르다.

2026-09-14 실측. 운영인프라담당에게 지시를 넣은 직후 배포로 슬롯이 바뀌며
스트림이 끊겼고 세션에는 이것만 남았다:

    ⚠️ 응답 생성이 중단되어 여기까지 보존된 내용이 없습니다.

보낸 기록만 남겼다면 그 마일스톤은 **영원히 멈춘 채로 조용히 방치된다.**
그래서 네 겹을 둔다.

1. **답이 왔는지 본다.** 보낸 시각 이후에 그 세션에 assistant 메시지가
   실제로 쌓였는지 확인한다. 진행중 표시(`⏳`)나 중단 표시(`⚠️`)는
   답으로 세지 않는다 — 그게 바로 끊긴 경우다.
2. **답이 없으면 다시 보낸다.** `_RETRY_AFTER_MIN` 이 지나야 재시도한다.
   너무 빨리 다시 보내면 아직 생각 중인 담당을 방해한다.
3. **한도가 있다.** `_MAX_DISPATCH` 회를 넘으면 더 보내지 않고
   `dispatch_note` 에 사유를 남긴다. 무한 재시도는 폭주다.
4. **조용히 포기하지 않는다.** 한도를 넘긴 건은 기록에 남아 사람이 볼 수
   있다. 이것이 없으면 "보냈는데 아무 일도 안 일어남" 이 침묵으로 끝난다.

한 사이클에 보내는 건수도 제한한다(`_MAX_PER_CYCLE`). 마일스톤이 한꺼번에
열려도 세션 열 개에 동시에 말을 걸지 않는다.
"""
from __future__ import annotations

import os
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

_ENABLED = os.getenv("GOAL_DISPATCH_ENABLED", "true").lower() in ("1", "true", "yes")
_MAX_PER_CYCLE = int(os.getenv("GOAL_DISPATCH_MAX_PER_CYCLE", "2"))
_MAX_DISPATCH = int(os.getenv("GOAL_DISPATCH_MAX_RETRY", "3"))
_RETRY_AFTER_MIN = int(os.getenv("GOAL_DISPATCH_RETRY_AFTER_MIN", "30"))

# 답으로 세지 않는 표시. 진행중·중단 안내는 담당이 쓴 것이 아니다.
_NOT_AN_ANSWER = ("⏳", "⚠️ _응답 생성이", "_AI가 응답을 생성 중")


def _build_message(row: Any) -> str:
    goal = row["goal_title"]
    title = row["milestone_title"]
    desc = (row["description"] or "").strip()
    criteria = (row["completion_criteria"] or "").strip()
    again = int(row["dispatch_count"] or 0) > 0

    head = "[목표 진행 — 재알림]" if again else "[목표 진행 — 착수]"
    body = [
        f"{head} {goal}",
        "",
        f"## 맡은 마일스톤\n**{title}**",
    ]
    if desc:
        body.append(f"\n{desc}")
    if criteria:
        body.append(f"\n## 완료 기준\n{criteria}")
    if again:
        body.append(
            "\n앞서 같은 내용을 보냈는데 답이 확인되지 않았다. 응답이 중간에 "
            "끊겼을 수 있다. 이미 진행한 것이 있으면 그것부터 정리해서 답해라."
        )
    body.append(
        "\n## 답할 때\n"
        "무엇을 **실측했는지**부터 써라. 추정으로 시작하지 마라.\n"
        "자기 범위를 벗어나면 `ask_session` 으로 해당 담당에게 넘겨라.\n"
        "막히면 막힌 이유를 적어라 — 조용히 멈추는 것이 제일 나쁘다."
    )
    return "\n".join(body)


async def dispatch_pending_milestones(project: str | None = None) -> dict[str, int]:
    """착수했는데 담당이 모르는 마일스톤에 지시를 넣는다."""
    if not _ENABLED:
        return {"sent": 0, "skipped": 0, "gave_up": 0}

    from app.core.db_pool import get_pool

    pool = get_pool()
    sent = skipped = gave_up = 0
    # 한 세션에 한 주기 한 번만. 세션이 목표 두 개에 참여하면 양쪽에서
    # 동시에 지시가 나갈 수 있는데, 담당은 그걸 두 개의 새 대화로 받는다.
    # 어느 쪽부터 할지 모른 채 섞어서 답한다.
    touched: set[str] = set()

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT m.id::text AS milestone_id, m.title AS milestone_title,
                   m.description, m.completion_criteria,
                   m.dispatch_count, m.dispatched_at,
                   g.title AS goal_title, g.project, g.id::text AS goal_id,
                   COALESCE(m.owner_session_id, s.id) AS session_id
            FROM milestones m
            JOIN goals g ON g.id = m.goal_id
            LEFT JOIN chat_sessions s
                   ON m.owner_session_id IS NULL
                  AND m.owner_role_key IS NOT NULL
                  AND s.role_key = m.owner_role_key
            WHERE m.status = 'in_progress'
              AND g.status = 'active'
              AND ($1::text IS NULL OR g.project = $1)
              AND (m.dispatched_at IS NULL
                   OR m.dispatched_at < NOW() - ($2 || ' minutes')::interval)
            ORDER BY m.dispatched_at NULLS FIRST, m.sequence_order
            LIMIT 20
            """,
            project, str(_RETRY_AFTER_MIN),
        )

        for row in rows:
            if sent >= _MAX_PER_CYCLE:
                break
            if not row["session_id"]:
                # 담당이 안 정해진 마일스톤. 말을 걸 곳이 없다.
                skipped += 1
                continue

            # 상한 셋을 본다. **미루는 것과 포기하는 것은 다르다** —
            # 아래 셋은 전부 미루기이므로 `dispatch_count` 를 올리지 않는다.
            # 올리면 부하나 비용 때문에 미룬 것이 재시도 한도를 헛되이 깎는다.
            from app.services.orchestration_limits import (
                cost_gate, load_gate, owner_paused,
            )

            if str(row["session_id"]) in touched:
                skipped += 1
                continue

            paused, why = await owner_paused(row["goal_id"], str(row["session_id"]))
            if paused:
                skipped += 1
                continue

            ok, why = await cost_gate(row["goal_id"])
            if not ok:
                logger.info(
                    "goal_dispatch_cost_gated",
                    milestone=row["milestone_id"][:8], why=why,
                )
                skipped += 1
                continue

            ok, why = await load_gate(row["project"])
            if not ok:
                logger.info(
                    "goal_dispatch_load_gated",
                    milestone=row["milestone_id"][:8], why=why,
                )
                skipped += 1
                continue

            count = int(row["dispatch_count"] or 0)
            if count >= _MAX_DISPATCH:
                if row["dispatched_at"] is not None:
                    await conn.execute(
                        "UPDATE milestones SET dispatch_note = $2, updated_at = NOW() "
                        "WHERE id = $1::uuid AND dispatch_note IS DISTINCT FROM $2",
                        row["milestone_id"],
                        f"{_MAX_DISPATCH}회 보냈으나 답이 확인되지 않음 — 사람이 확인해야 한다",
                    )
                gave_up += 1
                continue

            # 보낸 뒤에 **진짜 답**이 왔는지 본다.
            if row["dispatched_at"] is not None:
                answered = await conn.fetchval(
                    """
                    SELECT EXISTS (
                        SELECT 1 FROM chat_messages
                        WHERE session_id = $1 AND role = 'assistant'
                          AND created_at > $2
                          AND length(content) > 40
                          AND content NOT LIKE '⏳%'
                          AND content NOT LIKE '⚠️ _응답 생성이%'
                    )
                    """,
                    row["session_id"], row["dispatched_at"],
                )
                if answered:
                    skipped += 1
                    continue

            try:
                from app.services import chat_service as cs

                async for _chunk in cs.send_message_stream(
                    session_id=str(row["session_id"]),
                    content=_build_message(row),
                    intent_override="system_trigger",
                    response_mode="quality",
                ):
                    pass
            except Exception as exc:
                # 보내는 것 자체가 실패해도 횟수를 올린다. 올리지 않으면
                # 같은 오류로 매 사이클마다 재시도하며 폭주한다.
                logger.warning(
                    "goal_dispatch_send_failed",
                    milestone=row["milestone_id"][:8],
                    error=str(exc)[:160],
                )
                await conn.execute(
                    "UPDATE milestones SET dispatched_at = NOW(), "
                    "dispatch_count = dispatch_count + 1, dispatch_note = $2, "
                    "updated_at = NOW() WHERE id = $1::uuid",
                    row["milestone_id"], f"발송 실패: {str(exc)[:200]}",
                )
                continue

            await conn.execute(
                "UPDATE milestones SET dispatched_at = NOW(), "
                "dispatched_session_id = $2, dispatch_count = dispatch_count + 1, "
                "dispatch_note = NULL, updated_at = NOW() WHERE id = $1::uuid",
                row["milestone_id"], row["session_id"],
            )
            sent += 1
            touched.add(str(row["session_id"]))
            logger.info(
                "goal_dispatch_sent",
                milestone=row["milestone_id"][:8],
                session=str(row["session_id"])[:8],
                attempt=count + 1,
                project=row["project"],
            )

    if sent or gave_up:
        logger.info("goal_dispatch_cycle", sent=sent, skipped=skipped, gave_up=gave_up)
    return {"sent": sent, "skipped": skipped, "gave_up": gave_up}
