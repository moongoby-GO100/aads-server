"""진행 문구만 남고 끝난 턴을 도구 없이 한 번 더 쓰게 하는 경로.

2026-09-17 실측 근거: chat_turn_executions 24시간 중단 55건 중 11건이
output_validator_progress_only_no_retry 였다. 그 11건은 대표님 화면에
본문 없이 오류만 남았다. 재시도를 막아 둔 이유는 도구 루프 재실행 비용이었으므로,
도구를 끈 재시도 한 번으로 그 비용 없이 본문을 얻는다.
"""

from pathlib import Path

from app.services.output_validator import should_retry_without_tools

_CHAT_SERVICE = Path(__file__).resolve().parents[2] / "app" / "services" / "chat_service.py"


def test_progress_only_with_tools_retries_without_tools():
    assert should_retry_without_tools("PROGRESS_ONLY_RESPONSE", True) is True


def test_progress_only_without_tools_uses_normal_retry():
    """도구를 안 부른 턴은 원래부터 재시도 경로를 탔다 — 여기서 가로채지 않는다."""
    assert should_retry_without_tools("PROGRESS_ONLY_RESPONSE", False) is False


def test_other_violations_keep_tools():
    for violation in ("FABRICATED_RESULTS", "INCONSISTENT_DATA", "REPORT_STRUCTURE_WEAK"):
        assert should_retry_without_tools(violation, True) is False


def test_chat_service_no_longer_gives_up_on_progress_only():
    """예전 경로(재시도 없이 중단 처리)가 되살아나면 11건이 그대로 재발한다."""
    source = _CHAT_SERVICE.read_text(encoding="utf-8")
    assert "output_validator_progress_only_no_retry" not in source
    assert "should_retry_without_tools" in source
    assert "tools=None if _retry_without_tools else tools_for_api" in source
