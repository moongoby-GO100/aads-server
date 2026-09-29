"""AADS-REVIEWER-PRESERVATION-PRIVATE-SYMBOL-FALSEPOSITIVE — 밑줄 헬퍼 오탐 교정 고정.

runner-5ceddf2f 는 `def _row_builder` 삭제 하나로 PRESERVATION_HARD_GATE(FLAG 0.3)에
걸렸다. 그 함수는 정의와 유일한 호출이 같은 파일 안인 모듈 private 이었다.

예외는 하나뿐이다: 밑줄로 시작하는 함수이고 삭제 파일 밖에 참조가 0건일 때.
public 함수·클래스·@router.*·던더, 그리고 다른 파일이 참조하는 밑줄 함수는
계속 차단한다.

참조 검색은 이 프로세스가 보는 aads-server 트리만 훑으므로, 프로젝트가 AADS 가 아니거나
삭제 파일이 그 트리에서 확인되지 않으면 "참조 0건" 은 증거가 못 된다 -> 계속 차단한다.
"""

import asyncio

import pytest

import app.services.code_reviewer as cr

_TARGET = "app/services/target_module.py"


def _file_diff(path: str, body: str) -> str:
    return (
        f"diff --git a/{path} b/{path}\n"
        f"index 0000001..0000002 100644\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        f"@@ -1,8 +1,8 @@\n"
        f"{body}"
    )


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / "app" / "services").mkdir(parents=True)
    (root / "app" / "api").mkdir(parents=True)
    (root / "app" / "services" / "target_module.py").write_text(
        "def _row_builder(source, row):\n    return row\n\n\n"
        "def build(rows):\n    return [_row_builder('s', r) for r in rows]\n"
        "class Box:\n    pass\n"
    )
    monkeypatch.setattr(cr, "_REPO_ROOT", root)
    return root


def _gate(diff, project="AADS"):
    out: dict = {}
    verdict = cr._precheck_preservation_gate(diff, "", None, out, project)
    return verdict, out.get("preservation_exempted_private_symbols", [])


def test_private_symbol_without_cross_file_reference_is_exempt(repo):
    """① 실제 사례(runner-5ceddf2f): 밑줄 + 교차모듈 참조 0건 -> 통과, 사실은 기록."""
    diff = _file_diff(_TARGET, "-def _row_builder(source, row):\n+x = 1\n")

    verdict, exempted = _gate(diff)

    assert verdict is None
    assert exempted == ["def _row_builder"]


def test_runner_5ceddf2f_symbol_is_split_out_of_the_gate(repo):
    diff = _file_diff(_TARGET, "-def _row_builder(source, row):\n+x = 1\n")

    kept, exempted = cr._split_exempt_private_symbols(
        diff, cr._removed_preservation_symbols(diff), "AADS"
    )

    assert kept == []
    assert exempted == ["def _row_builder"]


def test_exemption_is_recorded_when_other_issues_still_block(repo):
    diff = _file_diff(
        _TARGET, "-def _row_builder(source, row):\n-class Box:\n+x = 1\n"
    )

    verdict, exempted = _gate(diff)

    assert verdict is not None and verdict.flag_category == "PRESERVATION_HARD_GATE"
    assert verdict.feedback["deleted_symbols"] == ["class Box"]
    assert verdict.feedback["preservation_exempted_private_symbols"] == ["def _row_builder"]
    assert exempted == ["def _row_builder"]


@pytest.mark.parametrize(
    "other_file", ["app/api/caller.py", "dashboard.ts", "run.sh", "q.sql", "x.tsx", "y.js"]
)
def test_private_symbol_referenced_by_another_file_stays_blocked(repo, other_file):
    """② 다른 파일이 한 번이라도 참조하면 종전처럼 차단."""
    (repo / other_file).write_text("call _row_builder now\n")
    diff = _file_diff(_TARGET, "-def _row_builder(source, row):\n+x = 1\n")

    verdict, exempted = _gate(diff)

    assert verdict is not None
    assert verdict.verdict == "FLAG"
    assert verdict.feedback["deleted_symbols"] == ["def _row_builder"]
    assert exempted == []


def test_private_symbol_added_reference_in_diff_stays_blocked(repo):
    diff = _file_diff(_TARGET, "-def _row_builder(source, row):\n+x = 1\n") + _file_diff(
        "app/api/new_caller.py", "+from x import _row_builder\n"
    )

    verdict, _ = _gate(diff)

    assert verdict is not None
    assert verdict.feedback["deleted_symbols"] == ["def _row_builder"]


def test_public_function_deletion_stays_blocked(repo):
    """③ 밑줄 없는 public 함수는 참조가 0건이어도 예외 대상이 아니다."""
    diff = _file_diff(_TARGET, "-def metric_row(source, row):\n+x = 1\n")

    verdict, exempted = _gate(diff)

    assert verdict is not None
    assert verdict.feedback["deleted_symbols"] == ["def metric_row"]
    assert exempted == []


@pytest.mark.parametrize(
    ("body", "symbol"),
    [
        ("-class _Hidden:\n+x = 1\n", "class _Hidden"),
        ("-class Widget:\n+x = 1\n", "class Widget"),
        ('-@router.get("/x")\n+x = 1\n', "@router.get"),
    ],
)
def test_class_and_router_deletion_stay_blocked(repo, body, symbol):
    """④ 클래스(밑줄 클래스 포함)와 API 라우터는 계속 차단."""
    verdict, exempted = _gate(_file_diff("app/api/x.py", body))

    assert verdict is not None
    assert verdict.feedback["deleted_symbols"] == [symbol]
    assert exempted == []


@pytest.mark.parametrize("name", ["__init__", "__call__", "__eq__"])
def test_dunder_method_deletion_stays_blocked(repo, name):
    """⑤ 던더는 밑줄로 시작하지만 프로토콜의 일부다."""
    diff = _file_diff(_TARGET, f"-    def {name}(self):\n+    x = 1\n")

    verdict, exempted = _gate(diff)

    assert verdict is not None
    assert verdict.feedback["deleted_symbols"] == [f"def {name}"]
    assert exempted == []


def test_more_than_cap_symbols_falls_back_to_old_behavior(repo):
    count = cr._PRIVATE_EXEMPT_MAX_SYMBOLS + 1
    body = "".join(f"-def _h{i}():\n" for i in range(count)) + "+x = 1\n"

    verdict, exempted = _gate(_file_diff(_TARGET, body))

    assert verdict is not None
    assert exempted == []
    assert len(verdict.feedback["deleted_symbols"]) == 20


def test_search_failure_never_exempts(repo, monkeypatch):
    monkeypatch.setattr(cr, "_find_symbol_reference_files", lambda names, roots: None)
    diff = _file_diff(_TARGET, "-def _row_builder(source, row):\n+x = 1\n")

    verdict, exempted = _gate(diff)

    assert verdict is not None
    assert exempted == []


def test_same_private_name_deleted_in_two_files_stays_blocked(repo):
    diff = _file_diff(_TARGET, "-def _dup():\n+x = 1\n") + _file_diff(
        "app/api/other.py", "-def _dup():\n+x = 1\n"
    )

    verdict, exempted = _gate(diff)

    assert verdict is not None
    assert exempted == []


_PRIVATE_DIFF = "-def _row_builder(source, row):\n+x = 1\n"


@pytest.mark.parametrize("project", [None, "", "KIS", "GO100", "SF", "NTV2", "NAS"])
def test_other_project_is_never_exempt(repo, project):
    """지적1: 다른 프로젝트 diff 는 이 트리에서 참조 0건이어도 면제하지 않는다."""
    verdict, exempted = _gate(_file_diff(_TARGET, _PRIVATE_DIFF), project=project)

    assert verdict is not None
    assert verdict.feedback["deleted_symbols"] == ["def _row_builder"]
    assert exempted == []


def test_project_name_is_case_insensitive_for_aads(repo):
    verdict, exempted = _gate(_file_diff(_TARGET, _PRIVATE_DIFF), project=" aads ")

    assert verdict is None
    assert exempted == ["def _row_builder"]


@pytest.mark.parametrize(
    "path",
    [
        "app/services/does_not_exist.py",
        "../outside.py",
        "app/services/target_module.ts",
        "/etc/hosts.py",
    ],
)
def test_deleted_file_not_in_tree_is_never_exempt(repo, tmp_path, path):
    """지적2: 삭제 파일이 트리에 실제로 없으면 참조 0건은 검색 부재일 뿐이다."""
    (tmp_path / "outside.py").write_text("def _row_builder():\n    pass\n")

    verdict, exempted = _gate(_file_diff(path, _PRIVATE_DIFF))

    assert verdict is not None
    assert exempted == []


def test_deleted_file_without_the_definition_is_never_exempt(repo):
    """지적2/3: 배포 트리와 diff 기준점이 어긋나 정의가 없으면 판단 불가 -> 차단."""
    (repo / "app" / "services" / "target_module.py").write_text("def other():\n    pass\n")

    verdict, exempted = _gate(_file_diff(_TARGET, _PRIVATE_DIFF))

    assert verdict is not None
    assert exempted == []


def test_missing_dashboard_root_is_recorded_in_feedback(repo):
    """지적5: 대시보드 트리를 못 봤다는 사실을 조용히 넘기지 않는다."""
    diff = _file_diff(_TARGET, _PRIVATE_DIFF) + _file_diff("app/api/x.py", "-class Widget:\n+x = 1\n")

    verdict, _ = _gate(diff)

    assert verdict is not None
    assert verdict.feedback["preservation_exempt_search_missing_roots"] == ["aads-dashboard"]
    assert verdict.feedback["preservation_exempt_search_roots"] == ["repo"]


def test_dashboard_sibling_is_searched_when_present(repo):
    dashboard = repo.parent / "aads-dashboard"
    (dashboard / "src").mkdir(parents=True)
    (dashboard / "src" / "a.ts").write_text("const x = _row_builder\n")

    verdict, exempted = _gate(_file_diff(_TARGET, _PRIVATE_DIFF))

    assert verdict is not None
    assert exempted == []


class _FakeConn:
    def __init__(self, sink):
        self.sink = sink

    async def execute(self, _sql, *args):
        self.sink.append(args)


class _FakePool:
    def __init__(self, sink):
        self.sink = sink

    def acquire(self):
        sink = self.sink

        class _Ctx:
            async def __aenter__(self_inner):
                return _FakeConn(sink)

            async def __aexit__(self_inner, *exc):
                return False

        return _Ctx()


def _save(job_id, sink, monkeypatch):
    import app.core.db_pool as db_pool

    monkeypatch.setattr(db_pool, "get_pool", lambda: _FakePool(sink))
    verdict = cr._build_review_verdict(
        verdict="FLAG", score=0.2, summary="s", issues=["i"], feedback={}
    )
    asyncio.run(
        cr._save_review_result(
            job_id=job_id, project="AADS", verdict=verdict, diff_size=1,
            model_used="m", cost=0.0,
        )
    )


def test_exemption_note_is_saved_only_for_its_own_job(monkeypatch):
    """지적4: 앞 리뷰의 면제 목록이 다른 job 의 feedback 에 섞이지 않는다."""
    import json

    sink: list = []
    cr._PRESERVATION_EXEMPTIONS.set(
        ("job-a", {"preservation_exempted_private_symbols": ["def _x"]})
    )
    _save("job-b", sink, monkeypatch)
    _save("job-a", sink, monkeypatch)

    assert "preservation_exempted_private_symbols" not in json.loads(sink[0][4])
    assert json.loads(sink[1][4])["preservation_exempted_private_symbols"] == ["def _x"]
    cr._PRESERVATION_EXEMPTIONS.set(None)


def test_review_start_clears_stale_exemption(monkeypatch):
    """지적4: precheck 에서 조기 반환하는 뒤 호출은 앞 호출의 잔재를 지운다."""
    async def _no_save(**_kwargs):
        return None

    monkeypatch.setattr(cr, "_save_review_result", _no_save)

    async def scenario():
        cr._PRESERVATION_EXEMPTIONS.set(
            ("job-a", {"preservation_exempted_private_symbols": ["def _x"]})
        )
        await cr.review_code_diff("AADS", "job-b", "", "")
        return cr._PRESERVATION_EXEMPTIONS.get()

    assert asyncio.run(scenario()) is None
