"""`.local` shell copies must not trigger the screen-evidence gate; real UI files must."""
import pytest

from app.services.e2e_verify import _NON_RENDERING_SUFFIXES, screen_verification_required

SCREEN_INSTRUCTION = "화면 증거 게이트 오탐 교정"
NEUTRAL_INSTRUCTION = "러너 스크립트 수정"
INSTRUCTIONS = [SCREEN_INSTRUCTION, NEUTRAL_INSTRUCTION]


def test_local_suffix_is_declared_non_rendering():
    assert ".local" in _NON_RENDERING_SUFFIXES


@pytest.mark.parametrize("instruction", INSTRUCTIONS)
def test_shell_only_change_with_local_copy_is_not_screen_work(instruction):
    changed = ["scripts/pipeline-runner.sh", "scripts/pipeline-runner.sh.local", "tests/unit/x.py"]
    assert screen_verification_required(instruction, changed) is False


@pytest.mark.parametrize("instruction", INSTRUCTIONS)
def test_tsx_page_is_still_screen_work(instruction):
    changed = ["frontend/src/go100/pages/CompanyAnalysisPage.tsx"]
    assert screen_verification_required(instruction, changed) is True


@pytest.mark.parametrize("instruction", INSTRUCTIONS)
def test_one_screen_file_among_local_copies_still_requires_evidence(instruction):
    changed = ["scripts/a.sh.local", "frontend/src/app/page.tsx"]
    assert screen_verification_required(instruction, changed) is True


def test_empty_changed_files_behavior_is_pinned():
    # A screen marker with no file list cannot be exempted; without a marker nothing signals UI work.
    assert screen_verification_required(SCREEN_INSTRUCTION, []) is True
    assert screen_verification_required(NEUTRAL_INSTRUCTION, []) is False
