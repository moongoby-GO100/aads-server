"""마일스톤 완료 판정 — 담당이 근거와 함께 신고하고 주도가 확인한다.

## 왜 이게 없으면 아무것도 안 도는가

`check_milestone_completion` 은 `goal_task_links` 의 상태로 판정한다.
그런데 담당 세션으로 굴러가는 마일스톤은 **묶인 작업이 0건**이다
(#310 의 링크는 `milestone_id NULL`, `task_type chat_session`).
`no_linked_tasks` 가 돌아오고 마일스톤은 영원히 `in_progress` 에 남는다.

담당은 답을 했으니 재알림도 안 간다. **조용한 영구 정지다.**

## 흐름

    담당  report_done(milestone, evidence)  → status 'review'
    주도  confirm(milestone, ok, reason)    → 'completed' | 'in_progress' | 'failed'

## 세 가지 규칙

**근거 없는 완료는 받지 않는다.** 나중에 "그때 진짜 됐던 건가" 에 답이
없다. 그리고 근거를 적게 하면 담당이 스스로 한 번 더 본다. 완료 기준에
숫자가 있으면 `numbers` 를 필수로 받는다.

**자기 확인은 확인이 아니다.** 주도가 맡은 마일스톤은 주도가 확인할 수
없다 — 대표님께 올라간다.

**주도가 확인을 안 하면 또 멈춘다.** 지시와 같은 재알림을 건다
(30분 / 3회 / 그 뒤 대표님).
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, Optional

import structlog

logger = structlog.get_logger(__name__)

_REVIEW_RETRY_MIN = int(os.getenv("MILESTONE_REVIEW_RETRY_MIN", "30"))
_REVIEW_MAX_ASK = int(os.getenv("MILESTONE_REVIEW_MAX_ASK", "3"))

# 완료 기준에 숫자가 들어 있으면 신고에도 숫자를 요구한다.
_HAS_NUMBER = re.compile(r"\d")


async def report_done(
    milestone_id: str, evidence: Dict[str, Any], reporter_session_id: str = ""
) -> Dict[str, Any]:
    """담당이 완료를 신고한다. **완료가 아니라 확인 대기(`review`)가 된다.**"""
    from app.core.db_pool import get_pool

    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT m.status, m.title, m.completion_criteria, m.goal_id::text AS goal_id "
            "FROM milestones m WHERE m.id = $1::uuid",
            milestone_id,
        )
        if not row:
            return {"error": "milestone_not_found"}
        # 착수 표시가 없어도 신고는 받는다. 2026-09-17 실측으로 GO100 활성
        # 마일스톤 55건 중 42건이 `pending` 이었고, 담당 세션은 일을 끝내도
        # `cannot_report_from_pending` 으로 신고 자체가 막혔다 — 담당들이
        # "마일스톤 검증이 불가하다" 고 올린 것이 이 게이트다.
        # `pending` → `review` 로 바로 올리고 착수 시각만 채운다. 막는 것은
        # **이미 끝난 것** 뿐이다. 끝난 것을 되돌리는 것은 판정의 일이다.
        if row["status"] in ("completed", "archived", "failed"):
            return {
                "error": f"cannot_report_from_{row['status']}",
                "message": "이미 판정이 끝난 마일스톤이다. 되돌릴 일이면 "
                           "주도나 대표님이 상태를 되돌려야 한다.",
            }

        summary = str((evidence or {}).get("summary") or "").strip()
        if len(summary) < 20:
            return {
                "error": "evidence_required",
                "message": "무엇을 했고 결과가 무엇인지 20자 이상 적어라. "
                           "근거 없는 완료는 나중에 설명할 수 없다.",
            }

        criteria = row["completion_criteria"] or ""
        numbers = (evidence or {}).get("numbers")
        if _HAS_NUMBER.search(criteria) and not numbers:
            return {
                "error": "numbers_required",
                "message": f"완료 기준에 숫자가 있다 — \"{criteria[:80]}\". "
                           "`numbers` 에 before/after 를 넣어라.",
            }

        await conn.execute(
            "UPDATE milestones SET status = 'review', reported_at = NOW(), "
            "started_at = COALESCE(started_at, NOW()), "
            "reported_by = NULLIF($2,'')::uuid, evidence = $3::jsonb, "
            "review_asked_at = NULL, review_ask_count = 0, updated_at = NOW() "
            "WHERE id = $1::uuid",
            milestone_id, reporter_session_id, json.dumps(evidence, ensure_ascii=False),
        )
    logger.info("milestone_reported", milestone=milestone_id[:8], title=row["title"][:30])
    return {"status": "review", "milestone": row["title"], "goal_id": row["goal_id"]}


async def confirm(
    milestone_id: str, ok: bool, reason: str = "", negative: bool = False,
    confirmer: str = "lead",
) -> Dict[str, Any]:
    """주도(또는 대표님)가 판정한다.

    `negative=True` 는 **제대로 했는데 결과가 음성**인 경우다. 반려와 다르다 —
    반려는 다시 하라는 것이고, 음성은 다음을 다시 짜라는 것이다.
    """
    from app.core.db_pool import get_pool

    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status, title, goal_id::text AS goal_id FROM milestones WHERE id = $1::uuid",
            milestone_id,
        )
        if not row:
            return {"error": "milestone_not_found"}
        if row["status"] != "review":
            # 사유만 던지면 주도 세션은 "검증이 불가하다" 로 끝낸다.
            # 무엇을 해야 확인 대기가 되는지 같이 준다.
            return {
                "error": "not_in_review",
                "status": row["status"],
                "message": "아직 담당이 완료를 신고하지 않았다(현재 "
                           f"{row['status']}). 판정은 신고 뒤에만 한다 — "
                           "담당이 `report_milestone_done(milestone_id, "
                           "summary, numbers, refs)` 로 근거를 올리게 하라. "
                           "담당이 응답하지 않으면 `ask_session` 으로 물어라.",
            }

        if ok:
            new = "failed" if negative else "completed"
            await conn.execute(
                "UPDATE milestones SET status = $2, "
                "completed_at = CASE WHEN $2 = 'completed' THEN NOW() ELSE NULL END, "
                "review_note = NULLIF($3,''), updated_at = NOW() WHERE id = $1::uuid",
                milestone_id, new, reason,
            )
        else:
            new = "in_progress"
            # 반려하면 발송 기록을 지운다 — 그래야 다시 지시가 나간다.
            # 안 지우면 "답은 왔다" 로 판정돼 조용히 멈춘다.
            await conn.execute(
                "UPDATE milestones SET status = 'in_progress', reported_at = NULL, "
                "evidence = NULL, review_note = NULLIF($2,''), "
                "dispatched_at = NULL, dispatch_count = 0, "
                "review_asked_at = NULL, review_ask_count = 0, updated_at = NOW() "
                "WHERE id = $1::uuid",
                milestone_id, reason,
            )
    logger.info(
        "milestone_confirmed", milestone=milestone_id[:8],
        result=new, by=confirmer,
    )
    return {"status": new, "milestone": row["title"], "goal_id": row["goal_id"]}


async def _lead_session(conn: Any, goal_id: str) -> Optional[str]:
    """주도 세션. **`goals.owner_session_id` 가 정본이다.**

    예전에는 `goal_task_links` 에서 `role_key LIKE '%Lead'` 인 세션만 찾았다.
    주도가 `Lead` 로 끝나지 않는 목표(#119 는 CTO 가 주도다)에서는 항상
    빈손으로 돌아왔고, 그러면 `to_ceo` 가 참이 되어 **모든 확인 요청이
    주도를 건너뛰고 대표님께 올라갔다** [실측 2026-09-17: 활성 목표 11건 중
    주도가 `%Lead` 인 것은 1건].
    """
    lead = await conn.fetchval(
        "SELECT g.owner_session_id::text FROM goals g "
        "WHERE g.id = $1::uuid AND g.owner_session_id IS NOT NULL",
        goal_id,
    )
    if lead:
        return lead
    return await conn.fetchval(
        "SELECT s.id::text FROM goal_task_links l "
        "JOIN chat_sessions s ON s.id = l.task_id::uuid "
        "WHERE l.goal_id = $1::uuid AND l.task_type = 'chat_session' "
        "  AND s.role_key LIKE '%Lead' "
        "ORDER BY l.created_at LIMIT 1",
        goal_id,
    )


def _ask_text(row: Any, to_ceo: bool) -> str:
    ev = row["evidence"] or {}
    if isinstance(ev, str):
        try:
            ev = json.loads(ev)
        except Exception:
            ev = {}
    who = "대표님" if to_ceo else "주도"
    head = "[완료 확인 요청 — 대표님]" if to_ceo else "[완료 확인 요청 — 주도]"
    parts = [
        f"{head} {row['goal_title']}",
        "",
        f"## {row['title']}",
        f"담당: {row['owner'] or '미지정'}",
        "",
        "## 담당이 낸 근거",
        str(ev.get("summary") or "(없음)"),
    ]
    if ev.get("numbers"):
        parts.append(f"\n수치: {json.dumps(ev['numbers'], ensure_ascii=False)}")
    if ev.get("refs"):
        parts.append(f"근거: {', '.join(str(r) for r in ev['refs'])}")
    if row["completion_criteria"]:
        parts.append(f"\n## 완료 기준\n{row['completion_criteria']}")
    if to_ceo:
        parts.append(
            "\n주도 자신이 맡은 마일스톤이라 자기 확인이 불가능해 "
            "대표님께 올렸습니다."
        )
    parts.append(
        f"\n## {who}가 판정할 것\n"
        "`confirm_milestone(milestone_id, ok=true)` — 완료\n"
        "`confirm_milestone(milestone_id, ok=true, negative=true, reason=...)` "
        "— 제대로 했으나 **결과가 음성**\n"
        "`confirm_milestone(milestone_id, ok=false, reason=...)` — 반려(다시 하라)\n\n"
        "근거가 기준을 만족하는지 보고 판정하라. 만족하지 않으면 무엇이 "
        "부족한지 적어서 반려하라."
    )
    return "\n".join(parts)


async def ask_pending_reviews(project: Optional[str] = None) -> Dict[str, int]:
    """`review` 인데 아직 확인 요청을 안 보냈거나 답이 없는 건을 처리한다."""
    from app.core.db_pool import get_pool

    pool = get_pool()
    asked = escalated = 0

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT m.id::text AS milestone_id, m.title, m.completion_criteria,
                   m.evidence, m.review_ask_count, m.review_asked_at,
                   COALESCE(m.owner_role_key, '') AS owner,
                   m.owner_session_id::text AS owner_session,
                   g.id::text AS goal_id, g.title AS goal_title, g.project
            FROM milestones m
            JOIN goals g ON g.id = m.goal_id
            WHERE m.status = 'review'
              AND ($1::text IS NULL OR g.project = $1)
              AND (m.review_asked_at IS NULL
                   OR m.review_asked_at < NOW() - ($2 || ' minutes')::interval)
            ORDER BY m.review_asked_at NULLS FIRST
            LIMIT 5
            """,
            project, str(_REVIEW_RETRY_MIN),
        )

        for row in rows:
            lead = await _lead_session(conn, row["goal_id"])
            lead_role = ""
            if lead:
                lead_role = await conn.fetchval(
                    "SELECT COALESCE(role_key, '') FROM chat_sessions "
                    "WHERE id = $1::uuid",
                    lead,
                ) or ""
            # 주도 자신의 마일스톤이면 자기 확인이 된다 — 대표님께 올린다.
            # 역할명이 'Lead' 로 끝나는지 보는 것으로는 못 잡았다. #119 는
            # 주도가 CTO 이고 CTO 가 맡은 마일스톤이 3건이다 — 예전 판정은
            # 그걸 자기확인이 아니라고 보고 주도에게 자기 것을 보냈다.
            self_confirm = bool(
                (lead and row["owner_session"] and lead == row["owner_session"])
                or (lead_role and row["owner"] and lead_role == row["owner"])
            )
            to_ceo = (
                int(row["review_ask_count"] or 0) >= _REVIEW_MAX_ASK
                or self_confirm
                or not lead
            )

            if to_ceo:
                try:
                    from app.services.goal_report import _telegram

                    await _telegram(
                        f"✅ [{row['project']}] 완료 확인 요청\n"
                        f"{row['goal_title']}\n{row['title']}\n"
                        f"담당 {row['owner'] or '미지정'} — 대표님 확인이 필요합니다"
                    )
                except Exception as exc:
                    logger.warning("review_escalate_failed", error=str(exc)[:160])
                escalated += 1

            if lead and not self_confirm:
                try:
                    from app.services import chat_service as cs

                    async for _c in cs.send_message_stream(
                        session_id=lead, content=_ask_text(row, to_ceo=False),
                        intent_override="system_trigger", response_mode="quality",
                    ):
                        pass
                    asked += 1
                except Exception as exc:
                    logger.warning(
                        "review_ask_failed", milestone=row["milestone_id"][:8],
                        error=str(exc)[:160],
                    )

            await conn.execute(
                "UPDATE milestones SET review_asked_at = NOW(), "
                "review_ask_count = review_ask_count + 1, updated_at = NOW() "
                "WHERE id = $1::uuid",
                row["milestone_id"],
            )

    if asked or escalated:
        logger.info("milestone_review_cycle", asked=asked, escalated=escalated)
    return {"asked": asked, "escalated": escalated}
