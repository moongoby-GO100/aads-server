"""모델 역할 조회 — async API 가 루프를 막지 않음 / 루프 스레드 동기 API 예외 / 추천 블록 역할 비활성 시 LLM 0회."""
import asyncio
import os
import sys
import time
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from app.services import model_roles as mr  # noqa: E402

BLOCK = (
    "### 💡 권장안 및 사유\n"
    "- **권장안**: 임시 로그를 정리합니다.\n"
    "- **권장 사유**: 배포 중단 리스크를 예방할 수 있습니다."
)


@pytest.fixture(autouse=True)
def _reset():
    mr.reset_for_tests()
    yield
    mr.reset_for_tests()


def _rows(role_rows):
    return [
        {"role_key": rk, "item_key": "", "model_id": mid, "priority": 0, "attrs": "{}"}
        for rk, mid in role_rows
    ]


async def test_sync_api_on_loop_thread_raises_when_not_loaded():
    with pytest.raises(mr.ModelRolesNotLoadedError):
        mr.role_scalar("bg_haiku_model")


async def test_async_api_does_not_block_loop():
    async def slow_fetch():
        await asyncio.sleep(0.3)  # 느린 DB — 비동기 대기여야 한다
        return _rows([("bg_haiku_model", "m-db")])

    ticks = []

    async def ticker():
        for _ in range(10):
            ticks.append(time.monotonic())
            await asyncio.sleep(0.02)

    with patch.object(mr, "_fetch_with_own_connection", new=slow_fetch):
        t = asyncio.create_task(ticker())
        value = await mr.role_scalar_async("bg_haiku_model")
        await t
    assert value == "m-db"
    gaps = [b - a for a, b in zip(ticks, ticks[1:])]
    assert max(gaps) < 0.15  # 루프가 0.3초 내내 멈췄다면 큰 간격이 생긴다
    assert mr.role_scalar("bg_haiku_model") == "m-db"  # 적재 후 루프 스레드 동기 읽기 허용


async def test_db_failure_falls_back_to_seed_without_raising():
    with patch.object(mr, "_fetch_with_own_connection", new=AsyncMock(side_effect=OSError("down"))):
        value = await mr.role_scalar_async("bg_haiku_model")
        assert value  # 시드 값
        assert mr.role_scalar("bg_haiku_model") == value  # 실패 확인 뒤 동기 읽기는 예외 없이 시드


def test_sync_api_off_loop_thread_still_works():
    with patch.object(mr, "_fetch_with_own_connection", new=AsyncMock(return_value=_rows([("x_role", "v1")]))):
        assert mr.role_scalar("x_role") == "v1"


async def _synth(roles_fetch):
    from app.services import chat_service

    llm = AsyncMock(return_value=BLOCK)
    with patch.object(mr, "_fetch_with_own_connection", new=roles_fetch), \
         patch("app.core.anthropic_client.call_llm_with_fallback", new=llm):
        result = await chat_service._synthesize_missing_recommendation_block("요청", "본문", intent="report")
    return llm, result


async def test_synth_role_disabled_skips_llm():
    llm, result = await _synth(AsyncMock(return_value=_rows([("bg_haiku_model", "other-model")])))
    assert llm.await_count == 0
    assert result == ""


async def test_synth_db_failure_uses_seed_model():
    llm, result = await _synth(AsyncMock(side_effect=OSError("down")))
    assert llm.await_count == 1
    assert llm.call_args.kwargs["model"] == mr.role_scalar("recommendation_synth_model")
    assert llm.call_args.kwargs["model"]
    assert "**권장안**" in result


async def test_synth_role_present_uses_that_model():
    llm, _ = await _synth(AsyncMock(return_value=_rows([("recommendation_synth_model", "db-synth-model")])))
    assert llm.await_count == 1
    assert llm.call_args.kwargs["model"] == "db-synth-model"
