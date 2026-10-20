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

"막힘" 은 `goal_dispatch` 가 재시도 한도까지 보냈는데도 답이 확인되지 않아
`dispatch_blocked_at` 이 기록된
경우다. **조용히 포기하지 않는다** 는 규칙의 마지막 고리다 — 기계가 못
하면 사람에게 넘긴다.

## 같은 것을 두 번 보내지 않는다

보낸 사건은 `goal_report_log` 에 남긴다. 스케줄러가 2~3분마다 도는데
기록이 없으면 같은 완료 소식을 하루 종일 보낸다.
"""
from __future__ import annotations

import os
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import structlog

logger = structlog.get_logger(__name__)

_ENABLED = os.getenv("GOAL_REPORT_ENABLED", "true").lower() in ("1", "true", "yes")
_MAX_PER_CYCLE = int(os.getenv("GOAL_REPORT_MAX_PER_CYCLE", "3"))
_KST = ZoneInfo("Asia/Seoul")


def _weekly_event_key(now: datetime | None = None) -> str:
    """Return the idempotency key for the current KST reporting week."""
    observed = now or datetime.now(_KST)
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=_KST)
    observed = observed.astimezone(_KST)
    iso_year, iso_week, _ = observed.isocalendar()
    return f"weekly_progress:{iso_year}-W{iso_week:02d}"


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
                   m.dispatch_count, m.dispatch_note, m.dispatch_blocked_at,
                   g.id::text AS goal_id, g.title AS goal_title, g.project,
                   CASE WHEN m.status = 'completed' THEN 'milestone_completed'
                        ELSE 'milestone_stuck' END AS event
            FROM milestones m
            JOIN goals g ON g.id = m.goal_id
            WHERE ($1::text IS NULL OR g.project = $1)
              AND (m.status = 'completed'
                   OR (m.status = 'in_progress' AND m.dispatch_blocked_at IS NOT NULL))
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


def _weekly_blockers(milestone: dict[str, Any]) -> list[str]:
    if milestone["status"] in ("completed", "cancelled"):
        return []
    reasons = []
    if milestone["status"] == "blocked":
        reasons.append("마일스톤 blocked")
    if milestone.get("dispatch_blocked_at"):
        reasons.append("발송 차단: " + str(milestone.get("dispatch_note") or "사유 확인 필요"))
    if not milestone.get("owner_session_id"):
        reasons.append("담당 세션 미지정")
    if milestone.get("failed_task_id"):
        reasons.append("실패 작업 링크: " + milestone["failed_task_id"])
    if milestone.get("retry_accounting", {}).get("exhausted"):
        reasons.append("재작업 실제 실행 한도 소진")
    return reasons


async def _weekly_owner_question(conn: Any, goal: Any, milestone: dict[str, Any]) -> str:
    """Ask from an existing lead only; the report log fences weekly submissions.

    Keep the claim on an ambiguous transport error: the relay may have committed.
    A negative response means this milestone was not accepted. In particular,
    the pair-in-flight gate may reject this item while another conversation is
    active, so release only this item's claim and allow a later retry. Keep the
    claim when transport outcome is ambiguous because the relay may have committed.
    """
    origin = goal.get("lead_session_id")
    target = milestone.get("owner_session_id")
    if not origin or origin == target:
        return "담당 확인 요청 보류 — 별도 기존 주도 세션 필요; 자동 실행 없음"
    valid = await conn.fetchval(
        "SELECT count(*) = 2 FROM chat_sessions WHERE id = ANY($1::uuid[]) "
        "AND tenant_id=$2::uuid AND role_key IS NOT NULL "
        "AND lower(role_key) NOT LIKE '%ceo%'",
        [origin, target], goal["tenant_id"],
    )
    if not valid:
        return "담당 확인 요청 보류 — 동일 테넌트의 기존 비CEO 담당/주도 필요"
    event = _weekly_event_key() + ":owner_question"
    subject = milestone["id"]
    claimed = await conn.fetchval(
        "INSERT INTO goal_report_log (goal_id, subject_id, event) "
        "VALUES ($1::uuid, $2::uuid, $3) "
        "ON CONFLICT (goal_id, subject_id, event) DO NOTHING RETURNING id",
        goal["goal_id"], subject, event,
    )
    if not claimed:
        return "담당 확인 요청 제출 시도 기록 있음 — 중복 전달 없음; 접수 여부/실행 시작 미확인"
    from app.services.session_relay import ask
    result = await ask(
        origin_session_id=origin, target=target,
        question=(f"GOAL_ID: {goal['goal_id']}\n"
                  f"목표 {goal['goal_id']} / 항목 {subject}: 차단 원인과 필요한 승인·복구 절차를 확인해 "
                  "주도 세션에 회신해 주세요. 원인 확인 요청이며 실행 승인이 아닙니다. "
                  "작업 재제출, 차단 해제, 실행 예산 초기화, 승인 자동 처리를 하지 마세요."),
    )
    if result.get("sent"):
        return "담당 확인 요청 접수 — 회신/승인 대기; 실행 시작 미확인"
    await conn.execute(
        "DELETE FROM goal_report_log WHERE goal_id=$1::uuid AND subject_id=$2::uuid AND event=$3",
        goal["goal_id"], subject, event,
    )
    if result.get("error") == "already_in_flight":
        return "담당 확인 요청 보류 — 담당 세션에서 다른 대화 진행 중; 재시도 대기"
    return "담당 확인 전달 실패 — " + str(result.get("error") or "접수 미확인")


async def _weekly_followup(conn: Any, goal: Any, milestone: dict[str, Any]) -> str:
    """Use the formal reservation/policy path; never submit a runner job here.

    This runs outside the report transaction: retry reservations must commit
    before proposing. Failed proposals retain their backoff/retry evidence even
    when this week's report has already been delivered.
    """
    if milestone["status"] in ("completed", "cancelled"):
        return "완료/취소 — 조치 없음"
    if goal.get("paused_at") or goal.get("goal_status") in ("completed", "cancelled", "archived"):
        return "목표 일시중지 — 재개 승인 전 후속 요청 없음"
    missing_owner = not milestone.get("owner_session_id")
    if missing_owner and (not milestone.get("owner_role_key") or not goal.get("lead_session_id")):
        return "담당 지정/생성 승인 필요 — 주도 세션/담당 역할 확인; 자동 생성 없음"
    if "ceo" in str(milestone.get("owner_role_key") or "").lower():
        return "CEO 자동 재제출 금지 — 수동 확인 필요"
    if not missing_owner and not milestone.get("failed_task_id") and not goal.get("lead_session_id"):
        return "담당 확인 요청 보류 — 기존 주도 세션 필요; 자동 실행 없음"
    from app.main import _is_active_api_container_for_background_jobs
    if not _is_active_api_container_for_background_jobs():
        return "활성 슬롯 처리 대기 — 요청 미접수"
    locked = False
    try:
        locked = await conn.fetchval(
            "SELECT pg_try_advisory_lock(hashtext('aads_goal_control_cycle'))"
        )
        if not locked:
            return "목표 제어 주기 처리 대기 — 요청 미접수"
        if missing_owner:
            requester = await conn.fetchval(
                "SELECT id::text FROM chat_sessions WHERE id=$1::uuid "
                "AND tenant_id=$2::uuid AND role_key IS NOT NULL "
                "AND lower(role_key) NOT LIKE '%ceo%'",
                goal["lead_session_id"], goal["tenant_id"],
            )
            if not requester:
                return "담당 생성 승인 요청 보류 — 유효한 기존 주도 세션 필요"
            from app.services.owner_session_provision import request_owner_session
            result = await request_owner_session(
                goal_id=goal["goal_id"], role_key=milestone["owner_role_key"],
                project=goal["project"], requester_session_id=requester,
            )
            if result.get("request_id"):
                return "담당 생성 승인 대기 — 요청 " + str(result["request_id"]) + "; 자동 생성/실행 없음"
            return "담당 생성 승인 요청 실패 — 재시도 대기: " + str(result.get("error") or "요청 id 없음")
        if not milestone.get("failed_task_id"):
            return await _weekly_owner_question(conn, goal, milestone)
        from app.services.goal_failure_retry import ensure_retry_candidate
        result = await ensure_retry_candidate(
            conn, milestone_id=milestone["id"], goal_id=goal["goal_id"],
            failed_task_id=milestone["failed_task_id"],
            reason="주간 목표 보고: 실패 링크 복구",
        )
        if result.get("created") or result.get("candidate_alive"):
            return "복구 요청 접수 — 승인 또는 정책 허용 큐 대기; 실행 시작 미확인"
        if result.get("why") in ("successor_active", "successor_completed"):
            return ("기존 후속 작업 진행 중" if result["why"] == "successor_active" else "기존 후속 작업 완료") + " — " + str(result.get("successor") or "")
        return "복구 보류/차단 — " + str(result.get("why") or "상태 확인 필요")
    except Exception as exc:  # noqa: BLE001 — preserve report delivery on recovery failure
        logger.warning("weekly_goal_followup_failed", milestone_id=milestone["id"], error=str(exc)[:200])
        return "복구 연결 실패 — 정식 경로 재시도 대기: " + str(exc)[:160]
    finally:
        if locked:
            try:
                await conn.fetchval(
                    "SELECT pg_advisory_unlock(hashtext('aads_goal_control_cycle'))"
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("weekly_goal_followup_unlock_failed",
                               milestone_id=milestone["id"], error=str(exc)[:200])


async def report_weekly_goal_progress(
    project: str | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, int]:
    """Deliver one KST-weekly milestone rollup to each goal owner session.

    This report intentionally stays inside AADS.  It writes to the owning
    session's inbox and never calls the Telegram/Ohvis alert path.  The
    ``goal_report_log`` row is inserted first with a non-null subject so its
    existing unique key provides an atomic once-per-goal, once-per-week gate.
    """
    if not _ENABLED:
        return {"reported": 0, "skipped_no_owner": 0}

    from app.core.db_pool import get_pool

    event = _weekly_event_key(now)
    reported = 0
    skipped_no_owner = 0
    pool = get_pool()

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
            SELECT g.id::text AS goal_id, g.title, g.project, g.status AS goal_status, g.paused_at,
                   g.tenant_id::text AS tenant_id, g.owner_session_id::text AS lead_session_id,
                   COALESCE(
                       g.owner_session_id,
                       (
                           SELECT s.id
                           FROM goal_task_links l
                           JOIN chat_sessions s ON s.id = l.task_id::uuid
                           WHERE l.goal_id = g.id
                             AND l.task_type = 'chat_session'
                             AND l.link_state = 'active'
                           ORDER BY (s.role_key LIKE '%Lead') DESC, l.created_at
                           LIMIT 1
                       )
                   )::text AS target_session_id,
                   count(m.id) FILTER (WHERE m.status <> 'cancelled') AS milestone_total,
                   count(m.id) FILTER (WHERE m.status = 'completed') AS milestone_completed
            FROM goals g
            LEFT JOIN milestones m ON m.goal_id = g.id
            WHERE ($1::text IS NULL OR g.project = $1)
              AND g.status NOT IN ('completed', 'cancelled', 'archived')
            GROUP BY g.id, g.title, g.project, g.owner_session_id
            ORDER BY g.project, g.title
            """,
            project,
        )

        for row in rows:
            target_session_id = row["target_session_id"]
            if not target_session_id:
                skipped_no_owner += 1
                continue
            total = int(row["milestone_total"] or 0)
            completed = int(row["milestone_completed"] or 0)
            percent = round(100 * completed / total) if total else 0

            # Read actual owners only; never resolve a missing owner to CEO.
            from app.services.goal_binding import (
                DONE_JOB_STATUSES, FAILED_JOB_STATUSES, RELEASE_GATED_PROJECTS,
            )
            from app.services.goal_failure_retry import attach_retry_accounting
            from app.services.goal_manager import (
                active_link_predicate,
                link_optional_columns,
                superseded_link_predicate,
            )
            columns = await link_optional_columns(conn)
            milestones = [dict(m) for m in await conn.fetch(
                f"""SELECT m.id::text, m.title, m.status, m.dispatch_blocked_at,
                           m.dispatch_note, m.evidence, m.owner_session_id::text,
                           COALESCE(s.role_key, m.owner_role_key) AS owner_role_key,
                           (SELECT l.task_id FROM goal_task_links l
                            JOIN pipeline_jobs j ON j.job_id = l.task_id
                            WHERE l.milestone_id=m.id AND l.goal_id=g.id
                              AND l.tenant_id=g.tenant_id AND j.tenant_id=g.tenant_id
                              AND l.task_type='pipeline_job'
                              AND (lower(trim(COALESCE(j.status, ''))) <> ALL($3::text[])
                                   OR (upper(trim(COALESCE(j.project, ''))) = ANY($4::text[])
                                       AND lower(trim(COALESCE(j.phase, ''))) = ANY($2::text[])))
                              AND (lower(trim(COALESCE(j.status, ''))) = ANY($2::text[])
                                   OR lower(trim(COALESCE(j.phase, ''))) = ANY($2::text[]))
                              {active_link_predicate(columns, 'l')}
                              {superseded_link_predicate(columns, 'l')}
                            ORDER BY l.created_at DESC LIMIT 1) AS failed_task_id
                    FROM milestones m JOIN goals g ON g.id=m.goal_id
                    LEFT JOIN chat_sessions s ON s.id=m.owner_session_id AND s.tenant_id=g.tenant_id
                    WHERE g.id=$1::uuid AND m.tenant_id=g.tenant_id
                    ORDER BY m.sequence_order, m.id""", row["goal_id"],
                sorted(FAILED_JOB_STATUSES), sorted(DONE_JOB_STATUSES), sorted(RELEASE_GATED_PROJECTS),
            )]
            milestones = await attach_retry_accounting(conn, row["tenant_id"], milestones)
            details = ["목표 자체 blocked — 주도 담당의 원인 확인/승인 필요"] if row["goal_status"] == "blocked" else []
            if row.get("paused_at"):
                details.append("목표 일시중지 — 재개 승인 전 후속 요청 없음")
            blocked = 0
            for milestone in milestones:
                reasons = _weekly_blockers(milestone)
                if not reasons:
                    continue
                blocked += 1
                action = await _weekly_followup(conn, row, milestone)
                details.append(
                    f"· {milestone['title']}: {', '.join(reasons)}; "
                    f"담당 역할={milestone.get('owner_role_key') or '미지정'}, "
                    f"세션={milestone.get('owner_session_id') or '미지정'}; 다음 행동: {action}"
                )

            if row["goal_status"] == "blocked" and not blocked:
                action = await _weekly_followup(conn, row, {
                    "id": row["goal_id"], "status": "blocked",
                    "owner_session_id": target_session_id, "owner_role_key": "",
                })
                details.append("목표 차단 다음 행동: " + action)

            async with conn.transaction():
                claimed = await conn.fetchval(
                    """
                    INSERT INTO goal_report_log (goal_id, subject_id, event)
                    VALUES ($1::uuid, $1::uuid, $2)
                    ON CONFLICT (goal_id, subject_id, event) DO NOTHING
                    RETURNING id
                    """,
                    row["goal_id"], event,
                )
                if not claimed:
                    continue
                text = (
                    f"주간 목표 달성률 — [{row['project']}] {row['title']}\n\n"
                    f"마일스톤 {completed}/{total} 완료 ({percent}%), "
                    f"차단 {blocked}건\n"
                    f"보고 수신 세션={target_session_id}\n"
                    + "\n".join(details)
                    + "\n이 알림 저장은 실행 성공을 뜻하지 않습니다."
                )
                await conn.execute(
                    """
                    INSERT INTO chat_messages (session_id, role, content, intent)
                    VALUES ($1::uuid, 'assistant', $2, 'goal_weekly_report')
                    """,
                    target_session_id, text,
                )
            reported += 1
            logger.info(
                "weekly_goal_progress_sent",
                goal_id=row["goal_id"], project=row["project"],
                completed=completed, total=total, blocked=blocked,
                report_event=event,
            )

    return {"reported": reported, "skipped_no_owner": skipped_no_owner}
