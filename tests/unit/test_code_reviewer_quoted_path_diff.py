"""AADS-REVIEW-QUOTED-PATH-DIFF-20261007 — 따옴표 경로 diff 헤더 인식 고정.

git 은 core.quotePath 기본값에서 비ASCII 경로를 `"a/...\\353..."` 처럼 따옴표+8진수로
출력한다. 종전 정규식은 `a/` 앞의 `"` 를 허용하지 않아 한글 파일명 diff 를
INVALID_REVIEW_INPUT 으로 오판했다 (runner-ef05b515).
"""

import time

import app.services.code_reviewer as cr

QUOTED = (
    'diff --git "a/docs/prd/20261007_NTV2_\\353\\211\\264\\355\\206\\241 PRD v3.md" '
    '"b/docs/prd/20261007_NTV2_\\353\\211\\264\\355\\206\\241 PRD v3.md"\n'
    "new file mode 100644\n--- /dev/null\n+++ \"b/docs/prd/20261007_NTV2_\\353\\211\\264\\355\\206\\241 PRD v3.md\"\n"
    "@@ -0,0 +1 @@\n+hello\n"
)
PLAIN = "diff --git a/app/x.py b/app/x.py\n--- a/app/x.py\n+++ b/app/x.py\n@@ -1 +1 @@\n-a\n+b\n"


def test_quoted_octal_korean_path_is_a_diff():
    assert cr._looks_like_git_diff(QUOTED) is True


def test_plain_diff_still_a_diff():
    assert cr._looks_like_git_diff(PLAIN) is True


def test_non_diff_text_is_not_a_diff():
    assert cr._looks_like_git_diff("그냥 설명 텍스트입니다.\nno diff here") is False
    assert cr._looks_like_git_diff("") is False


def test_stat_summary_extracts_quoted_path():
    summary = cr._diff_stat_summary(QUOTED + PLAIN)
    assert 'PRD v3.md' in summary
    assert "app/x.py | +1 -1" in summary
    assert "?" not in summary


def test_header_regex_is_not_catastrophic():
    evil = 'diff --git "a/' + ("a b " * 20000) + "\n"
    start = time.monotonic()
    cr._looks_like_git_diff(evil)
    assert time.monotonic() - start < 2.0
