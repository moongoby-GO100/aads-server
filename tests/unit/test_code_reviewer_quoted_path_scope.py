"""AADS-REVIEW-INVALID-INPUT-DEPLOY-ONLY-20261009 — 따옴표 경로 diff 의 변경파일 추출 회귀.

runner-6b2419a8 / runner-dfcfc106 / runner-ef05b515 는 diff 가 비어 있지 않은 정상
보고서 diff 였다. 헤더가 `diff --git "a/..\\353.." "b/..\\353.."` 형태여서 옛
_DIFF_HEADER_RE 가 거부했고 INVALID_REVIEW_INPUT(0.1)이 났다(1483307e 가 정규식 수정).
같은 뿌리의 잔여 결함: _extract_changed_files 가 따옴표 헤더를 못 읽고, 러너 셸의
job_diff_changed_files 가 따옴표 헤더 줄 원문을 파일명으로 내보낸다.
"""

import app.services.code_reviewer as cr

KOREAN = "docs/reports/20261007_AADS_문서클릭열람_운영배포검증.md"


def _quote(path: str) -> str:
    return '"' + "".join(
        f"\\{b:03o}" if b >= 0x80 else chr(b) for b in path.encode("utf-8")
    ) + '"'


def _diff_for(path: str) -> str:
    q = _quote(path)
    return (
        f"diff --git {q.replace(chr(34), chr(34) + 'a/', 1)} {q.replace(chr(34), chr(34) + 'b/', 1)}\n"
        "new file mode 100644\n--- /dev/null\n"
        f"+++ {q.replace(chr(34), chr(34) + 'b/', 1)}\n@@ -0,0 +1 @@\n+hello\n"
    )


def test_unquote_git_path_restores_korean():
    assert cr._unquote_git_path(_quote(KOREAN).strip('"')) == KOREAN


def test_extract_changed_files_reads_quoted_header():
    assert cr._extract_changed_files(_diff_for(KOREAN)) == [KOREAN]


def test_extract_changed_files_plain_unchanged():
    plain = "diff --git a/app/x.py b/app/x.py\n--- a/app/x.py\n+++ b/app/x.py\n@@ -1 +1 @@\n-a\n+b\n"
    assert cr._extract_changed_files(plain) == ["app/x.py"]


def test_normalize_files_changed_repairs_runner_header_line():
    header = _diff_for(KOREAN).splitlines()[0]
    assert cr._normalize_files_changed([header, "app/x.py", "app/x.py", ""]) == [KOREAN, "app/x.py"]


def test_quoted_korean_report_diff_passes_precheck():
    assert cr._precheck_review_input(_diff_for(KOREAN)) is None


def test_empty_diff_is_skip_not_invalid_input():
    verdict = cr._precheck_review_input("")
    assert verdict.verdict == "SKIP"
    assert verdict.flag_category != "INVALID_REVIEW_INPUT"
