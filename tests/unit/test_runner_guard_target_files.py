"""검증 명령의 인자로 등장한 경로를 수정 대상으로 오인하지 않는지 검증한다.

`_extract_target_files()` 가 지시서 본문의 경로처럼 생긴 토큰을 전부
"수정 대상 파일" 로 봐서, 실제로는 서로 다른 파일을 고치는 지시서들이
R-QUALITY 가 강제하는 공통 검증 명령(`scripts/run_unit_tests.sh`) 하나 때문에
`server:scripts/run_unit_tests.sh` 로 충돌 판정되어 큐 전체가 직렬화된
사고가 있었다(AADS-RUNNERGUARD-VERIFY-PATH-FALSEPOSITIVE-R2).
"""
import os

import pytest

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


def test_midline_verify_command_path_is_excluded():
    """목록 번호 뒤 문장 중간에 등장한 검증 명령의 인자 경로는 제외된다.

    실제 발생: "4. 신규 단위테스트 추가 후 `bash <스크립트> <테스트파일>` exit=0
    확인." 형태는 줄이 명령으로 시작하지 않아 기존 판정을 통과했다
    (AADS-RUNNERGUARD-VERIFY-PATH-MIDLINE-P1).
    """
    instruction = (
        "4. 신규 단위테스트 추가 후 "
        "`bash scripts/run_unit_tests.sh tests/unit/test_midline.py` exit=0 확인.\n"
    )
    assert _extract_target_files(instruction) == set()


def test_line_starting_with_exec_command_still_excluded():
    """줄이 명령으로 시작하는 기존 케이스는 회귀 없이 계속 제외된다."""
    instruction = "bash scripts/run_unit_tests.sh tests/unit/test_a.py\n"
    assert _extract_target_files(instruction) == set()


def test_modification_sentence_path_not_excluded():
    """수정 대상 문장에 단독으로 등장한 경로는 명령 토큰이 없으므로 남는다."""
    instruction = "다음 파일을 수정하라: app/services/foo.py\n"
    files = _extract_target_files(instruction)
    assert "server:app/services/foo.py" in files


def test_path_on_different_line_from_command_not_excluded():
    """명령 줄과 다른 줄에 단독으로 등장한 경로는 제외되지 않는다."""
    instruction = (
        "app/services/bar.py 를 고친다.\n"
        "bash scripts/run_unit_tests.sh tests/unit/test_bar.py\n"
    )
    files = _extract_target_files(instruction)
    assert "server:app/services/bar.py" in files
    assert "server:tests/unit/test_bar.py" not in files


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


# ---- 명시 머리표 줄만 제외 / 루트 핸드오버 제외 (AADS-RUNNERGUARD-HEADER-ONLY-EXCLUDE) ----
# 문장 안의 금지 표현으로는 경로를 빼지 않는다. 오탐보다 누락이 훨씬 위험하다.

P1 = "app/services/alpha_one.py"
P2 = "app/services/beta_two.py"
P3 = "app/services/gamma_three.py"
S1 = f"server:{P1}"
S2 = f"server:{P2}"
S3 = f"server:{P3}"


@pytest.mark.parametrize("header", ["FORBIDDEN_FILES", "READ_ONLY_FILES", "REFERENCE_FILES"])
def test_header_line_inline_values_are_excluded(header):
    assert _extract_target_files(f"{header}: {P1}, {P2}\n") == set()


@pytest.mark.parametrize("prefix", ["", "  ", "- ", "* ", "  - "])
def test_header_line_allows_leading_space_dash_star(prefix):
    assert _extract_target_files(f"{prefix}FORBIDDEN_FILES: {P1}\n") == set()


def test_empty_header_excludes_following_dash_list_only():
    instruction = (
        "READ_ONLY_FILES:\n"
        f"- {P1}\n"
        f"- {P2}\n"
        "\n"
        f"{P3} 를 수정한다.\n"
    )
    assert _extract_target_files(instruction) == {S3}


def test_empty_header_list_ends_at_next_header():
    instruction = (
        "REFERENCE_FILES:\n"
        f"- {P1}\n"
        f"TARGET_FILES: {P2}\n"
        f"{P3} 도 고친다.\n"
    )
    assert _extract_target_files(instruction) == {S2}


def test_empty_header_list_ends_at_non_list_line():
    instruction = (
        "FORBIDDEN_FILES:\n"
        f"- {P1}\n"
        f"{P2} 를 고친다.\n"
        f"- {P3}\n"
    )
    assert _extract_target_files(instruction) == {S2, S3}


def test_header_with_value_does_not_swallow_following_list():
    instruction = (
        f"READ_ONLY_FILES: {P1}\n"
        f"- {P2}\n"
    )
    assert _extract_target_files(instruction) == {S2}


@pytest.mark.parametrize(
    "sentence",
    [
        f"{P1} 삭제 금지, 로직 수정한다",
        f"{P1} 의 금지 목록에 항목을 추가한다",
        f"{P1} 의 수정 금지 메시지를 변경한다",
        f"{P1} 를 수정하라: 읽기전용 옵션을 추가",
    ],
)
def test_prohibition_words_inside_sentence_never_exclude_path(sentence):
    assert _extract_target_files(sentence + "\n") == {S1}


def test_changelog_with_inline_prohibition_stays_a_write():
    assert _extract_target_files("CHANGELOG.md 갱신 (삭제 금지)\n") == {"server:CHANGELOG.md"}


@pytest.mark.parametrize(
    "line",
    [
        f"READ_ONLY {P1}",
        f"READ_ONLY_FILES {P1}",
        f"read_only_files: {P1}",
        f"Forbidden_Files: {P1}",
        f"읽기전용: {P1}",
        f"수정 금지 파일: {P1}",
        f"참고 {P1} 수정 금지",
    ],
)
def test_non_exact_header_forms_stay_a_write(line):
    assert _extract_target_files(line + "\n") == {S1}


def test_malformed_declaration_disables_header_exclusion():
    """선언 오류(경로 아님)면 기존 보수 판정 — 머리표 줄 경로도 쓰기로 본다."""
    instruction = (
        "TARGET_FILES: not a path!!\n"
        f"FORBIDDEN_FILES: {P1}\n"
    )
    assert S1 in _extract_target_files(instruction)


def test_target_files_line_path_is_included():
    assert _extract_target_files(f"TARGET_FILES: {P1}\n") == {S1}


def test_target_files_with_forbidden_header_keeps_only_target():
    instruction = f"TARGET_FILES: {P1}\nFORBIDDEN_FILES: {P2}\n"
    assert _extract_target_files(instruction) == {S1}


@pytest.mark.parametrize(
    "instruction",
    [
        "HANDOVER.md 는 수정 금지.\n",
        "완료 후 HANDOVER.md 를 갱신한다.\n",
        "/root/aads/aads-server/HANDOVER.md 수정 금지\n",
        "/root/aads/aads-dashboard/HANDOVER.md 수정 금지\n",
        "GO100_HANDOVER.md 는 건드리지 않는다.\n",
    ],
)
def test_root_handover_doc_is_not_a_write(instruction):
    assert _extract_target_files(instruction) == set()


def test_dashboard_root_handover_doc_is_not_a_write():
    assert _extract_target_files("aads-dashboard/HANDOVER.md 수정 금지\n") == set()


def test_handover_under_subdir_stays_a_write():
    assert _extract_target_files("docs/HANDOVER.md 수정 금지\n") == {"server:docs/HANDOVER.md"}


def test_header_exclusion_keeps_exec_command_exclusion():
    instruction = (
        f"FORBIDDEN_FILES: {P1}\n"
        "bash scripts/run_unit_tests.sh tests/unit/test_a.py\n"
        f"{P2} 를 수정한다.\n"
    )
    assert _extract_target_files(instruction) == {S2}
