"""검증 명령의 인자로 등장한 경로를 수정 대상으로 오인하지 않는지 검증한다.

`_extract_target_files()` 가 지시서 본문의 경로처럼 생긴 토큰을 전부
"수정 대상 파일" 로 봐서, 실제로는 서로 다른 파일을 고치는 지시서들이
R-QUALITY 가 강제하는 공통 검증 명령(`scripts/run_unit_tests.sh`) 하나 때문에
`server:scripts/run_unit_tests.sh` 로 충돌 판정되어 큐 전체가 직렬화된
사고가 있었다(AADS-RUNNERGUARD-VERIFY-PATH-FALSEPOSITIVE-R2).
"""
import os

os.environ.setdefault("JWT_SECRET_KEY", "unit-test-secret")
os.environ.setdefault("E2B_API_KEY", "unit-test-e2b-key")

from app.api.pipeline_runner import _extract_target_files


def test_verify_command_line_paths_are_excluded():
    instruction = (
        "## 검증\n"
        "bash scripts/run_unit_tests.sh tests/unit/test_a.py\n"
    )
    assert _extract_target_files(instruction) == set()


def test_explicit_modification_target_is_included():
    instruction = "`app/services/goal_dispatch.py` 를 수정한다."
    files = _extract_target_files(instruction)
    assert "server:app/services/goal_dispatch.py" in files


def test_harness_file_named_as_modification_target_is_included():
    instruction = "`scripts/pipeline-runner.sh` 의 looks_like_git_diff 를 고친다."
    files = _extract_target_files(instruction)
    assert "server:scripts/pipeline-runner.sh" in files


def test_same_harness_file_as_command_argument_is_excluded():
    instruction = "bash scripts/pipeline-runner.sh --check\n"
    assert _extract_target_files(instruction) == set()


def test_todays_conflicting_instructions_have_no_intersection():
    work_recipe_instruction = (
        "TASK_ID: AADS-WORKRECIPE-NEW\n"
        "## 고칠 것 — 신규 파일 3개\n"
        "- `app/services/work_recipe_builder.py` 신설\n"
        "- `app/services/work_recipe_validator.py` 신설\n"
        "- `app/services/work_recipe_store.py` 신설\n"
        "## 검증\n"
        "bash scripts/run_unit_tests.sh tests/unit/test_work_recipe.py\n"
    )
    goal_instruction = (
        "TASK_ID: AADS-GOAL-BLOCK-SIGNAL-SPLIT\n"
        "## 고칠 것\n"
        "`app/services/goal_dispatch.py` 의 막힘 판정을 dispatch_blocked_at 으로 분리한다.\n"
        "`app/routers/goals.py`, `app/services/goal_report.py` 도 같이 수정한다.\n"
        "## 검증\n"
        "bash scripts/run_unit_tests.sh tests/unit/test_goal_block_signal_split.py\n"
    )
    work_recipe_files = _extract_target_files(work_recipe_instruction)
    goal_files = _extract_target_files(goal_instruction)
    assert work_recipe_files
    assert goal_files
    assert work_recipe_files & goal_files == set()
