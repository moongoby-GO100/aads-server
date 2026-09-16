"""추가지시 취소 — 프로세스 큐에서 실제로 빠지는지.

2026-09-16 이전의 화면 "✕ 취소" 는 로컬 카운터만 지웠다. 서버 큐와 DB 행은
그대로 남아, 취소했다고 생각한 지시가 다음 반영 지점에서 그대로 들어갔다.
설계: docs/prd/20260916_CHAT_INTERRUPT_QUEUE_PRD.md 2.2절.
"""
from __future__ import annotations

import pytest

from app.core import interrupt_queue as q


@pytest.fixture(autouse=True)
def _clean_queues():
    q._interrupt_queues.clear()
    q._pending_interrupts.clear()
    q._streaming_sessions.clear()
    yield
    q._interrupt_queues.clear()
    q._pending_interrupts.clear()
    q._streaming_sessions.clear()


def test_cancel_all_empties_queue():
    q.push_interrupt("s1", "배포해")
    q.push_interrupt("s1", "테스트도")
    assert q.has_interrupt("s1") is True

    assert q.cancel_interrupts("s1") == 2
    assert q.has_interrupt("s1") is False
    assert q.pop_interrupts("s1") == []


def test_cancel_selected_leaves_the_rest():
    q.push_interrupt("s1", "배포해")
    q.push_interrupt("s1", "테스트도")

    assert q.cancel_interrupts("s1", ["배포해"]) == 1

    remaining = q.pop_interrupts("s1")
    assert [item["content"] for item in remaining] == ["테스트도"]


def test_cancel_matches_db_prefixed_content():
    """DB 행은 '[추가 지시] ' 접두가 붙고 큐는 원문이다. 같은 것으로 본다."""
    q.push_interrupt("s1", "배포해")

    assert q.cancel_interrupts("s1", ["[추가 지시] 배포해"]) == 1
    assert q.has_interrupt("s1") is False


def test_cancel_same_text_twice_removes_one_each():
    q.push_interrupt("s1", "배포해")
    q.push_interrupt("s1", "배포해")

    assert q.cancel_interrupts("s1", ["배포해"]) == 1
    assert len(q.pop_interrupts("s1")) == 1


def test_cancel_also_drops_pending_after_stream_end():
    """스트림이 먼저 끝나 pending 으로 옮겨진 것도 취소 대상이다."""
    q.set_streaming("s1", True)
    q.push_interrupt("s1", "배포해")
    q.set_streaming("s1", False)
    assert q.has_pending_interrupts("s1") is True

    assert q.cancel_interrupts("s1") == 1
    assert q.has_pending_interrupts("s1") is False


def test_cancel_unknown_session_is_noop():
    assert q.cancel_interrupts("nobody") == 0
    assert q.cancel_interrupts("", ["x"]) == 0
