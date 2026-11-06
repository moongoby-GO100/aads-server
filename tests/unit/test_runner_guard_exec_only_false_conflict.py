"""실행 전용 경로와 산문 디렉터리로 인한 충돌 오판 회귀 검증."""
import os

os.environ.setdefault("JWT_SECRET_KEY", "unit-test-secret")
os.environ.setdefault("E2B_API_KEY", "unit-test-e2b-key")

import pytest

from app.api.pipeline_runner import (
    _actual_scope_files,
    _extract_target_files,
    _scope_overlap,
)


@pytest.mark.parametrize("command", [
    "bash scripts/run_unit_tests.sh tests/unit/x.py",
    "sh scripts/check.sh",
    "python3 scripts/check.py",
    "python scripts/check.py",
    "scripts/check.py 실행",
    "run scripts/check.sh",
    "검증: scripts/check.py 를 실행한다",
])
def test_execution_only_paths_do_not_conflict(command):
    assert not _extract_target_files(command)
    assert not _scope_overlap(_extract_target_files(command), _extract_target_files(command))


@pytest.mark.parametrize("declaration", [
    "TARGET_FILES: tests/unit/ 의 foo 테스트(있으면 수정)",
    "TARGET_FILES:\n- tests/unit/ 의 foo 테스트(있으면 수정)",
    "tests/unit/ 의 foo 테스트(있으면 수정)",
])
def test_prose_directory_does_not_own_child_files(declaration):
    assert not _scope_overlap(
        _extract_target_files(declaration),
        _extract_target_files("tests/unit/test_bar.py 수정"),
    )


def test_explicit_directories_still_conflict():
    scope = _extract_target_files("TARGET_FILES: app/services/")
    assert scope == {"server:app/services/"}
    assert _scope_overlap(scope, scope) == ["server:app/services/"]
    actual = _actual_scope_files("TARGET: /root/aads/aads-server", ["app/services/x.py"])
    assert _scope_overlap(scope, actual) == ["server:app/services/x.py"]


def test_explicit_file_edits_still_conflict():
    scope = _extract_target_files("app/services/doc_review_rubric.py 수정")
    assert _scope_overlap(scope, scope) == ["server:app/services/doc_review_rubric.py"]


def test_actual_script_change_still_conflicts_with_declared_edit():
    actual = _actual_scope_files("TARGET: /root/aads/aads-server", ["scripts/run_unit_tests.sh"])
    declared = _extract_target_files("scripts/run_unit_tests.sh 수정")
    assert _scope_overlap(actual, declared) == ["server:scripts/run_unit_tests.sh"]


@pytest.mark.parametrize("text", [
    "scripts/check.py 실행 후 수정",
    "run scripts/check.py and modify it",
    "scripts/check.sh 고친다",
    "scripts/check.py 추가 후 실행",
    "scripts/check.py 변경 후 실행",
    "edit scripts/check.py then run",
])
def test_execution_with_edit_intent_retains_script(text):
    assert _extract_target_files(text)
