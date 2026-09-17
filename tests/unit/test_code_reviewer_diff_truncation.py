"""AADS-REVIEWER-DIFF-TRUNCATION-P0 — 리뷰 diff 절단 동작 고정.

종전에는 `review_code_diff` 가 diff 앞 10,000자만 리뷰어에게 넘기고 나머지를
버렸다. 리뷰어는 잘린 뒷부분을 보지 못해 "diff 가 잘려 확인 불가" 를 근거로
반려했고, 2026-09-17 실측에서 runner 리뷰 287건 중 196건(68%)이 절단 상태로
심사돼 승인율이 41.8% → 22.4% 로 떨어졌다.

상한을 REVIEW_DIFF_MAX_CHARS(기본 200,000)로 올리고, 그래도 넘치면 앞부분만
남기는 대신 ①파일별 stat 요약 ②앞 60% ③뒤 40% 를 남긴다. 이 파일은 그
세 갈래를 고정한다.
"""

import app.services.code_reviewer as cr


def _diff_of_length(total: int, marker_head: str, marker_tail: str) -> str:
    """앞뒤에 식별 마커가 박힌 길이 `total` 의 가짜 diff 를 만든다."""
    body_len = total - len(marker_head) - len(marker_tail)
    assert body_len > 0
    return marker_head + ("x" * body_len) + marker_tail


def test_diff_under_limit_is_passed_through_whole():
    """상한 이하의 diff 는 한 글자도 잘리지 않는다."""
    diff = "diff --git a/app/x.py b/app/x.py\n" + ("+새 줄\n" * 2000)
    assert len(diff) <= cr.REVIEW_DIFF_MAX_CHARS

    out, was_truncated = cr._truncate_diff_for_review(diff)

    assert was_truncated is False
    assert out == diff


def test_diff_at_150k_is_not_truncated():
    """구 상한(10KB)이라면 잘렸을 150,000자도 현 상한에서는 전량 전달된다."""
    diff = _diff_of_length(150_000, "HEAD_MARKER\n", "\nTAIL_MARKER")

    out, was_truncated = cr._truncate_diff_for_review(diff)

    assert was_truncated is False
    assert out == diff
    assert "TAIL_MARKER" in out


def test_diff_over_limit_keeps_head_and_tail_with_stat_summary():
    """상한 초과 시 앞뒤가 모두 남고, stat 요약과 생략 마커가 붙는다."""
    head = "diff --git a/app/head.py b/app/head.py\n+HEAD_MARKER\n"
    tail = "\n+TAIL_MARKER\n"
    diff = _diff_of_length(cr.REVIEW_DIFF_MAX_CHARS + 50_000, head, tail)

    out, was_truncated = cr._truncate_diff_for_review(diff)

    assert was_truncated is True
    # 앞부분만 남기던 과거 동작의 회귀를 막는다 — 뒤가 반드시 남아야 한다.
    assert "HEAD_MARKER" in out
    assert "TAIL_MARKER" in out
    assert "생략" in out
    # 절단 사실이 '결함' 으로 읽히지 않도록 마커가 명시한다.
    assert "'확인 불가'이지 '결함'이 아니다" in out
    # 전체 규모를 항상 보여주는 stat 요약이 맨 앞에 붙는다.
    assert out.startswith("파일별 변경 요약") or out.startswith("전체 변경:")


def test_stat_summary_lists_each_changed_file():
    """stat 요약은 파일별 +/- 라인 수를 집계한다."""
    diff = (
        "diff --git a/app/a.py b/app/a.py\n"
        "--- a/app/a.py\n"
        "+++ b/app/a.py\n"
        "+한 줄 추가\n"
        "+두 줄 추가\n"
        "-한 줄 삭제\n"
        "diff --git a/app/b.py b/app/b.py\n"
        "--- a/app/b.py\n"
        "+++ b/app/b.py\n"
        "+한 줄 추가\n"
    )

    summary = cr._diff_stat_summary(diff)

    assert "app/a.py" in summary
    assert "app/b.py" in summary
    assert "+2 -1" in summary


def test_empty_diff_is_safe():
    """빈 diff 에서도 예외 없이 빈 문자열을 돌려준다."""
    out, was_truncated = cr._truncate_diff_for_review("")

    assert out == ""
    assert was_truncated is False
