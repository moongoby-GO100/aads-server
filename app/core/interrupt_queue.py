"""
CEO 인터럽트 큐 — 세션별 중간 메시지 보관 (텍스트 + 첨부파일)
AADS-FIX: 인터럽트 메시지 DB 저장 + 미소비 인터럽트 보존
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# 세션별 인터럽트 메시지 큐 — dict: {content: str, attachments: list[dict]}
_interrupt_queues: dict[str, list[dict[str, Any]]] = {}
# 세션별 스트리밍 상태
_streaming_sessions: set[str] = set()
# 스트리밍 종료 시 미소비된 인터럽트 → 다음 턴에 주입
_pending_interrupts: dict[str, list[dict[str, Any]]] = {}


def push_interrupt(session_id: str, message: str, attachments: list[dict] | None = None) -> None:
    """CEO 중간 메시지를 큐에 추가 (텍스트 + 선택적 첨부파일)"""
    if session_id not in _interrupt_queues:
        _interrupt_queues[session_id] = []
    _interrupt_queues[session_id].append({
        "content": message,
        "attachments": attachments or [],
    })
    logger.info("interrupt_pushed session_id=%s message=%s attachments=%d",
                session_id, message[:50], len(attachments or []))


def pop_interrupts(session_id: str) -> list[dict[str, Any]]:
    """큐에 쌓인 메시지 전부 꺼내기 (꺼내면 큐 비움). 각 항목: {content, attachments}"""
    msgs = _interrupt_queues.pop(session_id, [])
    if msgs:
        logger.info("interrupts_popped session_id=%s count=%d", session_id, len(msgs))
    return msgs


def has_interrupt(session_id: str) -> bool:
    """큐에 메시지가 있는지 확인"""
    return bool(_interrupt_queues.get(session_id))


def set_streaming(session_id: str, value: bool) -> None:
    """스트리밍 상태 설정"""
    if value:
        _streaming_sessions.add(session_id)
    else:
        _streaming_sessions.discard(session_id)
        # 미소비 인터럽트가 있으면 _pending_interrupts로 이동 (삭제하지 않음)
        remaining = _interrupt_queues.pop(session_id, None)
        if remaining:
            logger.warning(
                "unconsumed_interrupts session_id=%s count=%d — moved to pending",
                session_id, len(remaining),
            )
            _pending_interrupts[session_id] = remaining


def is_streaming(session_id: str) -> bool:
    """세션이 현재 스트리밍 중인지 확인"""
    return session_id in _streaming_sessions


def pop_pending_interrupts(session_id: str) -> list[dict[str, Any]]:
    """스트리밍 종료 후 미소비된 인터럽트를 꺼냄 (다음 턴 시작 시 호출)"""
    msgs = _pending_interrupts.pop(session_id, [])
    if msgs:
        logger.info("pending_interrupts_popped session_id=%s count=%d", session_id, len(msgs))
    return msgs


def has_pending_interrupts(session_id: str) -> bool:
    """미소비 인터럽트가 있는지 확인"""
    return bool(_pending_interrupts.get(session_id))
