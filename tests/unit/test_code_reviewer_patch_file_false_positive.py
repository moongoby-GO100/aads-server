"""보존 하드 게이트가 .patch/.diff 파일의 본문을 소스 코드로 읽던 오탐 회귀 고정.

2026-09-30 runner-0ebc83ce: `.runner_full_diff.patch`(추적 파일)를 지우는 diff 의
문맥줄이 바깥 diff 에서 `- @router.get(...)` / `- async def ...(` 로 보여
"삭제된 public 라우터·함수" 로 FLAG 0.3 이 났다. 실제 삭제된 소스 심볼은 0개였다.

고정하는 것:
① 패치 파일만 지우는 diff 는 보존 게이트가 차단하지 않는다.
② 같은 패턴이 소스 파일(app/api/*.py)에 있으면 계속 차단한다.
③ 패치 파일 삭제와 실제 소스 심볼 삭제가 섞이면 소스 쪽 때문에 차단한다.
④ 소스 파일을 .patch 로 개명하는 변경은 제외 대상이 아니다.
"""

import app.services.code_reviewer as cr

PATCH_PATH = ".runner_full_diff.patch"
SOURCE_PATH = "app/api/foo.py"

ROUTER_LINE = '@router.get("/uploaded-ledger")'
FUNC_LINE = "async def list_uploaded_ledger("


def _deleted_file_diff(path: str, body_lines: list) -> str:
    header = [
        f"diff --git a/{path} b/{path}",
        "deleted file mode 100644",
        "index 0000001..0000000",
        f"--- a/{path}",
        "+++ /dev/null",
        f"@@ -1,{len(body_lines)} +0,0 @@",
    ]
    return "\n".join(header + [f"-{line}" for line in body_lines]) + "\n"


def _patch_body() -> list:
    # 패치 파일 안에서 문맥줄은 앞 한 칸 공백이 붙는다 → 바깥 diff 에서 "- @router..." 가 된다.
    return [
        "diff --git a/app/api/x.py b/app/api/x.py",
        "@@ -10,4 +10,5 @@",
        f" {ROUTER_LINE}",
        f" {FUNC_LINE}",
        "     pass",
        "+    return None",
    ]


def _source_body() -> list:
    return [f" {ROUTER_LINE}", f" {FUNC_LINE}", "     pass"]


def _gate(diff: str, files=None):
    return cr._precheck_preservation_gate(diff, "", files)


def test_old_behaviour_would_block_patch_only_deletion():
    """필터를 거치지 않은 원본 diff 로는 실제로 심볼이 잡힌다(오탐 재현 근거)."""
    diff = _deleted_file_diff(PATCH_PATH, _patch_body())
    assert cr._removed_preservation_symbols(diff)
    additions, deletions = cr._diff_line_counts(diff)
    assert additions == 0 and deletions > 0


def test_case1_patch_only_deletion_is_not_blocked():
    diff = _deleted_file_diff(PATCH_PATH, _patch_body())
    assert _gate(diff, [PATCH_PATH]) is None


def test_case1_records_why_it_passed():
    diff = _deleted_file_diff(PATCH_PATH, _patch_body())
    notes: dict = {}
    assert cr._precheck_preservation_gate(diff, "", [PATCH_PATH], notes) is None
    assert notes["preservation_ignored_patch_files"] == [PATCH_PATH]
    assert notes["preservation_ignored_patch_lines"] == len(_patch_body())


def test_case2_same_pattern_in_source_file_is_still_blocked():
    diff = _deleted_file_diff(SOURCE_PATH, _source_body())
    verdict = _gate(diff, [SOURCE_PATH])
    assert verdict is not None
    assert verdict.verdict == "FLAG"
    symbols = verdict.feedback["deleted_symbols"]
    assert any("list_uploaded_ledger" in s for s in symbols)
    assert "preservation_ignored_patch_files" not in verdict.feedback


def test_case3_patch_deletion_mixed_with_real_source_deletion_is_blocked():
    diff = _deleted_file_diff(PATCH_PATH, _patch_body()) + _deleted_file_diff(
        SOURCE_PATH, _source_body()
    )
    verdict = _gate(diff, [PATCH_PATH, SOURCE_PATH])
    assert verdict is not None
    assert verdict.verdict == "FLAG"
    assert any("list_uploaded_ledger" in s for s in verdict.feedback["deleted_symbols"])
    # 제외 사실은 남는다 — 왜 patch 쪽이 판정에서 빠졌는지 추적 가능해야 한다.
    assert verdict.feedback["preservation_ignored_patch_files"] == [PATCH_PATH]
    assert verdict.feedback["preservation_ignored_patch_lines"] > 0


def test_diff_extension_is_also_ignored():
    diff = _deleted_file_diff("docs/old_change.diff", _patch_body())
    assert _gate(diff, ["docs/old_change.diff"]) is None


def test_renaming_source_to_patch_is_not_ignored():
    """a 경로가 소스면 b 경로가 .patch 여도 제외하지 않는다(우회 방지)."""
    diff = "\n".join(
        [
            f"diff --git a/{SOURCE_PATH} b/{SOURCE_PATH}.patch",
            "similarity index 40%",
            f"rename from {SOURCE_PATH}",
            f"rename to {SOURCE_PATH}.patch",
            f"--- a/{SOURCE_PATH}",
            f"+++ b/{SOURCE_PATH}.patch",
            "@@ -1,3 +1,1 @@",
            f"-{FUNC_LINE}",
            "+x",
        ]
    ) + "\n"
    ignored, paths, lines = cr._strip_patch_file_sections(diff)
    assert paths == [] and lines == 0
    assert ignored == diff


def test_scope_gate_still_sees_patch_file_changes():
    """제외는 보존 판정에만 적용된다. 지시서 범위 밖 .patch 변경은 범위 게이트가 계속 본다."""
    diff = _deleted_file_diff(PATCH_PATH, _patch_body())
    verdict = cr._precheck_preservation_gate(
        diff, "허용 파일: app/services/code_reviewer.py", [PATCH_PATH]
    )
    assert verdict is not None
    assert PATCH_PATH in verdict.feedback["out_of_scope_files"]
    assert "deleted_symbols" not in verdict.feedback


def test_strip_keeps_source_sections_byte_for_byte():
    source = _deleted_file_diff(SOURCE_PATH, _source_body())
    mixed = _deleted_file_diff(PATCH_PATH, _patch_body()) + source
    kept, paths, _ = cr._strip_patch_file_sections(mixed)
    assert kept == source
    assert paths == [PATCH_PATH]
