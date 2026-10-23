"""명시적 허용 경로의 유니코드 추출 및 보존 게이트 회귀 검증."""

import pytest

from app.services import code_reviewer as cr

DOCUMENT = "docs/design/20261023_GO100_일봉파동쉐도우원장생산기_설계.md"


@pytest.mark.parametrize("path", [
    DOCUMENT,
    "문서/설계/일봉_파동-v1.2.md",
    "설계_문서.md",
    "app/services/code_reviewer.py",
    "README.md",
])
def test_extracts_complete_authorized_path(path):
    assert cr._extract_explicit_scope_paths(f"EXACT AUTHORIZED FILES: {path}") == {path}


@pytest.mark.parametrize("instruction", [
    f"허용 경로: `{DOCUMENT}`, '설계.md', \"app/main.py\"",
    f"EXACT AUTHORIZED FILES:\n- `{DOCUMENT}`\n* 설계.md\n- app/main.py",
    f"allowed paths: ({DOCUMENT}), (설계.md), (app/main.py)",
])
def test_extracts_multiple_quoted_and_list_paths(instruction):
    assert cr._extract_explicit_scope_paths(instruction) == {
        DOCUMENT, "설계.md", "app/main.py",
    }


@pytest.mark.parametrize("path, expected", [
    ("./docs/설계.md", "docs/설계.md"),
    ("/root/aads/aads-server/docs/설계.md", "docs/설계.md"),
    ("/root/aads/aads-dashboard/src/main.ts", "src/main.ts"),
    ("docs/design.md.,", "docs/design.md"),
])
def test_preserves_scope_normalization(path, expected):
    assert cr._extract_explicit_scope_paths(f"allowed files: {path}") == {expected}


def _diff(path, quoted):
    def git_path(prefix):
        value = prefix + path
        if not quoted:
            return value
        return '"' + ''.join(
            f"\\{byte:03o}" if byte >= 128 else chr(byte)
            for byte in value.encode("utf-8")
        ) + '"'

    return (
        f"diff --git {git_path('a/')} {git_path('b/')}\n"
        f"--- {git_path('a/')}\n+++ {git_path('b/')}\n"
        "@@ -1 +1 @@\n-previous text\n+updated text\n"
    )


@pytest.mark.parametrize("quoted", [False, True])
@pytest.mark.parametrize("files_changed", [None, [DOCUMENT]])
def test_authorized_korean_document_passes_preservation_gate(quoted, files_changed):
    diff = _diff(DOCUMENT, quoted)
    assert cr._extract_changed_files(diff) == [DOCUMENT]
    assert cr._precheck_preservation_gate(
        diff, f"allowed files: {DOCUMENT}", files_changed,
    ) is None


@pytest.mark.parametrize("other", [
    "docs/design/다른설계.md", DOCUMENT + ".backup", "app/main.py",
])
def test_other_paths_still_fail_preservation_scope_gate(other):
    diff = _diff(DOCUMENT, True) + _diff(other, True)
    assert cr._extract_changed_files(diff) == [DOCUMENT, other]
    verdict = cr._precheck_preservation_gate(
        diff,
        f"allowed files: {DOCUMENT}", None,
    )
    assert verdict is not None
    assert verdict.flag_category == "PRESERVATION_HARD_GATE"
    assert verdict.feedback["allowed_paths"] == [DOCUMENT]
    assert verdict.feedback["out_of_scope_files"] == [other]


def test_unlabelled_paths_and_text_after_scope_block_are_not_authorized():
    instruction = f"참고: docs/참고.md\nallowed files: {DOCUMENT}\n\n참고: app/main.py"
    assert cr._extract_explicit_scope_paths(instruction) == {DOCUMENT}
