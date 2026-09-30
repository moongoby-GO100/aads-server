"""AADS-REVIEWER-PRESERVATION-SHADOWED-DUPLICATE-R3 — 스코프 기반 삭제 판정 고정.

525fce0d 는 tool_executor.py 에서 (A) 같은 클래스에 두 번 정의돼 있던 private 메서드의
앞 정의(死코드)와 (B) 파일 밖 참조가 없는 private 예외 클래스 둘, (C) 그 클래스 본문의
`__init__` 둘을 지웠다는 이유로 PRESERVATION_HARD_GATE 에 막혔다.

면제는 이름이 아니라 **스코프 경로**로만 판정한다. 근거는 tree 의 pre-image 에 diff 를 엄격히
적용해 얻은 post-image 이며, 적용·파싱·소속 확인 중 하나라도 실패하면 면제하지 않는다.

픽스처 심볼명 규칙: 저장소에 실재하는 private 심볼명을 이 파일에 쓰지 않는다. 규칙 B 는 "삭제 파일
밖 참조 0건" 을 요구하고 참조 검색은 tests/ 도 훑으므로, 실제 이름을 적는 순간 그 심볼에 대해
규칙 B 가 영구히 fail closed 된다(R2 반려 사유). 아래 `_Fixture*` 이름만 쓰고
`test_fixture_symbol_names_do_not_exist_in_production_code` 가 이를 지킨다.
"""

import difflib
from pathlib import Path

import pytest

import app.services.code_reviewer as cr

_TARGET = "app/services/target_module.py"

_SHADOWED = "preservation_exempted_shadowed_symbols"
_CLASSES = "preservation_exempted_private_classes"
_MEMBERS = "preservation_exempted_class_members"


def _diff(path: str, before: str, after: str) -> str:
    body = "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )
    return f"diff --git a/{path} b/{path}\nindex 0000001..0000002 100644\n{body}"


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / "app" / "services").mkdir(parents=True)
    (root / "app" / "api").mkdir(parents=True)
    monkeypatch.setattr(cr, "_REPO_ROOT", root)
    return root


def _case(repo, before: str, after: str, path: str = _TARGET) -> str:
    # 삭제 라인 수 이상을 추가해 줄 수 비율 게이트가 아니라 심볼 판정만 시험한다.
    padding = "".join(f"PAD_{i} = {i}\n" for i in range(before.count("\n")))
    (repo / path).write_text(before)
    return _diff(path, before, after + padding)


def _gate(diff, project="AADS"):
    out: dict = {}
    verdict = cr._precheck_preservation_gate(diff, "", None, out, project)
    return verdict, out


def _deleted(verdict):
    return verdict.feedback["deleted_symbols"]


_TWO_DEFS = (
    "class Tool:\n"
    "    def other(self):\n        return 0\n\n"
    "    def run(self):\n        return 1\n\n"
    "    def middle(self):\n        return 2\n\n"
    "    def run(self):\n        return 3\n"
)
_ONE_DEF = (
    "class Tool:\n"
    "    def other(self):\n        return 0\n\n"
    "    def middle(self):\n        return 2\n\n"
    "    def run(self):\n        return 3\n"
)


# ── 면제되어야 하는 것 ─────────────────────────────────────────────────


def test_1_shadowed_earlier_method_definition_is_not_a_deletion(repo):
    diff = _case(repo, _TWO_DEFS, _ONE_DEF)
    assert cr._removed_preservation_symbols(diff) == ["def run"]  # 기준선: 이름만 보면 삭제

    verdict, out = _gate(diff)

    assert verdict is None
    assert out[_SHADOWED] == ["def Tool.run"]
    assert _CLASSES not in out and _MEMBERS not in out


def test_1b_shadowed_module_level_function_is_not_a_deletion(repo):
    before = "def run():\n    return 1\n\n\nx = 1\n\n\ndef run():\n    return 2\n"
    after = "x = 1\n\n\ndef run():\n    return 2\n"

    verdict, out = _gate(_case(repo, before, after))

    assert verdict is None
    assert out[_SHADOWED] == ["def run"]


def test_2_private_class_without_outside_reference_is_exempt(repo):
    before = "class _Hidden:\n    limit = 1\n\n\nx = 1\n"
    after = "x = 1\n"

    verdict, out = _gate(_case(repo, before, after))

    assert verdict is None
    assert out[_CLASSES] == ["class _Hidden"]
    assert _SHADOWED not in out and _MEMBERS not in out


_BEFORE_C = (
    "class _FixtureRollback(Exception):\n"
    "    def __init__(self, plan):\n        self.plan = plan\n\n\n"
    "class _FixtureLimitExceeded(Exception):\n"
    "    def __init__(self, actual, limit):\n        self.actual = actual\n        self.limit = limit\n\n\n"
    "class Tool:\n"
    "    async def write(self):\n        return 1\n"
)
_AFTER_C = "class Tool:\n    async def write(self):\n        return 1\n"


def test_3_members_of_an_exempted_private_class_go_with_it(repo):
    diff = _case(repo, _BEFORE_C, _AFTER_C)
    assert cr._removed_preservation_symbols(diff) == [
        "class _FixtureRollback", "def __init__", "class _FixtureLimitExceeded", "def __init__",
    ]

    verdict, out = _gate(diff)

    assert verdict is None
    assert out[_CLASSES] == ["class _FixtureRollback", "class _FixtureLimitExceeded"]
    assert out[_MEMBERS] == ["def _FixtureRollback.__init__", "def _FixtureLimitExceeded.__init__"]


def test_removed_symbols_is_empty_once_the_project_is_known(repo):
    """실사례 형태(A+B+C 동시): project 를 주면 남는 삭제 심볼이 없다."""
    before = _BEFORE_C + "\n    async def run(self):\n        return 1\n\n    async def run(self):\n        return 2\n"
    after = _AFTER_C + "\n    async def run(self):\n        return 2\n"
    diff = _case(repo, before, after)
    notes: dict = {}

    assert cr._removed_preservation_symbols(diff, "AADS", notes) == []
    assert set(notes) >= {_SHADOWED, _CLASSES, _MEMBERS}


# ── 여전히 차단되어야 하는 것 ─────────────────────────────────────────


def test_4_same_method_name_in_a_different_class_is_a_real_deletion(repo):
    before = (
        "class A:\n    def run(self):\n        return 1\n\n\n"
        "class B:\n    def run(self):\n        return 2\n"
    )
    after = "class B:\n    def run(self):\n        return 2\n"

    verdict, out = _gate(_case(repo, before, after))

    assert verdict is not None and verdict.verdict == "FLAG"
    assert "class A" in _deleted(verdict) and "def run" in _deleted(verdict)
    assert _SHADOWED not in out


def test_4b_module_level_function_does_not_shadow_a_deleted_method(repo):
    before = "def run():\n    return 0\n\n\nclass A:\n    def run(self):\n        return 1\n"
    after = "def run():\n    return 0\n\n\nclass A:\n    pass\n"

    verdict, out = _gate(_case(repo, before, after))

    assert verdict is not None
    assert _deleted(verdict) == ["def run"]
    assert _SHADOWED not in out


def test_4c_sync_definition_does_not_shadow_async_definition(repo):
    before = "class A:\n    async def run(self):\n        return 1\n\n    def run(self):\n        return 2\n"
    after = "class A:\n    def run(self):\n        return 2\n"

    verdict, _ = _gate(_case(repo, before, after))

    assert verdict is not None
    assert _deleted(verdict) == ["async def run"]


def test_4d_definition_under_a_condition_does_not_shadow(repo):
    before = "def run():\n    return 1\n\n\nif FLAG:\n    def run():\n        return 2\n"
    after = "if FLAG:\n    def run():\n        return 2\n"

    verdict, _ = _gate(_case(repo, before, after))

    assert verdict is not None
    assert _deleted(verdict) == ["def run"]


def test_5_single_public_function_and_class_deletion_stay_blocked(repo):
    before = "def build():\n    return 1\n\n\nclass Widget:\n    limit = 1\n\n\nx = 1\n"
    after = "x = 1\n"

    verdict, out = _gate(_case(repo, before, after))

    assert verdict is not None
    assert _deleted(verdict) == ["def build", "class Widget"]
    assert out == {}


def test_6_private_class_referenced_from_another_file_stays_blocked(repo):
    (repo / "app" / "api" / "caller.py").write_text("from app.services.target_module import _Hidden\n")
    diff = _case(repo, "class _Hidden:\n    limit = 1\n\n\nx = 1\n", "x = 1\n")

    verdict, out = _gate(diff)

    assert verdict is not None
    assert _deleted(verdict) == ["class _Hidden"]
    assert _CLASSES not in out


def test_6b_members_of_a_referenced_private_class_stay_blocked(repo):
    (repo / "app" / "api" / "caller.py").write_text("x = _FixtureRollback\n")

    verdict, out = _gate(_case(repo, _BEFORE_C, _AFTER_C))

    assert verdict is not None
    assert _deleted(verdict) == ["class _FixtureRollback", "def __init__"]
    assert out[_CLASSES] == ["class _FixtureLimitExceeded"]
    assert out[_MEMBERS] == ["def _FixtureLimitExceeded.__init__"]


def test_7_init_of_a_live_public_class_stays_blocked(repo):
    before = "class Widget:\n    def __init__(self):\n        self.a = 1\n\n    def other(self):\n        return 1\n"
    after = "class Widget:\n    def other(self):\n        return 1\n"

    verdict, out = _gate(_case(repo, before, after))

    assert verdict is not None
    assert _deleted(verdict) == ["def __init__"]
    assert _MEMBERS not in out


def test_7b_init_name_alone_never_rides_on_another_classes_exemption(repo):
    """면제된 private 클래스와 같은 diff 에서 살아 있는 클래스의 __init__ 이 지워진 경우."""
    before = (
        "class _Hidden:\n    def __init__(self):\n        self.a = 1\n\n\n"
        "class Widget:\n    def __init__(self):\n        self.b = 2\n\n    def keep(self):\n        return 1\n"
    )
    after = "class Widget:\n    def keep(self):\n        return 1\n"

    verdict, out = _gate(_case(repo, before, after))

    assert verdict is not None
    assert _deleted(verdict) == ["def __init__"]
    assert out[_CLASSES] == ["class _Hidden"]
    assert out[_MEMBERS] == ["def _Hidden.__init__"]


def test_8_router_symbol_deletion_stays_blocked(repo):
    before = 'from x import router\n\n\n@router.get("/x")\nasync def get_x():\n    return 1\n\n\ny = 1\n'
    after = "from x import router\n\n\ny = 1\n"

    verdict, _ = _gate(_case(repo, before, after))

    assert verdict is not None
    assert "@router.get" in _deleted(verdict)


def test_9_unparsable_post_image_never_exempts(repo):
    """앞 정의 삭제는 그대로지만 diff 가 문법 오류를 만들면 post-image 를 못 얻는다 -> fail closed."""
    verdict, out = _gate(_case(repo, _TWO_DEFS, _ONE_DEF + "def broken(:\n"))

    assert verdict is not None
    assert _deleted(verdict) == ["def run"]
    assert _SHADOWED not in out


def test_10_existing_exemptions_still_work(repo):
    signature = "def create(req):\n    return req\n"
    rewritten = "def create(\n    req,\n):\n    return req\n"
    verdict, out = _gate(_case(repo, signature, rewritten))
    assert verdict is None and out == {}

    tests_path = "tests/unit/test_x.py"
    (repo / "tests" / "unit").mkdir(parents=True)
    verdict, _ = _gate(
        _case(repo, "def test_old():\n    pass\n", "def test_new():\n    pass\n", tests_path)
    )
    assert verdict is None


# ── fail closed 경로 ──────────────────────────────────────────────────


def test_tree_that_does_not_match_the_diff_never_exempts(repo):
    diff = _case(repo, _TWO_DEFS, _ONE_DEF)
    (repo / _TARGET).write_text(_TWO_DEFS.replace("return 0", "return 9"))

    verdict, out = _gate(diff)

    assert verdict is not None
    assert _SHADOWED not in out


def test_file_missing_from_tree_never_exempts(repo):
    diff = _case(repo, _TWO_DEFS, _ONE_DEF)
    (repo / _TARGET).unlink()

    verdict, _ = _gate(diff)

    assert verdict is not None


def test_hunk_header_that_lies_about_line_counts_never_exempts(repo):
    diff = _case(repo, _TWO_DEFS, _ONE_DEF)
    lines = diff.splitlines(keepends=True)
    hunk = next(i for i, line in enumerate(lines) if line.startswith("@@"))
    lines[hunk] = "@@ -1,99 +1,98 @@\n"

    verdict, _ = _gate("".join(lines))

    assert verdict is not None


@pytest.mark.parametrize("project", [None, "", "KIS", "GO100"])
def test_other_projects_never_get_the_scope_exemptions(repo, project):
    verdict, out = _gate(_case(repo, _TWO_DEFS, _ONE_DEF), project=project)

    assert verdict is not None
    assert _SHADOWED not in out


def test_new_file_and_rename_diffs_never_exempt(repo):
    diff = _case(repo, _TWO_DEFS, _ONE_DEF)
    marked = diff.replace("index 0000001..0000002 100644\n", "deleted file mode 100644\n", 1)

    verdict, _ = _gate(marked)

    assert verdict is not None


def test_fixture_symbol_names_do_not_exist_in_production_code():
    """픽스처 이름이 운영 코드(app/)에 실재하면 규칙 B 가 그 이름을 외부 참조로 보게 된다."""
    names = {"_FixtureRollback", "_FixtureLimitExceeded", "_Hidden"}
    real_root = Path(__file__).resolve().parents[2] / "app"

    found = cr._find_symbol_reference_files(names, [str(real_root)])

    assert found is not None, "참조 검색 자체가 실패했다 — fail closed 경로"
    assert all(not files for files in found.values()), found


def test_apply_helper_rejects_context_drift(repo):
    diff = _diff(_TARGET, "a = 1\nb = 2\nc = 3\n", "a = 1\nc = 3\n")
    assert cr._apply_file_diff_to_text("a = 1\nb = 2\nc = 3\n", diff, _TARGET) == (
        "a = 1\nc = 3\n", frozenset({2}),
    )
    assert cr._apply_file_diff_to_text("a = 1\nb = 9\nc = 3\n", diff, _TARGET) is None
