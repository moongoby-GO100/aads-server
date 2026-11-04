"""AI 리뷰 반려 사유가 빈칸으로 남는 결함(runner-ac83b1d3 리뷰 지적 3건) 회귀 테스트.

scripts/pipeline-runner.sh 의 parse_review_issues() 를 그대로 잘라 bash 로 실행한다.
  - issues: 배열 / JSON 인코딩 문자열 / "[]" / null / 평문
  - feedback: 객체 / JSON 인코딩 문자열 / 평문
  - 사유 안의 따옴표는 보존
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = ROOT / "scripts" / "pipeline-runner.sh"
SCRIPT = SCRIPT_PATH.read_text(encoding="utf-8")

pytestmark = pytest.mark.skipif(shutil.which("jq") is None, reason="jq 미설치")


def _function(name: str) -> str:
    start = SCRIPT.index(f"{name}() {{")
    return SCRIPT[start : SCRIPT.index("\n}\n", start) + 3]


def _parse(response) -> str:
    payload = response if isinstance(response, str) else json.dumps(response, ensure_ascii=False)
    harness = _function("parse_review_issues") + '\nout=$(printf "%s" "$PAYLOAD" | parse_review_issues)\nprintf "%s" "$out"\n'
    res = subprocess.run(
        ["bash", "-c", harness],
        env={"PAYLOAD": payload, "PATH": "/usr/local/bin:/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert res.returncode == 0, res.stderr
    return res.stdout


def test_issues_array_joined():
    assert _parse({"issues": ["a 실패", "b 누락"]}) == "a 실패; b 누락"


def test_issues_json_encoded_array_string_is_decoded():
    out = _parse({"issues": json.dumps(["첫째", "둘째"], ensure_ascii=False)})
    assert out == "첫째; 둘째"


def test_issues_empty_array_string_falls_back_to_feedback_summary():
    out = _parse({"issues": "[]", "feedback": {"summary": "요약 사유"}})
    assert out == "요약 사유"


def test_issues_null_with_json_string_feedback_uses_summary():
    out = _parse({"issues": None, "feedback": json.dumps({"summary": "JSON 문자열 요약"}, ensure_ascii=False)})
    assert out == "JSON 문자열 요약"


def test_quotes_inside_reason_are_preserved():
    reason = '`"status"` 필드가 "done" 이어야 한다'
    assert _parse({"issues": [reason]}) == reason
    assert _parse({"issues": json.dumps([reason], ensure_ascii=False)}) == reason
    assert _parse({"issues": reason}) == reason


def test_empty_array_and_no_feedback_gives_empty():
    assert _parse({"issues": []}) == ""
    assert _parse({"issues": "[]"}) == ""


def test_plain_string_feedback_is_used_as_summary():
    assert _parse({"issues": None, "feedback": "그냥 문자열 사유"}) == "그냥 문자열 사유"


def test_non_json_response_yields_empty_without_failing():
    assert _parse("not json at all") == ""


def test_runner_uses_parser_and_copies_stay_identical():
    assert "review_issues=$(printf '%s' \"$review_response\" | parse_review_issues)" in SCRIPT
    assert "jq -r '.issues | join(" not in SCRIPT
    local_copy = SCRIPT_PATH.with_name("pipeline-runner.sh.local").read_text(encoding="utf-8")
    assert local_copy == SCRIPT
