"""Layer 3 히스토리의 메시지당 상한 회귀 테스트.

2026-09-14 실측: 세션 5090a247 의 컨텍스트 54건이 95,291 토큰이었다.
원인은 도구 결과가 아니라 **긴 산문 답변** 한 건(58,906자, 전체의 34%)이었다.
기존 Deep Compression 은 2×관측윈도우(=40턴) 이전에만 걸려서 최근 40건은
아무리 길어도 원문 그대로 들어갔다.

상한 적용 후 32,377 토큰(66% 감축). 이 테스트는 두 가지를 지킨다.

1. 상한이 실제로 걸린다 — 상한이 도로 풀리면 컨텍스트가 다시 부풀고,
   첫 응답 타임아웃으로만 증상이 보여서 원인 추적이 오래 걸린다.
2. **최근 구간은 손대지 않는다** — "그거 다시 해줘" 같은 지시대명사가
   직전 맥락을 가리키므로, 최근 턴을 자르면 대화 자체가 깨진다.
"""
import pytest

from app.services import context_builder as cb


def _msgs(n: int, size: int):
    return [
        {"role": "assistant" if i % 2 else "user", "content": f"{i:03d}" + "가" * size}
        for i in range(n)
    ]


def test_long_old_message_is_capped():
    msgs = _msgs(30, 50_000)
    built = cb._build_layer3_messages(msgs)
    oldest = built[0]["content"]
    assert len(oldest) < 50_000, "오래된 긴 메시지에 상한이 걸리지 않았다"
    assert "생략" in oldest


def test_recent_messages_stay_verbatim():
    """최근 _VERBATIM_RECENT 건은 길어도 원문이어야 한다."""
    size = 50_000
    msgs = _msgs(30, size)
    built = cb._build_layer3_messages(msgs)
    for offset in range(1, cb._VERBATIM_RECENT + 1):
        content = built[-offset]["content"]
        assert content == msgs[-offset]["content"], (
            f"최근 {offset}번째 메시지가 잘렸다 — 지시대명사 맥락이 깨진다"
        )


def test_short_messages_untouched():
    msgs = _msgs(30, 10)
    built = cb._build_layer3_messages(msgs)
    assert [m["content"] for m in built] == [m["content"] for m in msgs]


def test_reduction_on_measured_shape():
    """실측 분포를 재현한다 — 한 건이 전체의 3분의 1을 먹는 모양.

    균일한 길이로는 이 문제가 재현되지 않는다. 5090a247 의 실제 분포는
    58,906자 / 16,354자 / 9,492자 + 나머지 짧은 메시지였고, 감축의 대부분은
    그 상위 몇 건에서 나왔다. 상한이 풀리면 이 테스트만 깨진다.
    """
    msgs = _msgs(54, 800)
    msgs[10]["content"] = "가" * 58_906
    msgs[20]["content"] = "나" * 16_354
    msgs[30]["content"] = "다" * 9_492
    before = sum(len(m["content"]) for m in msgs)
    after = sum(len(m["content"]) for m in cb._build_layer3_messages(msgs))
    assert after < before * 0.5, f"감축이 부족하다: {before:,} → {after:,}"


def test_empty_and_nonstring_content_do_not_raise():
    built = cb._build_layer3_messages(
        [{"role": "user", "content": ""}, {"role": "assistant", "content": None}]
    )
    assert len(built) == 2
