"""pre-commit Step 3 의 변경 파일 → 테스트 묶음 대응(scripts/pre_commit_test_map.py) 회귀시험.

무엇을 지키려는 테스트인가
  1. 배포·러너 코드만 고친 커밋이 단위 테스트를 한 건도 돌리지 않고 통과하던 구멍
     (2026-09-29, runner-104b2fda 산출물 유실)이 다시 열리지 않는다.
  2. 여러 묶음을 돌려도 하나라도 실행 불가(2)·실패(1)면 차단 종료코드가 나온다.
  3. 대응 없는 변경은 예전처럼 생략되고 종료코드 0 이다 — 막히는 게이트는 곧 우회된다.
  4. hook 이 실제로 이 대응표를 부른다.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("pre_commit_test_map", _REPO / "scripts" / "pre_commit_test_map.py")
assert _spec and _spec.loader
gate = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = gate  # dataclass 가 모듈을 조회한다
_spec.loader.exec_module(gate)

DEPLOY_TESTS = [
    "tests/unit/test_deploy_build_guards.py",
    "tests/unit/test_deploy_dependency_image_contract.py",
    "tests/unit/test_deploy_autoheal.py",
    "tests/unit/test_deploy_stream_reconcile.py",
    "tests/unit/test_deploy_terminal_state_contract.py",
    "tests/unit/test_deploy_ancestor_release_guard.py",
]


def _selected(paths):
    return {g.name: present for g, present, _missing in gate.select(paths)}


def test_runner_service_change_selects_runner_bundle():
    chosen = _selected(["app/services/pipeline_runner_service.py"])
    assert list(chosen) == ["runner"]
    assert "tests/unit/test_pipeline_runner_deploy_lock_requeue.py" in chosen["runner"]


def test_runner_shell_change_selects_runner_bundle():
    chosen = _selected(["scripts/pipeline-runner.sh"])
    assert "tests/unit/test_pipeline_runner_shell_deploy_lock_requeue.py" in chosen["runner"]
    assert "tests/unit/test_pipeline_runner_rebase_requeue_guard.py" in chosen["runner"]


def test_deploy_script_change_selects_deploy_bundle():
    for path in ("deploy.sh", "scripts/deploy_autoheal.sh"):
        chosen = _selected([path])
        assert list(chosen) == ["deploy"], path
        assert chosen["deploy"] == DEPLOY_TESTS, path


def test_hook_change_selects_dup_guard():
    for path in ("scripts/hooks/pre-commit", "scripts/dup_guard.py", "scripts/pre_commit_test_map.py"):
        assert "tests/unit/test_dup_guard.py" in _selected([path])["hooks"], path


def test_legacy_tool_pattern_still_selects_tools_test():
    """현행 TOOL_FILES_PATTERN 대응은 없애지 않는다."""
    chosen = _selected(["app/services/tool_executor.py"])
    assert chosen == {"tools": ["tests/unit/test_tools_and_pipeline.py"]}
    # 예전 hook 과 같이 .py 에만 건다.
    assert _selected(["docs/tool_executor.md"]) == {}


def test_unmapped_change_is_skipped_with_exit_zero():
    calls = []
    logs = []
    rc = gate.run(["README.md", "app/services/memory_manager.py"],
                  runner=lambda tests: calls.append(tests) or (0, ""), log=logs.append)
    assert rc == 0
    assert calls == []
    assert any("생략" in line for line in logs)


def test_every_mapped_test_exists_in_repo():
    """대응표가 가리키는 테스트가 사라지면 경고만 남고 검증이 빠진다 — 여기서 먼저 잡는다."""
    for g in gate.GROUPS:
        for t in g.tests:
            assert (_REPO / t).is_file(), "%s: %s 없음" % (g.name, t)


def test_missing_test_file_warns_instead_of_silently_skipping(monkeypatch):
    fake = gate.Group(name="ghost", tests=("tests/unit/test_does_not_exist.py",), globs=("x.sh",))
    monkeypatch.setattr(gate, "GROUPS", (fake,))
    logs = []
    rc = gate.run(["x.sh"], runner=lambda tests: (0, ""), log=logs.append)
    assert rc == 0
    assert any("test_does_not_exist.py" in line and "없습니다" in line for line in logs)


def test_multiple_bundles_any_unrunnable_blocks_with_2():
    results = {"runner": (1, "1 failed"), "deploy": (2, "no image"), "hooks": (0, "3 passed")}

    def runner(tests):
        for g in gate.GROUPS:
            if list(tests) and tests[0] in g.tests:
                return results[g.name]
        raise AssertionError(tests)

    paths = ["scripts/pipeline-runner.sh", "deploy.sh", "scripts/hooks/pre-commit"]
    assert gate.run(paths, runner=runner, log=lambda s: None) == 2


def test_multiple_bundles_any_failure_blocks_with_1():
    seen = []

    def runner(tests):
        seen.append(tests[0])
        return (1, "2 failed") if "deploy" in tests[0] else (0, "5 passed")

    rc = gate.run(["scripts/pipeline-runner.sh", "deploy.sh"], runner=runner, log=lambda s: None)
    assert rc == 1
    assert len(seen) == 2  # 앞 묶음이 실패해도 나머지 묶음은 끝까지 돌려 전부 보고한다


def test_combine_exit_codes():
    assert gate.combine([]) == 0
    assert gate.combine([0, 0]) == 0
    assert gate.combine([0, 1]) == 1
    assert gate.combine([1, 2, 0]) == 2
    assert gate.combine([5]) == 1


def test_pre_commit_hook_calls_gate_map_outside_python_block_and_blocks():
    hook = (_REPO / "scripts" / "hooks" / "pre-commit").read_text(encoding="utf-8")
    assert "python3 scripts/pre_commit_test_map.py --run" in hook
    # 파일 하나 고정 방식이 되살아나면 배포·러너 변경은 다시 게이트 밖이다.
    assert 'TEST_FILE="tests/unit/test_tools_and_pipeline.py"' not in hook
    # 셸 스크립트만 바뀐 커밋도 돌아야 하므로 PY_FILES 블록 안이 아니라 STAGED 를 본다.
    assert 'echo "$STAGED" | python3 scripts/pre_commit_test_map.py --run' in hook
    step3 = hook[hook.index("python3 scripts/pre_commit_test_map.py --run"):]
    assert 'GATE_RC" -eq 2' in step3 and 'GATE_RC" -ne 0' in step3
