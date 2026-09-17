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

import asyncio
import os
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

_ENABLED = os.getenv("GOAL_DISPATCH_ENABLED", "true").lower() in ("1", "true", "yes")
_MAX_PER_CYCLE = int(os.getenv("GOAL_DISPATCH_MAX_PER_CYCLE", "2"))
_MAX_DISPATCH = int(os.getenv("GOAL_DISPATCH_MAX_RETRY", "3"))
_RETRY_AFTER_MIN = int(os.getenv("GOAL_DISPATCH_RETRY_AFTER_MIN", "30"))
# 담당 세션 생성 승인 요청은 한 사이클에 이만큼까지. 카드가 한꺼번에 열 장
# 올라오면 대표님은 읽지 않고 누르신다.
_OWNER_REQUEST_PER_CYCLE = int(os.getenv("GOAL_OWNER_REQUEST_PER_CYCLE", "3"))

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


async def _note(conn, milestone_id: str, note: str) -> None:
    """사유를 남긴다 — **값이 달라질 때만.**

    `dispatch_count` 는 건드리지 않는다. 담당이 없어서 못 보낸 것은
    "미룬 것" 이지 "보냈는데 답이 없는 것" 이 아니다. 여기서 횟수를 올리면
    말을 걸어 본 적도 없는 마일스톤이 재시도 한도를 다 쓰고 포기 처리된다.
    """
    await conn.execute(
        "UPDATE milestones SET dispatch_note = $2, updated_at = NOW() "
        " WHERE id = $1::uuid AND dispatch_note IS DISTINCT FROM $2",
        milestone_id, note,
    )


async def _requester_session(conn, row: Any) -> str:
    """승인 요청을 올릴 세션. **주도가 정본이다.**

    주도가 없으면 같은 워크스페이스의 최근 활성 세션으로 폴백한다. 카드는
    누군가의 이름으로 올라가야 하고, 그 대화에 결정이 돌아간다 —
    올릴 곳이 없으면 대표님은 카드를 보시고도 맥락을 알 수 없다.
    """
    lead = str(row["goal_lead_session_id"] or "").strip()
    if lead:
        return lead
    fallback = await conn.fetchval(
        """
        SELECT s.id::text FROM chat_sessions s
         WHERE s.role_key IS NOT NULL
           AND s.workspace_id = (
                 SELECT s2.workspace_id FROM goal_task_links l
                   JOIN chat_sessions s2 ON s2.id = l.task_id::uuid
                  WHERE l.goal_id = $1::uuid AND l.task_type = 'chat_session'
                    AND COALESCE(l.link_state, 'active') = 'active'
                  ORDER BY l.created_at DESC LIMIT 1)
         ORDER BY s.updated_at DESC LIMIT 1
        """,
        row["goal_id"],
    )
    return str(fallback or "")


async def _handle_missing_owner(conn, row: Any, *, remaining: int) -> int:
    """담당 세션이 없는 마일스톤 하나를 처리한다. 올린 요청 수를 돌려준다.

    **여기서 세션을 만들지 않는다.** 만드는 것은 CEO 가 승인 카드를 누른
    뒤 `provision_owner_session` 하나뿐이다 — 채팅창이 대표님 모르게
    늘어나면 안 된다는 원칙(`add_goal_owner`)은 그대로다.
    """
    milestone_id = row["milestone_id"]
    role = str(row["owner_role_key"] or "").strip()

    # 역할조차 안 적힌 마일스톤은 요청 대상이 아니다. 누구를 만들지 모른다.
    if not role:
        await _note(conn, milestone_id, "담당 역할이 지정되지 않음")
        return 0

    requester = await _requester_session(conn, row)
    if not requester:
        await _note(conn, milestone_id, "주도 세션이 없어 승인 요청을 올릴 곳이 없음")
        return 0

    if remaining <= 0:
        await _note(
            conn, milestone_id,
            f"담당 세션 없음(role={role}) — 이번 주기 요청 상한 초과, 다음 주기에 올림",
        )
        return 0

    try:
        from app.services.owner_session_provision import request_owner_session

        result = await request_owner_session(
            goal_id=row["goal_id"], role_key=role,
            project=str(row["project"] or ""), requester_session_id=requester,
        )
    except Exception as exc:  # noqa: BLE001
        # 요청 하나가 실패해도 사이클 전체를 멈추지 않는다. 나머지
        # 마일스톤 발송까지 같이 죽으면 한 건의 오류가 전부를 막는다.
        logger.warning(
            "goal_owner_request_failed",
            milestone=str(milestone_id)[:8], role=role, error=str(exc)[:160],
        )
        await _note(conn, milestone_id, f"담당 세션 없음(role={role}) — 승인 요청 실패")
        return 0

    if result.get("error"):
        logger.warning(
            "goal_owner_request_rejected",
            milestone=str(milestone_id)[:8], role=role, why=str(result["error"])[:160],
        )
        await _note(conn, milestone_id, f"담당 세션 없음(role={role}) — {result['error']}")
        return 0

    await _note(conn, milestone_id, f"담당 세션 없음(role={role}) — 생성 승인 요청함")
    logger.info(
        "goal_owner_request_open",
        milestone=str(milestone_id)[:8], role=role,
        request=str(result.get("request_id") or "")[:8],
        reused=bool(result.get("reused")),
    )
    return 1


async def repair_owner_links(conn) -> int:
    """주도·담당은 정해졌는데 **화면 연결고리가 없는** 목표를 이어 붙인다.

    2026-09-17 실측. 라일론 목표(NTV2, P0)는 `goals.owner_session_id` 와
    마일스톤 6건의 `owner_session_id` 가 전부 채워져 있었는데도 다섯 개 창
    **어디에도 보이지 않았다.** 화면 조회(`GET /goals/for-session/{sid}`)가
    `goal_task_links` 를 FROM 기준 표로 쓰기 때문이다. 목표를 API 가 아니라
    DB 직접 시드로 만들면 그 표가 비고, 목표는 등록됐는데 주도도 담당도
    자기 목표를 못 보는 상태가 된다 — 그날 대표님이 "안 뜨지?" 로 먼저
    알아채셨다.

    소유자가 정해진 것과 화면에 뜨는 것이 서로 다른 표에 적히는 구조라면,
    한쪽만 채워진 상태를 매 주기 메워 주는 편이 맞다. 시드 스크립트마다
    링크 INSERT 를 기억해 넣으라고 규칙으로 적어 두는 것은 또 잊힌다.

    **뗀 것은 다시 붙이지 않는다.** 이미 줄이 있으면(`detached` 포함) 손대지
    않는다 — 대표님이 손으로 떼신 담당을 스케줄러가 되살리면 그건 복원이
    아니라 되돌리기다.
    """
    result = await conn.execute(
        """
        INSERT INTO goal_task_links (goal_id, task_type, task_id, status,
                                     bind_source, bound_by, link_state)
        SELECT o.goal_id, 'chat_session', o.sid, 'active',
               'auto_repair', 'goal_dispatch', 'active'
          FROM (
                SELECT g.id AS goal_id, g.owner_session_id::text AS sid
                  FROM goals g
                 WHERE g.owner_session_id IS NOT NULL
                   AND g.status IN ('draft', 'active', 'blocked')
                UNION
                SELECT m.goal_id, m.owner_session_id::text
                  FROM milestones m
                  JOIN goals g2 ON g2.id = m.goal_id
                 WHERE m.owner_session_id IS NOT NULL
                   AND g2.status IN ('draft', 'active', 'blocked')
               ) o
          JOIN chat_sessions s ON s.id = o.sid::uuid
         WHERE NOT EXISTS (
                SELECT 1 FROM goal_task_links l
                 WHERE l.goal_id = o.goal_id
                   AND l.task_type = 'chat_session'
                   AND l.task_id = o.sid
               )
        """
    )
    try:
        inserted = int(str(result).split()[-1])
    except (ValueError, IndexError):
        inserted = 0
    if inserted:
        logger.info("goal_owner_links_repaired", inserted=inserted)
    return inserted


async def dispatch_pending_milestones(project: str | None = None) -> dict[str, int]:
    """착수했는데 담당이 모르는 마일스톤에 지시를 넣는다."""
    if not _ENABLED:
        return {"sent": 0, "skipped": 0, "gave_up": 0, "owner_requests": 0}

    from app.core.db_pool import get_pool

    pool = get_pool()
    sent = skipped = gave_up = owner_requests = 0
    # 한 세션에 한 주기 한 번만. 세션이 목표 두 개에 참여하면 양쪽에서
    # 동시에 지시가 나갈 수 있는데, 담당은 그걸 두 개의 새 대화로 받는다.
    # 어느 쪽부터 할지 모른 채 섞어서 답한다.
    touched: set[str] = set()

    async with pool.acquire() as conn:
        # 지시를 보내기 전에 연결고리부터 메운다. 링크가 없으면 담당은
        # 지시를 받아도 자기 화면에서 그 목표를 찾을 수 없다.
        links_repaired = await repair_owner_links(conn)

        rows = await conn.fetch(
            """
            SELECT m.id::text AS milestone_id, m.title AS milestone_title,
                   m.description, m.completion_criteria,
                   m.dispatch_count, m.dispatched_at,
                   g.title AS goal_title, g.project, g.id::text AS goal_id,
                   COALESCE(m.owner_role_key, '') AS owner_role_key,
                   COALESCE(g.owner_session_id::text, '') AS goal_lead_session_id,
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
            if not row["session_id"]:
                # 담당이 안 정해진 마일스톤. 말을 걸 곳이 **아직** 없다.
                #
                # 2026-09-17 이전에는 여기서 그냥 넘어갔다. 기록이 어디에도
                # 남지 않아서, 담당 없는 마일스톤은 착수 상태로 열린 채
                # 영원히 방치됐다 — 아무도 그런 것이 있는 줄 몰랐다.
                # 이제 사유를 남기고, 주도 세션이 생성 승인을 요청한다.
                skipped += 1
                owner_requests += await _handle_missing_owner(
                    conn, row, remaining=_OWNER_REQUEST_PER_CYCLE - owner_requests,
                )
                continue

            # 발송 상한은 담당 없는 건을 처리한 **뒤에** 본다. 먼저 보면
            # 발송이 상한에 차는 사이클마다 승인 요청이 통째로 밀린다.
            #
            # `break` 가 아니라 `continue` 인 이유도 같다. 상한에 찼다고
            # 루프를 끊으면 뒤에 남은 담당 없는 마일스톤이 이번 사이클에
            # 아예 보이지 않는다 — 발송이 바쁜 목표일수록 담당 공백이
            # 영원히 안 드러난다. 한 사이클 20건이라 도는 비용은 없다.
            if sent >= _MAX_PER_CYCLE:
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

            # **보내기 전에 기록한다.** 순서가 뒤집히면 기록이 사라진다.
            #
            # 2026-09-17 실측. 여기는 스트림을 끝까지 소비한 **뒤에**
            # 기록하고 있었다. 그런데 이 코루틴은 상위 사이클에서
            # `asyncio.wait_for(..., 120초)` 로 감싸여 돈다(main.py). 상한이
            # 먼저 터지면 코루틴이 취소되고 뒤에 있던 UPDATE 는 실행되지
            # 않는다. 지시는 담당 세션에 이미 들어갔는데 `dispatched_at` 은
            # NULL 로 남는다.
            #
            # 라일론 목표 M4 에서 그대로 일어났다. 10:17:12 에 발송된 지시가
            # 기록되지 않아 다음 주기가 10:20:09 에 **같은 지시를 다시**
            # 보냈고, 첫 턴은 `interrupted_partial` 로 죽었다. 담당은 같은
            # 일을 두 번 받고 LLM 비용은 두 번 든다.
            #
            # 그래서 순서를 바꾼다. 못 보냈는데 보냈다고 적히는 쪽이
            # 나은가 — 그렇다. 그건 다음 주기가 "재알림" 으로 복구한다.
            # 반대는 복구되지 않고 중복 발송을 계속 만든다.
            await conn.execute(
                "UPDATE milestones SET dispatched_at = NOW(), "
                "dispatched_session_id = $2, dispatch_count = dispatch_count + 1, "
                "dispatch_note = NULL, updated_at = NOW() WHERE id = $1::uuid",
                row["milestone_id"], row["session_id"],
            )

            try:
                from app.services import chat_service as cs

                async for _chunk in cs.send_message_stream(
                    session_id=str(row["session_id"]),
                    content=_build_message(row),
                    intent_override="system_trigger",
                    response_mode="quality",
                ):
                    pass
            except asyncio.CancelledError:
                # 상위 상한(`_GOAL_STAGE_TIMEOUT`)이 이 코루틴을 끊는 경로다.
                # `CancelledError` 는 `Exception` 의 하위가 아니므로 아래 포괄
                # except 에 걸리지 않는다 — 여기서 잡지 않으면 이 경로는 아무
                # 흔적도 남기지 않고 사라진다. 2026-09-17 M4 가 그랬다:
                # `goal_dispatch_timeout` 은 사이클 쪽에만 찍히고, 어느
                # 마일스톤이 끊겼는지는 어디에도 없었다.
                #
                # 발송 기록은 위에서 이미 남았으므로 중복 발송은 나가지 않고,
                # 담당이 답을 못 하면 `_RETRY_AFTER_MIN` 뒤 재알림이 이어받는다.
                # **DB 를 다시 건드리지 않는다** — 취소 중에는 그 await 도 곧
                # 취소되어 사유가 남지 않는다. 이 경로의 기록은 이 로그다.
                logger.warning(
                    "goal_dispatch_cancelled",
                    milestone=row["milestone_id"][:8],
                    session=str(row["session_id"])[:8],
                    attempt=count + 1,
                    note="상위 상한으로 스트림이 끊겼다 — 답이 없으면 재알림된다",
                )
                raise
            except Exception as exc:
                # 횟수는 위에서 이미 올렸다. 여기서 또 올리면 한 번의 시도가
                # 재시도 한도를 두 칸 깎는다. 사유만 남긴다.
                logger.warning(
                    "goal_dispatch_send_failed",
                    milestone=row["milestone_id"][:8],
                    error=str(exc)[:160],
                )
                await _note(
                    conn, row["milestone_id"], f"발송 실패: {str(exc)[:200]}",
                )
                continue

            sent += 1
            touched.add(str(row["session_id"]))
            logger.info(
                "goal_dispatch_sent",
                milestone=row["milestone_id"][:8],
                session=str(row["session_id"])[:8],
                attempt=count + 1,
                project=row["project"],
            )

    if sent or gave_up or owner_requests or links_repaired:
        logger.info(
            "goal_dispatch_cycle", sent=sent, skipped=skipped, gave_up=gave_up,
            owner_requests=owner_requests, links_repaired=links_repaired,
        )
    return {
        "sent": sent, "skipped": skipped, "gave_up": gave_up,
        "owner_requests": owner_requests, "links_repaired": links_repaired,
    }
