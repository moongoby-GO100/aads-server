"""재작업 소진 주도 판단 요청 — 기록 먼저, 발송은 커밋 뒤 별도 스윕.

소진 시점(`ensure_retry_candidate`)에는 요청 행 1개만 남긴다. 발송·무응답 판정·대표님 상신은
`sweep_exhaust_requests()` 가 목표 주기에서 따로 처리한다. 외부 호출은 DB INSERT 로 끝나는
`enqueue_next_step_reaction` 과 `ohvis_alert.notify` 둘뿐이다.

상태 전이: lead pending -> sent -> resolved | escalated(+ceo 행) / undeliverable(+ceo 행)
           ceo  pending -> sent | undeliverable
"""
from __future__ import annotations

import logging
import os
from typing import Any

from app.core.db_pool import get_pool

logger = logging.getLogger(__name__)

_TABLE = "goal_retry_exhaust_requests"
_BATCH = 20
_CLOSED = ("completed", "cancelled")


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except ValueError:
        return default


def _max_attempts() -> int:
    return _env_int("GOAL_RETRY_EXHAUST_MAX_ATTEMPTS", 5)


def _lead_wait_minutes() -> int:
    return _env_int("GOAL_RETRY_EXHAUST_LEAD_WAIT_MINUTES", 60)


async def _table_exists(conn) -> bool:
    return bool(await conn.fetchval(f"SELECT to_regclass('public.{_TABLE}')"))


# ── ① 기록 ────────────────────────────────────────────────────────────────────

async def record_exhaustion_request(
    conn, *, tenant_id: str, goal_id: str, milestone_id: str, lineage: str,
    milestone_status: str | None, executions: int,
) -> str:
    """소진 분기의 기존 트랜잭션 안에서 부른다. 발송은 하지 않고, 실패해도 바깥을 깨지 않는다."""
    if milestone_status == "completed":
        return "skipped_completed"
    try:
        async with conn.transaction():  # savepoint — 실패가 바깥 트랜잭션을 aborted 로 만들지 않는다
            if not await _table_exists(conn):
                return "skipped_no_table"
            inserted = await conn.fetchval(
                f"""INSERT INTO {_TABLE}
                        (tenant_id, goal_id, milestone_id, lineage, kind,
                         milestone_status_at_request, executions)
                    VALUES ($1::uuid, $2::uuid, $3::uuid, $4, 'lead', $5, $6)
                    ON CONFLICT (tenant_id, milestone_id, lineage, kind) DO NOTHING
                    RETURNING id::text""",
                tenant_id, goal_id, milestone_id, lineage, milestone_status, executions,
            )
            return "recorded" if inserted else "exists"
    except Exception as exc:
        logger.warning("goal_retry_exhaust_record_failed milestone=%s error=%s",
                       milestone_id, str(exc)[:200])
        return "error"


# ── 공통 ──────────────────────────────────────────────────────────────────────

async def _insert_ceo_from(conn, lead_id: str) -> None:
    await conn.execute(
        f"""INSERT INTO {_TABLE}
                (tenant_id, goal_id, milestone_id, lineage, kind,
                 milestone_status_at_request, executions)
            SELECT tenant_id, goal_id, milestone_id, lineage, 'ceo',
                   milestone_status_at_request, executions
            FROM {_TABLE} WHERE id = $1::uuid
            ON CONFLICT (tenant_id, milestone_id, lineage, kind) DO NOTHING""",
        lead_id,
    )


async def _fail_and_maybe_give_up(pool, row_id: str, error: str, *, escalate: bool) -> None:
    """시도 횟수를 올리고 최대치면 undeliverable 로 닫는다. escalate 면 대표님 행도 만든다."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            new_status = await conn.fetchval(
                f"""UPDATE {_TABLE}
                    SET attempts = attempts + 1, last_error = $2,
                        status = CASE WHEN attempts + 1 >= $3 THEN 'undeliverable' ELSE status END,
                        resolution = CASE WHEN attempts + 1 >= $3 THEN 'send_failed' ELSE resolution END,
                        resolved_at = CASE WHEN attempts + 1 >= $3 THEN NOW() ELSE resolved_at END,
                        updated_at = NOW()
                    WHERE id = $1::uuid AND status = 'pending'
                    RETURNING status""",
                row_id, error[:300], _max_attempts(),
            )
            if new_status == "undeliverable":
                if escalate:
                    await _insert_ceo_from(conn, row_id)
                else:
                    logger.warning("goal_retry_exhaust_ceo_undeliverable id=%s error=%s",
                                   row_id[:8], error[:120])


_SELECT_ROWS = f"""
    SELECT r.id::text AS id, r.tenant_id::text AS tenant_id, r.goal_id::text AS goal_id,
           r.milestone_id::text AS milestone_id, r.lineage, r.executions, r.attempts,
           g.title AS goal_title, g.project AS project,
           g.owner_session_id::text AS owner_session_id,
           r.target_session_id::text AS target_session_id, r.delivery_ref AS delivery_ref,
           m.title AS milestone_title, m.status AS milestone_status
    FROM {_TABLE} r
    LEFT JOIN goals g ON g.id = r.goal_id AND g.tenant_id = r.tenant_id
    LEFT JOIN milestones m ON m.id = r.milestone_id AND m.tenant_id = r.tenant_id
    WHERE r.kind = $1 AND r.status = 'pending'
    ORDER BY r.created_at
    LIMIT {_BATCH}"""


# ── ② 주도 발송 ───────────────────────────────────────────────────────────────

def _lead_message(row: Any) -> str:
    return (
        f"[재작업 소진] 목표 «{row['goal_title']}» / 마일스톤 «{row['milestone_title']}»\n"
        f"계보: {row['lineage']} — 재작업 실제 실행 {row['executions']}/2 소진.\n"
        "시스템은 상태를 바꾸지 않았다. 주도가 아래 중 하나를 골라 진행하라.\n"
        "1) 승인 범위 안에서 재제출\n"
        "2) 범위를 줄여 재설계\n"
        "3) 취소 의견 제출\n"
        "4) 대표님 상신 요청\n"
        f"{_lead_wait_minutes()}분 안에 새 작업 연결이나 상태 변경이 없으면 대표님께 올라간다."
    )


async def _enqueue(session_id: str, message: str, dedupe_key: str) -> dict:
    from app.services.chat_service import enqueue_next_step_reaction

    return await enqueue_next_step_reaction(
        session_id, message, dedupe_key=dedupe_key, reuse_seconds=0,
    )


async def _resolve_pending(pool, row_id: str, resolution: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            f"""UPDATE {_TABLE}
                SET status = 'resolved', resolution = $2, resolved_at = NOW(), updated_at = NOW()
                WHERE id = $1::uuid AND status = 'pending'""",
            row_id, resolution,
        )


async def _give_up_no_lead(pool, row_id: str) -> None:
    async with pool.acquire() as conn:
        async with conn.transaction():
            closed = await conn.fetchval(
                f"""UPDATE {_TABLE}
                    SET status = 'undeliverable', resolution = 'no_lead_session',
                        resolved_at = NOW(), updated_at = NOW()
                    WHERE id = $1::uuid AND status = 'pending'
                    RETURNING id::text""",
                row_id,
            )
            if closed:
                await _insert_ceo_from(conn, row_id)


async def _session_alive(pool, session_id: str, tenant_id: str) -> bool:
    async with pool.acquire() as conn:
        return bool(await conn.fetchval(
            "SELECT id::text FROM chat_sessions WHERE id = $1::uuid AND tenant_id = $2::uuid",
            session_id, tenant_id,
        ))


async def _pin_target(pool, row: Any) -> str | None:
    """수신자와 첫 시도 시각을 발송 전에 고정한다. 이미 고정된 행은 owner 를 다시 읽지 않는다."""
    if row["target_session_id"]:
        return row["target_session_id"] if await _session_alive(
            pool, row["target_session_id"], row["tenant_id"]) else None
    if not row["owner_session_id"] or not await _session_alive(
            pool, row["owner_session_id"], row["tenant_id"]):
        return None
    async with pool.acquire() as conn:
        pinned = await conn.fetchval(
            f"""UPDATE {_TABLE}
                SET target_session_id = $2::uuid,
                    first_attempt_at = COALESCE(first_attempt_at, NOW()), updated_at = NOW()
                WHERE id = $1::uuid AND status = 'pending' AND target_session_id IS NULL
                RETURNING target_session_id::text""",
            row["id"], row["owner_session_id"],
        )
    # 동시에 다른 스윕이 먼저 고정했으면 이번 회차는 건너뛰고 다음 스윕이 저장값을 쓴다.
    return pinned or ""


async def _send_one_lead(pool, row: Any, stats: dict) -> None:
    row_id = row["id"]
    if row["milestone_status"] is None or row["milestone_status"] in _CLOSED:
        await _resolve_pending(pool, row_id, "milestone_closed")
        stats["closed"] += 1
        return
    session_id = await _pin_target(pool, row)
    if session_id is None:
        await _give_up_no_lead(pool, row_id)
        stats["no_lead"] += 1
        return
    if not session_id:
        return
    try:
        queued = await _enqueue(session_id, _lead_message(row), f"goal_retry_exhaust:{row_id}")
    except Exception as exc:
        await _fail_and_maybe_give_up(pool, row_id, f"enqueue: {exc}", escalate=True)
        stats["send_failed"] += 1
        return
    # 여기서 죽거나 UPDATE 가 실패해도 다음 스윕이 저장된 수신자·같은 dedupe_key 로 기존 큐를 돌려받는다.
    async with pool.acquire() as conn:
        await conn.execute(
            f"""UPDATE {_TABLE}
                SET status = 'sent', sent_at = COALESCE(sent_at, NOW()), delivery_ref = $2,
                    last_error = NULL, updated_at = NOW()
                WHERE id = $1::uuid AND status = 'pending'""",
            row_id, str(queued.get("queue_id")),
        )
    stats["sent"] += 1


# ── ③ 무응답 판정 ─────────────────────────────────────────────────────────────

async def _judge_one_sent(pool, row_id: str, stats: dict) -> None:
    async with pool.acquire() as conn:
        async with conn.transaction():
            req = await conn.fetchrow(
                f"""SELECT r.id::text AS id, r.tenant_id::text AS tenant_id,
                           r.milestone_id::text AS milestone_id,
                           COALESCE(r.first_attempt_at, r.sent_at) AS baseline_at,
                           r.milestone_status_at_request AS status_at_request,
                           m.status AS current_status
                    FROM {_TABLE} r
                    LEFT JOIN milestones m ON m.id = r.milestone_id AND m.tenant_id = r.tenant_id
                    WHERE r.id = $1::uuid AND r.status = 'sent'
                    FOR UPDATE OF r""",
                row_id,
            )
            if not req:
                return
            responded = req["current_status"] != req["status_at_request"]
            if not responded:
                responded = bool(await conn.fetchval(
                    """SELECT 1 FROM goal_task_links
                       WHERE milestone_id = $1::uuid AND tenant_id = $2::uuid
                         AND created_at > $3 LIMIT 1""",
                    req["milestone_id"], req["tenant_id"], req["baseline_at"],
                ))
            if responded:
                await conn.execute(
                    f"""UPDATE {_TABLE}
                        SET status = 'resolved', resolution = 'lead_acted',
                            resolved_at = NOW(), updated_at = NOW()
                        WHERE id = $1::uuid AND status = 'sent'""",
                    row_id,
                )
                stats["resolved"] += 1
                return
            escalated = await conn.fetchval(
                f"""UPDATE {_TABLE}
                    SET status = 'escalated', resolution = 'lead_no_response',
                        resolved_at = NOW(), updated_at = NOW()
                    WHERE id = $1::uuid AND status = 'sent'
                    RETURNING id::text""",
                row_id,
            )
            if escalated:
                await _insert_ceo_from(conn, row_id)
                stats["escalated"] += 1


# ── ④ 대표님 상신 ─────────────────────────────────────────────────────────────

def _ceo_title(row: Any) -> str:
    goal = (row["goal_title"] or "(삭제된 목표)")[:40]
    milestone = (row["milestone_title"] or "(삭제된 마일스톤)")[:40]
    return f"[재작업 소진] {goal} / {milestone} ({row['id'][:8]})"


async def _notify(title: str, message: str, project: str | None) -> bool:
    from app.services.ohvis_alert import WARNING, notify

    return await notify(
        title, message, severity=WARNING, category="goal_retry_exhausted",
        project=project, dedupe_minutes=0,
    )


async def _mark_ceo_sent(pool, row_id: str, ref: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            f"""UPDATE {_TABLE}
                SET status = 'sent', sent_at = COALESCE(sent_at, NOW()), delivery_ref = $2,
                    last_error = NULL, updated_at = NOW()
                WHERE id = $1::uuid AND status = 'pending'""",
            row_id, ref[:300],
        )


async def _pin_ceo_title(pool, row: Any) -> str | None:
    """notify 전에 제목을 행에 저장한다. 이후 중복 확인·재시도는 저장된 제목만 쓴다."""
    if row["delivery_ref"]:
        return row["delivery_ref"]
    async with pool.acquire() as conn:
        return await conn.fetchval(
            f"""UPDATE {_TABLE}
                SET delivery_ref = $2, updated_at = NOW()
                WHERE id = $1::uuid AND status = 'pending' AND delivery_ref IS NULL
                RETURNING delivery_ref""",
            row["id"], _ceo_title(row)[:300],
        )


async def _escalate_one_ceo(pool, row: Any, stats: dict) -> None:
    title = await _pin_ceo_title(pool, row)
    if not title:
        return
    async with pool.acquire() as conn:
        already = await conn.fetchval("SELECT 1 FROM alert_history WHERE title = $1 LIMIT 1", title)
    if already:
        await _mark_ceo_sent(pool, row["id"], title)
        stats["ceo_sent"] += 1
        return
    message = (
        f"목표 «{row['goal_title']}» 마일스톤 «{row['milestone_title']}» 의 재작업이 "
        f"{row['executions']}/2 회 소진됐다(계보 {row['lineage']}). "
        "주도 판단 요청에 응답이 없거나 주도 세션이 없어 올린다. 시스템은 상태를 바꾸지 않았다."
    )
    ok = False
    try:
        ok = await _notify(title, message, row["project"])
    except Exception as exc:
        logger.warning("goal_retry_exhaust_notify_error id=%s error=%s", row["id"][:8], str(exc)[:160])
    if ok:
        await _mark_ceo_sent(pool, row["id"], title)
        stats["ceo_sent"] += 1
        return
    await _fail_and_maybe_give_up(pool, row["id"], "notify_false", escalate=False)
    stats["ceo_failed"] += 1


# ── 스윕 ──────────────────────────────────────────────────────────────────────

async def sweep_exhaust_requests() -> dict:
    pool = get_pool()
    async with pool.acquire() as conn:
        if not await _table_exists(conn):
            return {"skipped": "no_table"}
    stats = {"sent": 0, "closed": 0, "no_lead": 0, "send_failed": 0,
             "resolved": 0, "escalated": 0, "ceo_sent": 0, "ceo_failed": 0}

    async def _rows(kind: str) -> list:
        async with pool.acquire() as conn:
            return list(await conn.fetch(_SELECT_ROWS, kind))

    async def _guarded(label: str, row_id: str, coro) -> None:
        try:
            await coro
        except Exception as exc:
            logger.warning("goal_retry_exhaust_%s_failed id=%s error=%s",
                           label, str(row_id)[:8], str(exc)[:200])

    try:
        for row in await _rows("lead"):
            await _guarded("send", row["id"], _send_one_lead(pool, row, stats))
    except Exception as exc:
        logger.warning("goal_retry_exhaust_send_stage_failed error=%s", str(exc)[:200])

    try:
        async with pool.acquire() as conn:
            sent_ids = [r["id"] for r in await conn.fetch(
                f"""SELECT id::text AS id FROM {_TABLE}
                    WHERE kind = 'lead' AND status = 'sent'
                      AND COALESCE(first_attempt_at, sent_at) < NOW() - make_interval(mins => $1::int)
                    ORDER BY COALESCE(first_attempt_at, sent_at) LIMIT {_BATCH}""",
                _lead_wait_minutes(),
            )]
        for row_id in sent_ids:
            await _guarded("judge", row_id, _judge_one_sent(pool, row_id, stats))
    except Exception as exc:
        logger.warning("goal_retry_exhaust_judge_stage_failed error=%s", str(exc)[:200])

    try:
        for row in await _rows("ceo"):
            await _guarded("ceo", row["id"], _escalate_one_ceo(pool, row, stats))
    except Exception as exc:
        logger.warning("goal_retry_exhaust_ceo_stage_failed error=%s", str(exc)[:200])

    if any(stats.values()):
        logger.info("goal_retry_exhaust_sweep %s", stats)
    return stats
