"""
실시간 작업 로그 — Pipeline B/C 진행 상황을 DB 저장 + SSE 브로드캐스트.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ─── In-memory SSE 구독자 관리 ───────────────────────────────────────────────
# task_id → [asyncio.Queue, ...]
_subscribers: Dict[str, List[asyncio.Queue]] = defaultdict(list)

# task_id → 마지막 로그 시간 (stall 감지용)
_last_log_time: Dict[str, float] = {}


def subscribe(task_id: str) -> asyncio.Queue:
    """SSE 구독 시작. 반환된 Queue에서 이벤트를 꺼내면 됨."""
    q: asyncio.Queue = asyncio.Queue(maxsize=200)
    _subscribers[task_id].append(q)
    return q


def unsubscribe(task_id: str, q: asyncio.Queue) -> None:
    """SSE 구독 해제."""
    if task_id in _subscribers:
        try:
            _subscribers[task_id].remove(q)
        except ValueError:
            pass
        if not _subscribers[task_id]:
            del _subscribers[task_id]


_seq_numbers: Dict[str, int] = defaultdict(int)


def _broadcast(task_id: str, event: dict) -> None:
    """모든 구독자에게 이벤트 전달 (시퀀스 번호 포함)."""
    seq = _seq_numbers[task_id]
    _seq_numbers[task_id] = seq + 1
    event["seq"] = seq
    for q in _subscribers.get(task_id, []):
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            logger.warning(f"[TaskLog] Queue full task={task_id} seq={seq} dropped")


async def emit_task_log(
    task_id: str,
    log_type: str,
    content: str,
    phase: str = "",
    metadata: Optional[dict] = None,
) -> None:
    """
    작업 로그 발행: DB INSERT + SSE 브로드캐스트.
    fire-and-forget으로 호출해도 안전 (실패 시 경고 로그만).
    """
    now = datetime.now(timezone.utc)
    _last_log_time[task_id] = time.monotonic()

    event = {
        "task_id": task_id,
        "log_type": log_type,
        "content": content[:2000],
        "phase": phase,
        "timestamp": now.isoformat(),
        "metadata": metadata or {},
    }

    # SSE 브로드캐스트 (즉시)
    _broadcast(task_id, {"type": "task_log", **event})

    # DB 저장 (fire-and-forget)
    try:
        from app.core.db_pool import get_pool
        pool = get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO task_logs (task_id, log_type, content, phase, metadata)
                VALUES ($1, $2, $3, $4, $5::jsonb)
                """,
                task_id, log_type, content[:2000], phase,
                json.dumps(metadata or {}),
            )
    except Exception as e:
        logger.warning(f"[TaskLogger] DB 저장 실패 task={task_id}: {e}")


async def emit_task_started(
    task_id: str,
    project: str,
    title: str,
    pipeline: str,
    session_id: str = "",
) -> None:
    """작업 시작 이벤트 브로드캐스트."""
    event = {
        "type": "task_started",
        "task_id": task_id,
        "project": project,
        "title": title[:200],
        "pipeline": pipeline,
        "session_id": session_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    _broadcast(task_id, event)


async def emit_task_completed(
    task_id: str,
    status: str,
    summary: str = "",
) -> None:
    """작업 완료 이벤트 브로드캐스트."""
    event = {
        "type": "task_completed",
        "task_id": task_id,
        "status": status,
        "summary": summary[:500],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    _broadcast(task_id, event)
    # 정리
    _last_log_time.pop(task_id, None)


def get_stalled_tasks(threshold_sec: float = 300) -> List[str]:
    """마지막 로그가 threshold 초 이상 지난 작업 목록."""
    now = time.monotonic()
    return [
        tid for tid, t in _last_log_time.items()
        if now - t > threshold_sec
    ]


async def gc_old_logs(days: int = 7) -> int:
    """오래된 로그 삭제."""
    try:
        from app.core.db_pool import get_pool
        pool = get_pool()
        async with pool.acquire() as conn:
            result = await conn.execute(
                f"DELETE FROM task_logs WHERE created_at < NOW() - interval '{days} days'"
            )
            count = int(result.split()[-1]) if result else 0
            logger.info(f"[TaskLogger] GC: {count}건 삭제 ({days}일 이상)")
            return count
    except Exception as e:
        logger.warning(f"[TaskLogger] GC 실패: {e}")
        return 0
