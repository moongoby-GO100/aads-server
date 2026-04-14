"""
AADS-204: Background LLM qwen-turbo 전환 품질 테스트.

검증 항목:
1. call_background_llm() 정상 호출 (DashScope 성공)
2. call_background_llm() DashScope 실패 시 claude-haiku 폴백
3. call_llm_with_fallback(model="qwen-turbo") → _call_dashscope 라우팅
4. 10개 서비스 모듈 import 정상
5. 10개 서비스의 모델 설정값 qwen-turbo 확인
"""
import sys
import os
import pytest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))


# ── 픽스처: 연속 실패 카운터 리셋 ────────────────────────────────────────────

@pytest.fixture(autouse=True)
def reset_bg_qwen_fail_streak():
    """각 테스트 전후로 모듈 수준 qwen 실패 카운터를 리셋."""
    import app.core.anthropic_client as ac
    ac._bg_qwen_fail_streak = 0
    yield
    ac._bg_qwen_fail_streak = 0


# ── Test 1: call_background_llm() 정상 호출 ──────────────────────────────────

@pytest.mark.asyncio
async def test_call_background_llm_success():
    """call_background_llm() 정상 호출 — DashScope 응답 시뮬레이션, 반환값 비어있지 않음."""
    from app.core.anthropic_client import call_background_llm

    with patch(
        "app.core.anthropic_client._call_dashscope",
        new=AsyncMock(return_value="qwen response text"),
    ) as mock_ds:
        result = await call_background_llm("테스트 프롬프트")

    assert result, "call_background_llm() returned empty string"
    assert result == "qwen response text"
    mock_ds.assert_called_once()
    args, kwargs = mock_ds.call_args
    assert args[1] == "qwen-turbo", f"모델이 qwen-turbo가 아님: {args[1]}"


# ── Test 2: DashScope 실패 시 claude-haiku 폴백 ──────────────────────────────

@pytest.mark.asyncio
async def test_call_background_llm_dashscope_fail_haiku_fallback():
    """DashScope exception 발생 시 claude-haiku 폴백 호출됨 확인."""
    from app.core.anthropic_client import call_background_llm

    with patch(
        "app.core.anthropic_client._call_dashscope",
        new=AsyncMock(side_effect=Exception("DashScope timeout")),
    ) as mock_ds, patch(
        "app.core.anthropic_client.call_llm_with_fallback",
        new=AsyncMock(return_value="haiku fallback response"),
    ) as mock_fallback:
        result = await call_background_llm("테스트 프롬프트")

    mock_ds.assert_called_once()
    mock_fallback.assert_called_once()

    # haiku 모델로 폴백했는지 확인
    _, kwargs = mock_fallback.call_args
    assert "claude-haiku" in kwargs.get("model", ""), (
        f"폴백 모델이 haiku가 아님: {kwargs.get('model')}"
    )
    assert result == "haiku fallback response"


# ── Test 3: qwen 모델 → _call_dashscope 라우팅 확인 ──────────────────────────

@pytest.mark.asyncio
async def test_qwen_model_routes_to_dashscope():
    """call_llm_with_fallback(model="qwen-turbo") 호출 시 _call_dashscope() 경로로 라우팅됨."""
    from app.core.anthropic_client import call_llm_with_fallback

    with patch(
        "app.core.anthropic_client._call_dashscope",
        new=AsyncMock(return_value="qwen result"),
    ) as mock_ds:
        result = await call_llm_with_fallback("테스트", model="qwen-turbo")

    mock_ds.assert_called_once()
    call_args = mock_ds.call_args
    assert call_args[0][1] == "qwen-turbo", f"DashScope 모델이 qwen-turbo가 아님: {call_args[0][1]}"
    assert result == "qwen result"


# ── Test 4: 10개 서비스 모듈 import 정상 ────────────────────────────────────

def test_service_modules_import():
    """10개 서비스 모듈 import 정상 — import 에러 없음 확인."""
    modules = [
        "app.services.compaction_service",
        "app.services.experience_learner",
        "app.services.code_reviewer",
        "app.services.fact_extractor",
        "app.services.response_critic",
        "app.services.self_evaluator",
        "app.services.quality_feedback_loop",
        "app.services.smart_search_service",
        "app.services.memory_manager",
        "app.services.kakaobot_ai",
    ]
    for mod_name in modules:
        try:
            __import__(mod_name)
        except ImportError as e:
            pytest.fail(f"모듈 import 실패: {mod_name} — {e}")
        assert mod_name in sys.modules, f"{mod_name} not in sys.modules"


# ── Test 5: 서비스 모델 설정값 qwen-turbo 확인 ──────────────────────────────

def test_service_model_config_qwen():
    """_HAIKU_MODEL, _REVIEW_MODEL이 qwen-turbo이거나 call_background_llm 사용 확인."""
    import app.services.experience_learner as el
    import app.services.code_reviewer as cr
    import app.services.fact_extractor as fe
    import app.services.response_critic as rc
    import app.services.self_evaluator as se
    import app.services.quality_feedback_loop as qfl

    assert el._HAIKU_MODEL == "qwen-turbo", (
        f"experience_learner._HAIKU_MODEL={el._HAIKU_MODEL!r} (expected 'qwen-turbo')"
    )
    assert cr._REVIEW_MODEL_FALLBACK == "qwen-turbo", (
        f"code_reviewer._REVIEW_MODEL_FALLBACK={cr._REVIEW_MODEL_FALLBACK!r} (expected 'qwen-turbo')"
    )
    assert fe._HAIKU_MODEL == "qwen-turbo", (
        f"fact_extractor._HAIKU_MODEL={fe._HAIKU_MODEL!r} (expected 'qwen-turbo')"
    )
    assert rc._HAIKU_MODEL == "qwen-turbo", (
        f"response_critic._HAIKU_MODEL={rc._HAIKU_MODEL!r} (expected 'qwen-turbo')"
    )
    assert se._HAIKU_MODEL == "qwen-turbo", (
        f"self_evaluator._HAIKU_MODEL={se._HAIKU_MODEL!r} (expected 'qwen-turbo')"
    )
    assert qfl._HAIKU_MODEL == "qwen-turbo", (
        f"quality_feedback_loop._HAIKU_MODEL={qfl._HAIKU_MODEL!r} (expected 'qwen-turbo')"
    )

    # smart_search_service, compaction_service, memory_manager는 call_background_llm 또는
    # call_llm_with_fallback(model="qwen-turbo") 사용 — 소스 코드 참조로 확인
    import inspect
    import app.services.smart_search_service as sss
    src = inspect.getsource(sss)
    assert "call_background_llm" in src, "smart_search_service does not use call_background_llm"

    import app.services.compaction_service as cs
    src = inspect.getsource(cs)
    assert "qwen-turbo" in src, "compaction_service does not reference qwen-turbo"
