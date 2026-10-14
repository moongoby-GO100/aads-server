"""보존 하드 게이트가 러너 공용 루트 RESULT.md 재작성을 코드 삭제로 센 오탐 회귀 고정.

2026-10-14 runner-5d5fe17b(RESULT.md +77/-766), runner-f767d7f0(+54/-796) 가
코드 삭제가 4줄뿐인데 0.39 PRESERVATION_HARD_GATE 로 반려됐다.

고정하는 것:
(a) 루트 RESULT.md 만 대량 삭제 + 코드 소량 추가 → 통과
(b) 코드 파일 대량 삭제 → 차단
(c) docs/x/RESULT.md 대량 삭제 → 차단(루트 한 경로만 제외)
(d) 루트 RESULT.md 대량 삭제 + 코드 대량 삭제 → 차단
"""

import app.services.code_reviewer as cr

CODE_PATH = "app/services/foo.py"


def _modify_diff(path: str, added: int, removed: int, tag: str = "x") -> str:
    lines = [
        f"diff --git a/{path} b/{path}",
        "index 0000001..0000002 100644",
        f"--- a/{path}",
        f"+++ b/{path}",
        f"@@ -1,{removed} +1,{added} @@",
    ]
    lines += [f"-{tag}_old_{i} = {i}" for i in range(removed)]
    lines += [f"+{tag}_new_{i} = {i}" for i in range(added)]
    return "\n".join(lines) + "\n"


def _gate(diff: str, files=None, notes=None):
    return cr._precheck_preservation_gate(diff, "", files, notes)


def test_old_behaviour_counts_result_md_as_deletion():
    diff = _modify_diff("RESULT.md", 54, 796) + _modify_diff(CODE_PATH, 12, 4)
    additions, deletions = cr._diff_line_counts(diff)
    assert deletions > additions * 0.5 and deletions - additions > 0


def test_case_a_result_md_mass_rewrite_with_small_code_change_passes():
    diff = _modify_diff("RESULT.md", 54, 796, "r") + _modify_diff(CODE_PATH, 12, 4)
    notes: dict = {}
    assert _gate(diff, ["RESULT.md", CODE_PATH], notes) is None
    assert notes["preservation_ignored_result_md_lines"] == 54 + 796


def test_case_a_feedback_records_ignored_lines_when_other_issue_blocks():
    diff = _modify_diff("RESULT.md", 5, 100, "r") + _modify_diff(CODE_PATH, 10, 40)
    verdict = _gate(diff, ["RESULT.md", CODE_PATH])
    assert verdict is not None
    assert verdict.feedback["preservation_ignored_result_md_lines"] == 105
    assert verdict.feedback["additions"] == 10
    assert verdict.feedback["deletions"] == 40


def test_result_md_only_change_passes():
    assert _gate(_modify_diff("RESULT.md", 0, 300), ["RESULT.md"]) is None


def test_case_b_code_mass_deletion_still_blocked():
    diff = _modify_diff(CODE_PATH, 12, 445)
    verdict = _gate(diff, [CODE_PATH])
    assert verdict is not None
    assert verdict.flag_category == "PRESERVATION_HARD_GATE"
    assert "preservation_ignored_result_md_lines" not in verdict.feedback


def test_case_c_nested_result_md_still_counted_and_blocked():
    path = "docs/x/RESULT.md"
    diff = _modify_diff(path, 12, 445)
    verdict = _gate(diff, [path])
    assert verdict is not None
    assert verdict.flag_category == "PRESERVATION_HARD_GATE"
    assert verdict.feedback["deletions"] == 445
    assert "preservation_ignored_result_md_lines" not in verdict.feedback


def test_case_c_other_markdown_still_counted():
    verdict = _gate(_modify_diff("README.md", 5, 200), ["README.md"])
    assert verdict is not None


def test_case_d_result_md_rewrite_plus_code_mass_deletion_blocked():
    diff = _modify_diff("RESULT.md", 54, 796, "r") + _modify_diff(CODE_PATH, 12, 445)
    verdict = _gate(diff, ["RESULT.md", CODE_PATH])
    assert verdict is not None
    assert verdict.flag_category == "PRESERVATION_HARD_GATE"
    assert verdict.feedback["deletions"] == 445
    assert verdict.feedback["preservation_ignored_result_md_lines"] == 54 + 796


def test_scope_gate_still_sees_result_md():
    diff = _modify_diff("RESULT.md", 54, 796)
    verdict = cr._precheck_preservation_gate(
        diff, "허용 파일: app/services/code_reviewer.py", ["RESULT.md"]
    )
    assert verdict is not None
    assert "RESULT.md" in verdict.feedback["out_of_scope_files"]


def test_strip_only_exact_root_path():
    nested = _modify_diff("sub/RESULT.md", 1, 2)
    kept, lines = cr._strip_runner_result_sections(nested)
    assert kept == nested and lines == 0
    renamed = (
        "diff --git a/app/foo.py b/RESULT.md\n--- a/app/foo.py\n+++ b/RESULT.md\n"
        "@@ -1,1 +1,1 @@\n-x\n+y\n"
    )
    kept, lines = cr._strip_runner_result_sections(renamed)
    assert kept == renamed and lines == 0
