from app.services.self_evaluator import (
    _build_improvement_hint,
    _coerce_meta_value,
    _detect_failure_type,
)


def test_detect_failure_type_returns_none_without_signal():
    assert _detect_failure_type("안녕하세요", "정상 응답입니다") is None


def test_detect_failure_type_prioritizes_explicit_instruction_violation():
    failure_type = _detect_failure_type(
        "반드시 표로 보고하라고 했잖아",
        "도구 오류도 있었습니다",
    )

    assert failure_type == "지시_위반"


def test_build_improvement_hint_is_rule_based_and_specific():
    hint = _build_improvement_hint(
        "도구_오류",
        "서버 확인해",
        "도구 호출 실패했습니다",
        0.3,
    )

    assert "대안 도구" in hint
    assert "폴백 검증" in hint


def test_coerce_meta_value_accepts_json_string():
    value = _coerce_meta_value('{"fail_count": 2, "success_count": 1}')

    assert value["fail_count"] == 2
    assert value["success_count"] == 1

