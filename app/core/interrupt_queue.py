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


def _cancel_key(text: Any) -> str:
    """취소 대조용 정규화 — DB 행은 '[추가 지시] ' 접두가 붙고 큐는 원문이다."""
    s = str(text or "").strip()
    if s.startswith("[추가 지시]"):
        s = s[len("[추가 지시]"):].strip()
    return s


def cancel_interrupts(session_id: str, contents: list[str] | None = None) -> int:
    """아직 소비되지 않은 인터럽트를 프로세스 큐에서 제거한다.

    ``contents`` 가 None 이면 그 세션의 대기 전부를 버린다. 지정하면 같은
    문구 1건씩만 제거한다(같은 문구를 두 번 보냈고 한 건만 취소하는 경우).

    DB 행 무효화는 호출자(라우터)가 한다. 여기는 메모리만 책임진다 —
    2026-09-16 실측 기준 화면의 "취소"는 로컬 카운터만 지웠고, 프로세스
    큐와 DB 행은 그대로 남아 다음 반영 지점에서 그대로 들어갔다.
    """
    if not session_id:
        return 0

    removed = 0
    targets: list[str] | None = None
    if contents is not None:
        targets = [_cancel_key(c) for c in contents if _cancel_key(c)]
        if not targets:
            return 0

    for store in (_interrupt_queues, _pending_interrupts):
        items = store.get(session_id)
        if not items:
            continue
        if targets is None:
            removed += len(items)
            store.pop(session_id, None)
            continue

        kept: list[dict[str, Any]] = []
        remaining = list(targets)
        for item in items:
            key = _cancel_key(item.get("content"))
            if key in remaining:
                remaining.remove(key)
                removed += 1
                continue
            kept.append(item)
        if kept:
            store[session_id] = kept
        else:
            store.pop(session_id, None)

    if removed:
        logger.info("interrupts_cancelled session_id=%s count=%d", session_id, removed)
    return removed
