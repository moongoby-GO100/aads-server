"""배경 LLM 1순위 죽은 경로 제거 (AADS-BG-LLM-DEAD-PRIMARY-REMOVAL-20261002).

1. 기본 1순위가 claude-haiku → LiteLLM/DashScope 를 건드리지 않고 call_llm_with_fallback 1회, 성공 로그
2. env 로 groq 지정 + 연속 3회 실패 → 4번째 호출은 1순위 건너뜀, 600초 뒤 1회 재시도, 성공 시 리셋
"""
import os
import sys
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

import app.core.anthropic_client as ac  # noqa: E402

HAIKU = "claude-haiku-4-5-20251001"


@pytest.fixture(autouse=True)
def reset_breaker():
    ac._bg_primary_fail_streak = 0
    ac._bg_primary_skip_until = 0.0
    yield
    ac._bg_primary_fail_streak = 0
    ac._bg_primary_skip_until = 0.0


def test_default_primary_is_haiku():
    if "LLM_BG_PRIMARY_MODEL" not in os.environ:
        assert ac._BG_PRIMARY_MODEL == HAIKU


async def test_claude_primary_skips_litellm_and_logs_success():
    with patch.object(ac, "_BG_PRIMARY_MODEL", HAIKU), \
         patch.object(ac, "_call_litellm", new=AsyncMock(return_value="x")) as ll, \
         patch.object(ac, "_call_dashscope", new=AsyncMock(return_value="x")) as ds, \
         patch.object(ac, "call_llm_with_fallback", new=AsyncMock(return_value="haiku ok")) as fb, \
         patch.object(ac, "_bg_llm_log", new=AsyncMock()) as log:
        result = await ac.call_background_llm("p")

    assert result == "haiku ok"
    ll.assert_not_called()
    ds.assert_not_called()
    fb.assert_called_once()
    assert fb.call_args.kwargs["model"] == HAIKU
    log.assert_awaited_once()
    assert log.call_args.args == ("background", HAIKU, True)


async def test_claude_primary_failure_logs_bg_primary_failed_without_second_call():
    with patch.object(ac, "_BG_PRIMARY_MODEL", HAIKU), \
         patch.object(ac, "call_llm_with_fallback", new=AsyncMock(return_value=None)) as fb, \
         patch.object(ac, "_bg_llm_log", new=AsyncMock()) as log:
        result = await ac.call_background_llm("p")

    assert result == ""
    fb.assert_called_once()
    log.assert_awaited_once()
    assert log.call_args.args == ("background", HAIKU, False)
    assert log.call_args.kwargs["error_code"] == "bg_primary_failed"


async def test_non_claude_primary_breaker_opens_after_three_failures_and_recovers():
    clock = {"t": 1000.0}
    ll = AsyncMock(side_effect=Exception("groq down"))
    fb = AsyncMock(return_value="haiku fallback")
    with patch.object(ac, "_BG_PRIMARY_MODEL", "groq-gpt-oss-120b"), \
         patch.object(ac, "_call_litellm", new=ll), \
         patch.object(ac, "call_llm_with_fallback", new=fb), \
         patch.object(ac, "_bg_llm_log", new=AsyncMock()), \
         patch.object(ac, "_notify_bg_llm_alert", new=AsyncMock()) as alert, \
         patch.object(ac.time, "monotonic", side_effect=lambda: clock["t"]):
        for _ in range(3):
            assert await ac.call_background_llm("p") == "haiku fallback"
        assert ll.await_count == 3
        alert.assert_awaited_once()

        # 4번째: 쿨다운 중이라 1순위를 건너뛰고 haiku 직행
        assert await ac.call_background_llm("p") == "haiku fallback"
        assert ll.await_count == 3
        assert fb.await_count == 4
        assert fb.call_args.kwargs["model"] == HAIKU

        # 599초 뒤에도 아직 건너뜀
        clock["t"] += 599
        await ac.call_background_llm("p")
        assert ll.await_count == 3

        # 600초 경과 → 1회 재시도, 실패하면 다시 오픈
        clock["t"] += 1
        await ac.call_background_llm("p")
        assert ll.await_count == 4
        await ac.call_background_llm("p")
        assert ll.await_count == 4

        # 다시 쿨다운 경과 → 재시도 성공 시 streak 리셋
        clock["t"] += 600
        ll.side_effect = None
        ll.return_value = "groq ok"
        assert await ac.call_background_llm("p") == "groq ok"
        assert ac._bg_primary_fail_streak == 0
        assert await ac.call_background_llm("p") == "groq ok"
        assert ll.await_count == 6
