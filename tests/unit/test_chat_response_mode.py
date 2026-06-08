from app.services.chat_service import (
    _normalize_response_mode,
    _response_mode_prompt_block,
)


def test_response_mode_defaults_to_quality_for_invalid_values():
    assert _normalize_response_mode(None) == "quality"
    assert _normalize_response_mode("") == "quality"
    assert _normalize_response_mode("unknown") == "quality"


def test_response_mode_accepts_fast_and_quality_case_insensitive():
    assert _normalize_response_mode("fast") == "fast"
    assert _normalize_response_mode("FAST") == "fast"
    assert _normalize_response_mode("quality") == "quality"


def test_response_mode_prompt_blocks_have_distinct_contracts():
    fast_block = _response_mode_prompt_block("fast")
    quality_block = _response_mode_prompt_block("quality")

    assert "응답 모드: fast" in fast_block
    assert "도구 사용을 최소화" in fast_block
    assert "응답 모드: quality" in quality_block
    assert "최종 완료보고 조건" in quality_block
