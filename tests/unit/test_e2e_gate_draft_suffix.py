"""Draft/backup suffixes must not trigger the screen-evidence gate; real UI files must."""
import pytest

from app.services.e2e_verify import screen_verification_required

SCREEN_INSTRUCTION = "관리 화면 영향 라우팅 정본 설계"


def test_sql_draft_with_docs_and_tests_is_not_screen_work():
    changed = ["docs/a.md", "migrations/drafts/x.sql_draft", "tests/unit/t.py"]
    assert screen_verification_required(SCREEN_INSTRUCTION, changed) is False


def test_static_html_is_still_screen_work():
    assert screen_verification_required(SCREEN_INSTRUCTION, ["app/static/x.html"]) is True


def test_tsx_backup_is_not_screen_work():
    assert screen_verification_required(SCREEN_INSTRUCTION, ["frontend/components/a.tsx.bak"]) is False


@pytest.mark.parametrize("suffix", [".sql_draft", ".md_draft", ".draft", ".bak", ".orig", ".patch", ".diff"])
def test_each_draft_suffix_is_non_rendering(suffix):
    assert screen_verification_required(SCREEN_INSTRUCTION, [f"app/static/x{suffix}"]) is False


def test_screen_file_next_to_draft_still_requires_evidence():
    changed = ["migrations/drafts/x.sql_draft", "frontend/components/a.tsx"]
    assert screen_verification_required(SCREEN_INSTRUCTION, changed) is True
