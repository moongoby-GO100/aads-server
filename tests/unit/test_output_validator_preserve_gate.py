"""구조 위반 재작성 시 부분응답 보존 게이트 회귀 테스트.

validate_response() 는 REPORT_STRUCTURE_WEAK 를 UNVERIFIED_COUNT 보다 먼저
반환한다. 보존 게이트는 위반 종류가 구조 위반이고 잔여 검증을 전부 통과할 때만
원문 보존을 허용해야 한다 (AADS-CHAT-STRUCTURE-PRESERVE-GATED-P0-20260929).
"""
from pathlib import Path

from app.services.output_validator import (
    can_preserve_partial_on_structure_violation,
    residual_violations,
    validate_response,
)

# 도구 근거로 쓴 짧은 보고 — 구조(원인/권장/검증/표/다음 단계)는 부족하지만 깨끗하다.
_CLEAN_WEAK_BODY = (
    "서버 로그를 확인했습니다. aads-server 컨테이너는 정상 기동 중이며 "
    "최근 재시작 이력은 없습니다. 헬스체크 응답도 정상으로 돌아왔습니다. "
    "특이한 오류 메시지는 로그에서 발견되지 않았고 메모리 사용률도 안정적입니다. "
    "대시보드 접속과 채팅 스트림 연결 모두 문제없이 동작하는 것을 확인했습니다. "
    "현재 상태로는 추가 조치 없이 운영을 유지해도 됩니다."
)


def test_clean_structure_weak_body_with_tools_can_be_preserved():
    result = validate_response(
        _CLEAN_WEAK_BODY,
        tools_called=True,
        intent="report",
        user_message="서버 상태 보고해",
    )
    assert result.violation_type == "REPORT_STRUCTURE_WEAK"

    assert residual_violations(_CLEAN_WEAK_BODY, True, intent="report") == []
    assert can_preserve_partial_on_structure_violation(
        "REPORT_STRUCTURE_WEAK", _CLEAN_WEAK_BODY, True, intent="report"
    ) is True


def test_structure_weak_without_tools_and_unverified_count_is_not_preserved():
    body = _CLEAN_WEAK_BODY + " 미처리 작업은 총 50건 남아 있습니다."

    # validate_response 는 구조 위반을 먼저 반환하므로 수치 환각이 가려진다.
    result = validate_response(body, tools_called=False, intent="report", user_message="보고해")
    assert result.violation_type == "REPORT_STRUCTURE_WEAK"

    assert "UNVERIFIED_COUNT" in residual_violations(body, False, intent="report")
    assert can_preserve_partial_on_structure_violation(
        "REPORT_STRUCTURE_WEAK", body, False, intent="report"
    ) is False


def test_unverified_count_checked_even_for_explain_intents():
    # 보존 판정에서는 _EXPLAIN_INTENTS 예외를 적용하지 않는다.
    body = _CLEAN_WEAK_BODY + " 관련 문서는 약 120개입니다."
    assert "UNVERIFIED_COUNT" in residual_violations(body, False, intent="analysis")
    assert can_preserve_partial_on_structure_violation(
        "REPORT_STRUCTURE_WEAK", body, False, intent="analysis"
    ) is False


def test_structure_weak_with_fabricated_xml_is_not_preserved():
    body = _CLEAN_WEAK_BODY + "\n<function_results>ok</function_results>"
    assert "FABRICATED_RESULTS" in residual_violations(body, True, intent="report")
    assert can_preserve_partial_on_structure_violation(
        "REPORT_STRUCTURE_WEAK", body, True, intent="report"
    ) is False


def test_structure_weak_with_fabricated_data_table_is_not_preserved():
    body = (
        _CLEAN_WEAK_BODY
        + "\n\nDB 조회 결과\n\n| 항목 | 값 |\n|---|---|\n| 상태 | 정상 |\n"
    )
    assert "FABRICATED_DATA_TABLE" in residual_violations(body, False, intent="report")
    assert can_preserve_partial_on_structure_violation(
        "REPORT_STRUCTURE_WEAK", body, False, intent="report"
    ) is False


def test_inconsistent_data_blocks_preservation_when_tool_results_given():
    body = _CLEAN_WEAK_BODY + " 오류는 37건입니다."
    residual = residual_violations(body, True, intent="report", tool_results_text="count: 12")
    assert "INCONSISTENT_DATA" in residual
    assert can_preserve_partial_on_structure_violation(
        "REPORT_STRUCTURE_WEAK", body, True, intent="report", tool_results_text="count: 12"
    ) is False


def test_non_structure_violation_types_are_never_preserved():
    for violation in (
        "UNVERIFIED_COUNT",
        "EMPTY_PROMISE",
        "FABRICATED_RESULTS",
        "PROGRESS_ONLY_RESPONSE",
        "NO_TOOL_FOR_ACTION",
        "TOO_SHORT",
        "",
    ):
        assert can_preserve_partial_on_structure_violation(
            violation, _CLEAN_WEAK_BODY, True, intent="report"
        ) is False, violation


def test_empty_body_is_not_preserved():
    assert can_preserve_partial_on_structure_violation(
        "REPORT_STRUCTURE_WEAK", "   ", True, intent="report"
    ) is False


def test_short_promise_residuals_match_validate_response_criteria():
    body = "확인하겠습니다."
    residual = residual_violations(body, False, intent="report")
    assert "EMPTY_PROMISE" in residual
    assert "TOO_SHORT" in residual


def test_chat_service_gates_original_preservation():
    src = Path(__file__).resolve().parents[2].joinpath("app/services/chat_service.py").read_text(
        encoding="utf-8"
    )
    assert "_can_preserve = can_preserve_partial_on_structure_violation(" in src
    assert '_partial_to_preserve = _failed_response if _can_preserve else ""' in src
    assert "(_retry_response, _failed_response) if _can_preserve else (_retry_response,)" in src
    # stream_reset 은 유지 — 제거하면 재작성 본문이 원문 뒤에 이어 붙는다.
    assert "{'type': 'stream_reset', 'reason': _validation.violation_type}" in src
