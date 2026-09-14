"""와치독이 살아 있는 턴을 죽이지 않게 한다.

2026-09-15 08:42, #310 주도 세션이 잘렸다. 종료 기록은
`age=1348s idle=31s timeout=1200s content_len=0 tool_count=4` 였다 —
**31초 전까지 움직이던 턴**이다. 두 가지가 겹쳤다.

1. 살려두는 예외 조건이 "눈에 보이는 본문이 있을 것" 이었다. 계속 도구만
   돌던 턴은 본문이 없어 예외를 못 받았다.
2. 배포 전환으로 여섯 번 갈아탔는데(retry_count=5) 나이 시계가 첫 시도
   시각 그대로라 누적 나이가 상한을 넘었다.
"""
import inspect

import pytest

from app.services import chat_service as cs


def _body(fn) -> str:
    return "\n".join(
        line for line in inspect.getsource(fn).splitlines()
        if not line.strip().startswith("#")
    )


def test_recent_activity_counts_as_alive():
    assert cs._stream_looks_alive(31) is True
    assert cs._stream_looks_alive(0) is True
    assert cs._stream_looks_alive(cs._STREAM_LIVENESS_IDLE_SEC) is False
    assert cs._stream_looks_alive(None) is False


def test_absolute_ceiling_exists_and_exceeds_hard_timeout():
    """살려두기로 한 이상, 진짜 폭주를 잡을 마지막 선이 있어야 한다."""
    ceiling = cs.get_stream_absolute_ceiling_sec()
    assert ceiling >= 1200
    assert ceiling > max(cs._MODEL_TIMEOUT_OVERRIDES.values())


def test_watchdog_measures_the_current_attempt_not_the_whole_turn():
    body = _body(cs.cleanup_overlong_running_executions)
    assert "attempt_age_seconds" in body
    assert "attempt_started_at" in body
    # 전체 나이는 절대 상한에만 쓴다.
    assert "total_age < ceiling" in body


def test_attempt_clock_restarts_on_new_attempt():
    """실제 모델 호출 직전과 슬롯 인수 시점 — 두 곳 모두에서 다시 건다."""
    src = inspect.getsource(cs)
    assert src.count("attempt_started_at = NOW()") >= 2


def test_liveness_defer_does_not_require_content():
    body = _body(cs.cleanup_overlong_running_executions)
    # 본문이 있을 때(content)와 없어도 살아 있을 때(alive) 둘 다 유예한다.
    assert 'defer_reason = "content"' in body
    assert 'defer_reason = "alive"' in body
