"""
CEO 인터럽트 큐 — 세션별 중간 메시지 보관
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# 세션별 인터럽트 메시지 큐
_interrupt_queues: dict[str, list[str]] = {}
# 세션별 스트리밍 상태
_streaming_sessions: set[str] = set()


def push_interrupt(session_id: str, message: str) -> None:
    """CEO 중간 메시지를 큐에 추가"""
    if session_id not in _interrupt_queues:
        _interrupt_queues[session_id] = []
    _interrupt_queues[session_id].append(message)
    logger.info("interrupt_pushed session_id=%s message=%s", session_id, message[:50])


def pop_interrupts(session_id: str) -> list[str]:
    """큐에 쌓인 메시지 전부 꺼내기 (꺼내면 큐 비움)"""
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
        # 스트리밍 종료 시 인터럽트 큐도 정리
        _interrupt_queues.pop(session_id, None)


def is_streaming(session_id: str) -> bool:
    """세션이 현재 스트리밍 중인지 확인"""
    return session_id in _streaming_sessions
