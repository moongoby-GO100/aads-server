"""추가 지시(interrupt)의 durable 수명주기.

RECEIVED(원문 저장 성공) → QUEUED(현재 실행에 반영 대기) → APPLIED(실제 pop/consume)
→ WORKING(반영 후 모델 출력 시작) → DONE(턴 저장 완료). 취소·만료는 CANCELLED/EXPIRED.

``chat_messages.intent`` 는 기존 값을 그대로 쓰고(호환), 상태·시각·대기 사유는
``chat_interrupt_states`` 에 남긴다. 상태는 앞으로만 간다 — 이미 APPLIED 인 지시가
다시 QUEUED 로 돌아가지 않는다. ``queued=true`` 는 APPLIED 가 아니다.

테이블이 아직 없는 배포 전환 구간에서는 상태 기록만 건너뛰고(접수 자체는 원문 행이
durable 하다), 조회는 intent 로부터 상태를 유도한다.
"""

from __future__ import annotations

import hashlib
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

STATES = ("RECEIVED", "QUEUED", "APPLIED", "WORKING", "DONE", "CANCELLED", "EXPIRED")
TERMINAL_STATES = frozenset({"DONE", "CANCELLED", "EXPIRED"})
WAIT_REASONS = ("tool_running", "relay_wait", "model_pre_output", "none")

SUMMARY_LIMIT = 120
STATUS_HEARTBEAT_SECONDS = 12.0

_KST = timezone(timedelta(hours=9))
_RELAY_WAIT_TOKENS = ("codex_relay_busy", "relay_semaphore_timeout")

# 기존 intent → 상태. 상태 행이 없는 지시(전환 구간·구버전 접수)를 조회할 때만 쓴다.
_LEGACY_INTENT_STATE = {
    "queued_interrupt": "QUEUED",
    "interrupt_needs_confirm": "QUEUED",
    "interrupt_confirmed": "QUEUED",
    "interrupt_applied": "APPLIED",
    "recovered_interrupt": "APPLIED",
    "interrupt_completed": "DONE",
    "interrupt_cancelled": "CANCELLED",
    "interrupt_expired": "EXPIRED",
}

INTERRUPT_REPLY_INSTRUCTION = (
    "\n\n[응답 형식] 이 추가 지시를 읽은 직후 첫 문단은 한 줄로 "
    "'추가 지시 반영: 기존 A 계속, B 우선/추가. 지금 C 확인 중, 다음 D' 형식으로 쓰세요 "
    "(A=기존 작업, B=새 지시, C=지금 하는 일, D=다음 단계). "
    "내부 추론은 쓰지 말고, 이어서 작업을 계속하세요."
)

_WAIT_LABEL = {
    "tool_running": "진행 중인 도구 결과",
    "relay_wait": "응답 슬롯 배정",
    "model_pre_output": "모델 첫 응답",
    "none": "현재 단계",
}


def _is_undefined_table(exc: BaseException) -> bool:
    return type(exc).__name__ == "UndefinedTableError"


def to_kst_iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, datetime):
        return str(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(_KST).isoformat()


def build_summary(content: Any) -> str:
    """앞 120자 정규화 요약. 지시 번호·대상·시각은 앞부분 그대로 남기고 비밀만 가린다."""
    from app.services.llmops_store import mask_secrets

    text = re.sub(r"\s+", " ", str(content or "")).strip()
    text = mask_secrets(text)
    return text[:SUMMARY_LIMIT]


def build_public_reply(summary: str, wait_reason: str = "none") -> str:
    """모델 출력 전에도 서버가 먼저 남길 수 있는 최소 반영 문구."""
    label = _WAIT_LABEL.get(wait_reason, _WAIT_LABEL["none"])
    shown = (summary or "")[:60]
    return (
        f"추가 지시 반영: 기존 작업 계속, '{shown}' 추가 반영. "
        f"지금 {label} 확인 중, 다음 반영 결과 보고."
    )


def classify_wait_reason(
    *,
    status: Optional[str],
    tool_events: Iterable[dict[str, Any]],
    has_output: bool,
    error_message: Optional[str] = None,
) -> str:
    """지시가 왜 아직 반영되지 않는지. ``tool_events`` 는 normalize_tool_events 결과."""
    pending: dict[str, str] = {}
    for ev in tool_events or []:
        kind = ev.get("type")
        key = str(ev.get("tool_use_id") or ev.get("tool_name") or "")
        if kind == "tool_use":
            pending[key] = str(ev.get("tool_name") or "")
        elif kind == "tool_result":
            pending.pop(key, None)
            if not ev.get("tool_use_id"):
                pending.pop(str(ev.get("tool_name") or ""), None)
    if pending:
        return "tool_running"
    err = str(error_message or "")
    if status == "retrying" and any(tok in err for tok in _RELAY_WAIT_TOKENS):
        return "relay_wait"
    if not has_output:
        return "model_pre_output"
    return "none"


def _item(row: Any) -> dict[str, Any]:
    r = dict(row)
    return {
        "id": str(r["message_id"]),
        "state": r["state"],
        "wait_reason": r.get("wait_reason") or "none",
        "summary": r.get("summary") or "",
        "public_reply": r.get("public_reply"),
        "received_at": to_kst_iso(r.get("received_at")),
        "queued_at": to_kst_iso(r.get("queued_at")),
        "applied_at": to_kst_iso(r.get("applied_at")),
        "working_at": to_kst_iso(r.get("working_at")),
        "done_at": to_kst_iso(r.get("done_at")),
        "execution_id": str(r["execution_id"]) if r.get("execution_id") else None,
        "generation_id": str(r["generation_id"]) if r.get("generation_id") else None,
        "owner_instance": r.get("owner_instance"),
    }


def _uuid(value: Any) -> Optional[uuid.UUID]:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


# ── 상태 기록 (conn 을 받는다 — 호출자가 트랜잭션을 정한다) ──────────────
#
# 상태 테이블을 건드리는 쿼리는 전부 ``_guarded`` 를 거친다. 호출자가 이미 트랜잭션 안에
# 있으면 savepoint 가 되므로, 테이블 부재(배포 전환 구간)든 다른 오류든 실패한 문장이
# 호출자의 트랜잭션을 abort 상태로 남기지 않는다.


async def _guarded(conn: Any, work: Any, default: Any = None) -> Any:
    """``work()`` 를 savepoint 안에서 실행한다. 테이블 부재만 ``default`` 로 삼키고,
    그 외 오류는 savepoint 를 롤백한 뒤 그대로 전파한다."""
    try:
        async with conn.transaction():
            return await work()
    except Exception as exc:
        if _is_undefined_table(exc):
            return default
        raise


async def lock_idempotency(conn: Any, session_id: Any, key: str) -> None:
    """같은 (세션, 키) 의 동시 접수를 직렬화한다. 호출자의 트랜잭션이 끝나면 풀린다.

    조회→INSERT 사이에 락이 없으면 동시 재전송 둘이 모두 "없음"을 보고 INSERT 해서
    하나가 유니크 위반(=접수 실패 503)이 된다. 락을 잡으면 뒤 요청이 앞 요청의
    커밋을 기다렸다가 같은 키의 기존 접수를 그대로 돌려받는다.
    """
    await conn.fetchval(
        """
        -- ilc:lock_idem
        SELECT pg_advisory_xact_lock(hashtextextended($1, 0))
        """,
        f"chat_interrupt_idem:{session_id}:{key}",
    )


async def find_by_idempotency_key(conn: Any, session_id: Any, key: str) -> Optional[str]:
    async def _q() -> Any:
        return await conn.fetchval(
            """
            -- ilc:find_idem
            SELECT message_id::text FROM chat_interrupt_states
             WHERE session_id = $1 AND idempotency_key = $2
            """,
            session_id,
            key,
        )

    found = await _guarded(conn, _q)
    return str(found) if found else None


async def record_receipt(
    conn: Any,
    *,
    message_id: Any,
    session_id: Any,
    content: str,
    wait_reason: str,
    execution_id: Optional[str],
    generation_id: Optional[str],
    owner_instance: Optional[str],
    idempotency_key: Optional[str],
) -> dict[str, Any]:
    """접수 상태를 쓴다. 실행에 붙일 수 있으면 QUEUED, 아니면 RECEIVED.

    테이블 부재(UndefinedTable)만 삼킨다. 유니크 위반 등 다른 오류는 호출자의
    트랜잭션을 깨뜨려 접수 실패(5xx)가 된다 — 접수 확인을 거짓으로 내보내지 않는다.
    같은 idempotency_key 의 동시 접수는 호출자가 ``lock_idempotency`` 로 직렬화한다.
    """
    state = "QUEUED" if execution_id else "RECEIVED"
    reason = wait_reason if wait_reason in WAIT_REASONS else "none"
    summary = build_summary(content)
    fallback = {
        "id": str(message_id), "state": state, "wait_reason": reason,
        "summary": summary, "public_reply": None, "persisted": False,
    }

    async def _insert() -> Any:
        return await conn.fetchrow(
            """
            -- ilc:insert
            INSERT INTO chat_interrupt_states
                (message_id, session_id, state, wait_reason, summary, idempotency_key,
                 execution_id, generation_id, owner_instance, received_at, queued_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7::uuid, $8::uuid, $9, clock_timestamp(),
                    CASE WHEN $3 = 'QUEUED' THEN clock_timestamp() END)
            ON CONFLICT (message_id) DO NOTHING
            RETURNING *
            """,
            message_id,
            session_id,
            state,
            reason,
            summary,
            idempotency_key or None,
            _uuid(execution_id),
            _uuid(generation_id),
            owner_instance,
        )

    missing = object()
    row = await _guarded(conn, _insert, default=missing)
    if row is missing:
        logger.warning("interrupt_state_table_missing message=%s", str(message_id)[:8])
        return fallback
    if row is None:
        existing = await get_item(conn, message_id)
        return {**existing, "persisted": True} if existing else fallback
    return {**_item(row), "persisted": True}


async def get_item(conn: Any, message_id: Any) -> Optional[dict[str, Any]]:
    async def _q() -> Any:
        return await conn.fetchrow(
            """
            -- ilc:get
            SELECT * FROM chat_interrupt_states WHERE message_id = $1
            """,
            message_id,
        )

    row = await _guarded(conn, _q)
    return _item(row) if row else None


def _legacy_item(r: Any) -> dict[str, Any]:
    """상태 행이 없는 지시를 chat_messages.intent 로부터 유도한 항목."""
    state = _LEGACY_INTENT_STATE.get(r["intent"], "QUEUED")
    edited = r["edited_at"]
    return _item({
        "message_id": r["message_id"],
        "state": state,
        "wait_reason": "none",
        "summary": build_summary(r["content"]),
        "received_at": r["created_at"],
        "applied_at": edited if state in ("APPLIED", "DONE") else None,
        "done_at": edited if state == "DONE" else None,
    })


async def get_receipt(conn: Any, message_id: Any) -> Optional[dict[str, Any]]:
    """상태 행이 있으면 그것을, 없으면(전환 구간·구버전 접수) 원문 행의 intent 로 유도한다.

    재전송 응답이 이미 APPLIED/DONE 인 지시를 QUEUED 로 되돌려 말하지 않게 한다.
    """
    item = await get_item(conn, message_id)
    if item:
        return item
    row = await conn.fetchrow(
        """
        -- ilc:msg
        SELECT id AS message_id, intent, content, created_at, edited_at
          FROM chat_messages WHERE id = $1
        """,
        message_id,
    )
    return _legacy_item(row) if row else None


async def _apply_one(
    conn: Any,
    session_id: Any,
    mid: Any,
    execution_id: Optional[str],
    generation_id: Optional[str],
) -> None:
    """한 건을 APPLIED 로. 상태·시각·공개 문구를 한 문장으로 써서 중간 상태가 남지 않는다."""

    async def _work() -> None:
        row = await conn.fetchrow(
            """
            -- ilc:apply_lock
            SELECT summary, wait_reason FROM chat_interrupt_states
             WHERE message_id = $1 AND session_id = $2 AND state IN ('RECEIVED', 'QUEUED')
               FOR UPDATE
            """,
            mid,
            session_id,
        )
        if row is None:
            return
        await conn.execute(
            """
            -- ilc:apply
            UPDATE chat_interrupt_states
               SET state = 'APPLIED',
                   applied_at = clock_timestamp(),
                   execution_id = COALESCE(execution_id, $2::uuid),
                   generation_id = COALESCE(generation_id, $3::uuid),
                   applied_execution_id = $2::uuid,
                   public_reply = $4,
                   wait_reason = 'none',
                   updated_at = NOW()
             WHERE message_id = $1
            """,
            mid,
            _uuid(execution_id),
            _uuid(generation_id),
            build_public_reply(row["summary"], row["wait_reason"]),
        )

    await _guarded(conn, _work)


async def mark_applied(
    conn: Any,
    session_id: Any,
    message_ids: Iterable[Any],
    *,
    execution_id: Optional[str] = None,
    generation_id: Optional[str] = None,
) -> list[dict[str, Any]]:
    """지시를 소비한 시점에 APPLIED 로. 건별로 순서대로 시각을 찍는다.

    한 건의 실패가 나머지를 막지 않는다 — 실패한 건은 상태가 그대로(QUEUED)이고
    이벤트는 message_id 없이 나간다.
    """
    applied: list[dict[str, Any]] = []
    for mid in message_ids:
        try:
            await _apply_one(conn, session_id, mid, execution_id, generation_id)
            current = await get_item(conn, mid)
        except Exception as exc:
            logger.warning("interrupt_apply_one_failed message=%s error=%s", str(mid)[:8], str(exc)[:160])
            continue
        if current:
            applied.append(current)
    return applied


async def mark_working(conn: Any, session_id: Any, execution_id: Optional[str] = None) -> int:
    """이 실행이 반영한 지시만 WORKING 으로. 같은 세션의 다른 실행이 반영한 것은 건드리지 않는다."""
    return await _execute_state(
        conn,
        """
        -- ilc:working
        UPDATE chat_interrupt_states
           SET state = 'WORKING', working_at = clock_timestamp(), updated_at = NOW()
         WHERE session_id = $1 AND state = 'APPLIED'
           AND ($2::uuid IS NULL OR applied_execution_id IS NULL OR applied_execution_id = $2::uuid)
        """,
        session_id,
        _uuid(execution_id),
    )


async def mark_done(conn: Any, session_id: Any, execution_id: Optional[str] = None) -> int:
    """턴 저장 완료 시점. 이 실행이 반영한 지시만 DONE 으로 올린다."""
    return await _execute_state(
        conn,
        """
        -- ilc:done
        UPDATE chat_interrupt_states
           SET state = 'DONE', done_at = clock_timestamp(), wait_reason = 'none', updated_at = NOW()
         WHERE session_id = $1 AND state IN ('APPLIED', 'WORKING')
           AND ($2::uuid IS NULL OR applied_execution_id IS NULL OR applied_execution_id = $2::uuid)
        """,
        session_id,
        _uuid(execution_id),
    )


async def mark_cancelled(conn: Any, session_id: Any, message_ids: Iterable[Any]) -> int:
    ids = [_uuid(m) for m in message_ids if _uuid(m)]
    if not ids:
        return 0
    return await _execute_state(
        conn,
        """
        -- ilc:cancel
        UPDATE chat_interrupt_states
           SET state = 'CANCELLED', wait_reason = 'none', updated_at = NOW()
         WHERE session_id = $1 AND message_id = ANY($2::uuid[])
           AND state IN ('RECEIVED', 'QUEUED')
        """,
        session_id,
        ids,
    )


async def mark_expired(conn: Any, session_id: Any = None) -> int:
    return await _execute_state(
        conn,
        """
        -- ilc:expire
        UPDATE chat_interrupt_states s
           SET state = 'EXPIRED', wait_reason = 'none', updated_at = NOW()
          FROM chat_messages m
         WHERE m.id = s.message_id
           AND m.intent = 'interrupt_expired'
           AND s.state IN ('RECEIVED', 'QUEUED')
           AND ($1::uuid IS NULL OR s.session_id = $1::uuid)
        """,
        session_id,
    )


async def refresh_wait_reason(conn: Any, session_id: Any, wait_reason: str) -> int:
    if wait_reason not in WAIT_REASONS:
        return 0
    return await _execute_state(
        conn,
        """
        -- ilc:refresh
        UPDATE chat_interrupt_states
           SET wait_reason = $2, updated_at = NOW()
         WHERE session_id = $1 AND state IN ('RECEIVED', 'QUEUED') AND wait_reason <> $2
        """,
        session_id,
        wait_reason,
    )


async def _execute_state(conn: Any, sql: str, *args: Any) -> int:
    async def _q() -> int:
        return _count(await conn.execute(sql, *args))

    return await _guarded(conn, _q, default=0)


def _count(result: Any) -> int:
    try:
        return int(str(result).split()[-1])
    except (ValueError, IndexError):
        return 0


_LEGACY_SQL = """
    -- ilc:{tag}
    SELECT m.id AS message_id, m.intent, m.content, m.created_at, m.edited_at
      FROM chat_messages m
     WHERE m.session_id = $1
       AND m.role = 'user'
       AND m.intent IN ('queued_interrupt', 'interrupt_applied', 'interrupt_completed')
       AND m.created_at > NOW() - INTERVAL '24 hours'
       {not_exists}
     ORDER BY m.created_at ASC
     LIMIT $2
"""
_NOT_EXISTS_STATE = "AND NOT EXISTS (SELECT 1 FROM chat_interrupt_states s WHERE s.message_id = m.id)"


async def fetch_snapshot(
    conn: Any, session_id: Any, *, limit: int = 20, include_legacy: bool = True
) -> list[dict[str, Any]]:
    """세션의 추가 지시 상태. DB 만 본다 — 새로고침·재연결·다른 슬롯에서 같은 결과.

    ``include_legacy=False`` 는 heartbeat 용: 상태 테이블만 한 번 읽는다.
    모든 쿼리는 savepoint 안에서 돌아, 실패해도 호출자가 쥔 트랜잭션을 abort 시키지 않는다.
    """

    async def _states() -> Any:
        return await conn.fetch(
            """
            -- ilc:snapshot
            SELECT * FROM (
                SELECT * FROM chat_interrupt_states
                 WHERE session_id = $1
                   AND (state NOT IN ('DONE', 'CANCELLED', 'EXPIRED')
                        OR received_at > NOW() - INTERVAL '24 hours')
                 ORDER BY received_at DESC
                 LIMIT $2
            ) recent ORDER BY received_at ASC
            """,
            session_id,
            limit,
        )

    rows = await _guarded(conn, _states, default=None)
    table_ready = rows is not None
    items = [_item(r) for r in rows or []]
    if not include_legacy:
        return items

    async def _legacy() -> Any:
        sql = _LEGACY_SQL.format(
            tag="legacy" if table_ready else "legacy_nostate",
            not_exists=_NOT_EXISTS_STATE if table_ready else "",
        )
        return await conn.fetch(sql, session_id, limit)

    for r in await _guarded(conn, _legacy, default=[]) or []:
        items.append(_legacy_item(r))
    items.sort(key=lambda i: i["received_at"] or "")
    return items


# ── 이벤트 ────────────────────────────────────────────────────────────────


def applied_event(item: dict[str, Any], content: str = "") -> dict[str, Any]:
    return {
        "type": "interrupt_applied",
        "content": (content or item.get("summary") or "")[:100],
        "message_id": item["id"],
        "state": item["state"],
        "applied_at": item.get("applied_at"),
        "public_reply": item.get("public_reply"),
    }


async def apply_interrupts(session_id: str, interrupts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """소비한 지시를 APPLIED 로 올리고 SSE 이벤트를 돌려준다. 절대 raise 하지 않는다.

    상태 기록 실패가 지시 반영을 막으면 안 된다 — 이벤트는 message_id 없이도 나간다.
    """
    events: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    ordered: list[Any] = []
    for intr in interrupts or []:
        mid = intr.get("message_id")
        if mid and _uuid(mid) and str(mid) not in by_id:
            by_id[str(mid)] = intr
            ordered.append(_uuid(mid))
    applied: dict[str, dict[str, Any]] = {}
    if ordered:
        try:
            from app.core.db_pool import get_pool
            async with get_pool().acquire() as conn:
                ctx = await conn.fetchrow(
                    """
                    -- ilc:ctx
                    SELECT s.current_execution_id::text AS execution_id,
                           (SELECT g.generation_id::text FROM chat_execution_generations g
                             WHERE g.execution_id = s.current_execution_id AND g.ended_at IS NULL
                             ORDER BY g.attempt DESC LIMIT 1) AS generation_id
                      FROM chat_sessions s WHERE s.id = $1
                    """,
                    _uuid(session_id),
                )
                for item in await mark_applied(
                    conn, _uuid(session_id), ordered,
                    execution_id=ctx["execution_id"] if ctx else None,
                    generation_id=ctx["generation_id"] if ctx else None,
                ):
                    applied[item["id"]] = item
        except Exception as exc:
            logger.warning("interrupt_apply_state_failed session=%s error=%s", str(session_id)[:8], str(exc)[:160])
    for intr in interrupts or []:
        content = str(intr.get("content") or "")
        item = applied.get(str(intr.get("message_id") or ""))
        if item:
            events.append(applied_event(item, content))
        else:
            events.append({"type": "interrupt_applied", "content": content[:100]})
    return events


async def promote_working(session_id: str, execution_id: Optional[str] = None) -> None:
    try:
        from app.core.db_pool import get_pool
        async with get_pool().acquire() as conn:
            await mark_working(conn, _uuid(session_id), execution_id)
    except Exception as exc:
        logger.debug("interrupt_working_mark_failed session=%s error=%s", str(session_id)[:8], str(exc)[:160])


def applied_sse(event: dict[str, Any]) -> str:
    import json

    payload = {k: v for k, v in event.items() if v is not None or k == "content"}
    payload["type"] = "interrupt_applied"
    return f"event: interrupt_applied\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


# ── 상태 heartbeat ────────────────────────────────────────────────────────


def _digest(items: list[dict[str, Any]]) -> str:
    raw = "|".join(f"{i['id']}:{i['state']}:{i['wait_reason']}" for i in items)
    return hashlib.sha1(raw.encode()).hexdigest()[:12] if raw else ""


def should_poll_status(*, queue_has_items: bool, last_digest: str, tick: int, probe_every: int = 5) -> bool:
    """heartbeat 틱마다 DB 를 읽을지. 지시가 없는 스트림은 ``probe_every`` 틱에 한 번만 본다.

    읽는 경우: 이 프로세스 큐에 대기 지시가 있다 / 직전에 활성 상태를 보냈다(종료 상태를
    한 번 더 보내야 한다). 다른 슬롯이 DB 에만 남긴 지시는 주기 probe 로 잡는다.
    """
    return bool(queue_has_items or last_digest or (probe_every > 0 and tick % probe_every == 0))


async def poll_status_event(
    session_id: str,
    last_digest: Optional[str],
    *,
    wait_reason: Optional[str] = None,
) -> tuple[Optional[dict[str, Any]], str]:
    """진행 중인 지시가 있을 때만 ``interrupt_status`` 이벤트를 만든다.

    직전과 같으면 ``unchanged=True`` 로 본문 없이 보낸다. 모두 끝났는데 직전에 활성
    상태를 보냈다면 마지막으로 한 번 전체를 보내 클라이언트가 DONE 을 보게 한다.
    """
    from app.core.db_pool import get_pool

    async with get_pool().acquire() as conn:
        sid = _uuid(session_id)
        items = await fetch_snapshot(conn, sid, include_legacy=False)
        active = [i for i in items if i["state"] not in TERMINAL_STATES]
        if wait_reason and active:
            if await refresh_wait_reason(conn, sid, wait_reason):
                items = await fetch_snapshot(conn, sid, include_legacy=False)
                active = [i for i in items if i["state"] not in TERMINAL_STATES]
    digest = _digest(active)
    if not active and not last_digest:
        return None, ""
    if digest == (last_digest or ""):
        return {"type": "interrupt_status", "unchanged": True, "digest": digest}, digest
    return {
        "type": "interrupt_status",
        "unchanged": False,
        "digest": digest,
        "interrupts": items,
    }, digest
