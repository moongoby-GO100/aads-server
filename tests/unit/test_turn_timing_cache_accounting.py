"""턴 단위 프롬프트 캐시 누적이 실제로 쌓이는지 본다.

## 왜 필요한가

2026-09-17, `<currentTime>` 을 프롬프트 맨 앞에서 꼬리로 옮겨 캐시 프리픽스를
살렸다. 그런데 그 효과를 **증명할 지표가 없었다.**

`oauth_usage_log` 의 cli_relay 행은 30초 창 집계라 러너 트래픽이 섞인다.
창당 cache_read 가 180만 토큰 규모라 채팅 한 턴이 아끼는 1만 토큰은 0.6% —
수정 전후를 재 보니 2.36% → 2.83% 로 **오히려 올라간 것처럼** 보였다.
개선이 없어서가 아니라 그 표로는 분해가 안 되기 때문이다.

그래서 턴 단위로 센다. 모델 호출은 한 턴에 여러 번(도구 루프) 일어나므로
누적이어야 하고, 누적은 **생성기 안쪽에서 더한 값이 바깥 타이머에 보여야**
한다 — 그 두 가지가 이 파일이 지키는 것이다.
"""
from __future__ import annotations

import asyncio

import pytest

from app.services import turn_timing as tt


def test_누적은_여러_호출을_더한다():
    tt.begin_cache_accounting()
    tt.add_cache_tokens(1000, 50)
    tt.add_cache_tokens(2500, 30)
    assert tt.take_cache_tokens() == (3500, 80)


def test_턴을_새로_시작하면_0부터_센다():
    tt.begin_cache_accounting()
    tt.add_cache_tokens(999, 999)
    tt.begin_cache_accounting()
    assert tt.take_cache_tokens() == (0, 0)


def test_누산기가_없으면_조용히_버린다():
    """러너·배경 호출은 타이머 없이 모델을 부른다. 거기서 죽으면 안 된다."""
    tt._cache_acc.set(None)
    tt.add_cache_tokens(100, 100)  # 예외가 나면 실패
    assert tt.take_cache_tokens() == (0, 0)


@pytest.mark.parametrize("read,create", [(None, None), ("x", 5), (7, "y")])
def test_이상한_값에도_죽지_않는다(read, create):
    """usage 필드가 없거나 타입이 다를 수 있다. 계측이 응답을 막으면 본말전도다."""
    tt.begin_cache_accounting()
    tt.add_cache_tokens(read, create)
    got_read, got_create = tt.take_cache_tokens()
    assert isinstance(got_read, int) and isinstance(got_create, int)


def test_TurnTimer_생성이_누산기를_건다():
    tt._cache_acc.set(None)
    tt.TurnTimer("11111111-1111-1111-1111-111111111111")
    tt.add_cache_tokens(42, 7)
    assert tt.take_cache_tokens() == (42, 7)


def test_생성기_안쪽에서_더한_값이_바깥에_보인다():
    """실제 경로가 이 모양이다 — chat_service 가 타이머를 만들고,
    model_selector 의 async generator 안에서 더한다. 컨텍스트가 복사돼도
    같은 리스트 객체를 가리키므로 보여야 한다. `set()` 으로 바꾸면 안 보인다.
    """
    # 누산기는 바깥(동기 경로)에서 건다. asyncio.run 은 컨텍스트를 복사하므로
    # 안에서 begin 하면 바깥에서 안 보인다 — 실제 코드도 타이머를 먼저 만든다.
    tt.TurnTimer("22222222-2222-2222-2222-222222222222")

    async def _run():
        async def _stream():
            for _ in range(3):
                tt.add_cache_tokens(1000, 10)
                yield 1

        async for _ in _stream():
            pass

    asyncio.run(_run())
    assert tt.take_cache_tokens() == (3000, 30)
