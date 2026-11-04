"""리뷰 프롬프트의 '작업 지시' 부분 — 허용 파일은 전문 기준, 본문은 앞 6,000자.

종전에는 `instruction[:500]` 만 넘겨 긴 지시서의 허용 파일·범위 메모가 잘렸다.
하드 게이트는 전문에서 `_extract_explicit_scope_paths` 로 경로를 뽑으므로 리뷰어와
게이트가 서로 다른 범위를 보고 있었다.
"""

import re

import pytest

import app.services.code_reviewer as cr

build = cr._build_review_instruction_section
HEAD = cr._REVIEW_INSTRUCTION_HEAD_CHARS
NO_DECL = "허용 파일 선언 없음"


def _gate_block_paths(section: str) -> set[str]:
    m = re.search(r"^허용 파일\(하드 게이트 인식.*?\):\n((?:- .*\n?)+)", section, re.M)
    if not m:
        return set()
    return {line[2:].strip() for line in m.group(1).splitlines() if line.startswith("- ")}


def _declared_block_paths(section: str) -> set[str]:
    m = re.search(r"^TARGET_FILES 선언\(.*?\):\n((?:- .*\n?)+)", section, re.M)
    if not m:
        return set()
    return {line[2:].strip() for line in m.group(1).splitlines() if line.startswith("- ")}


def test_t1_target_files_after_7000_chars_of_prose():
    instruction = ("설명 문장입니다. " * 600)[:7000] + "\nTARGET_FILES:\n- app/x.py\n- tests/unit/test_x.py\n"
    section = build(instruction)
    assert "app/x.py" in section and "tests/unit/test_x.py" in section
    assert _declared_block_paths(section) == {"app/x.py", "tests/unit/test_x.py"}


def test_t2_target_files_after_long_scope_section():
    instruction = "## 범위\n" + ("범위 설명. " * 700)[:3500] + "\nTARGET_FILES:\n- app/a.py\n- tests/unit/test_a.py\n"
    section = build(instruction)
    assert "app/a.py" in section and "tests/unit/test_a.py" in section


@pytest.mark.parametrize(
    "instruction",
    [
        "허용 파일:\n- app/x.py\n- tests/unit/test_x.py",
        "허용 파일: app/a.py, tests/unit/test_a.py\n\n- app/private.py 는 참고만 하세요.",
        "허용 파일:\n\n- app/x.py",
        "허용 파일:\napp/a.py\ntests/unit/test_a.py",
        "TARGET_FILES:\n\n- app/x.py",
        "TARGET_FILES:\n- app/x.py",
        "본문만 있고 선언이 없다",
    ],
)
def test_t3_gate_block_equals_gate_function(instruction):
    section = build(instruction)
    assert _gate_block_paths(section) == cr._extract_explicit_scope_paths(instruction)


def test_gate_block_is_labelled_zero_when_gate_sees_nothing_but_target_files_is_shown():
    section = build("TARGET_FILES:\napp/a.py\ntests/unit/test_a.py\n")
    assert NO_DECL in section
    assert _declared_block_paths(section) == {"app/a.py", "tests/unit/test_a.py"}


def test_target_files_list_stops_at_blank_line():
    section = build("TARGET_FILES: app/a.py\n\n- app/private.py 는 참고만 하고 수정하지 마세요.")
    assert _declared_block_paths(section) == {"app/a.py"}
    assert "app/private.py" not in _declared_block_paths(section)

    section = build("TARGET_FILES:\n- app/a.py\n\n- app/private.py 는 참고만")
    assert _declared_block_paths(section) == {"app/a.py"}


def test_target_files_inline_value_does_not_absorb_next_bullets():
    section = build("TARGET_FILES: app/a.py\n- app/private.py 는 참고만")
    assert _declared_block_paths(section) == {"app/a.py"}


def test_t4_single_scope_memo_line_before_huge_body():
    memo = "범위 메모: 위 파일만 바꾼다."
    instruction = memo + "\n" + ("본문 " * 40000)
    assert len(instruction) > 120_000
    section = build(instruction)
    assert memo in section
    assert len(section) <= HEAD + 10 * 1000 + 2000
    assert len(section) < 12_000


def test_t5_first_line_over_4000_chars_is_preserved():
    first = "필수 동작:" + ("가" * 4100)
    section = build(first + "\nTARGET_FILES: app/x.py\n")
    assert first in section
    assert "app/x.py" in section


def test_t6_omission_notice_only_when_over_limit():
    short = build("가" * HEAD)
    assert "이하 생략" not in short
    long = build("가" * (HEAD + 1))
    assert "이하 생략" in long
    assert f"전체 {HEAD + 1}자 중 앞 6,000자" in long


@pytest.mark.parametrize("instruction", [None, "", "   "])
def test_t7_empty_instruction(instruction):
    section = build(instruction)
    assert NO_DECL in section
    assert "이하 생략" not in section


def test_t8_scope_memo_line_and_char_caps():
    lines = [f"범위 메모 {i}: " + ("나" * 1500) for i in range(11)]
    section = build("\n".join(lines))
    memo_block = section.split("범위 메모:\n", 1)[1].split("\n\n", 1)[0]
    shown = [ln for ln in memo_block.splitlines() if ln.startswith("- 범위 메모")]
    assert len(shown) == 10
    assert all(len(ln) <= 2 + 1000 for ln in shown)
    assert "범위 메모 11줄 중 10줄 표시" in memo_block
    assert "일부 절단" in memo_block


def test_scope_memo_accepts_indent_and_dash_and_does_not_join_next_line():
    section = build("  - 범위 메모(리뷰어): 위 2개 파일만\n이 줄은 메모가 아니다 SENTINEL_NEXT\n")
    memo_block = section.split("범위 메모:\n", 1)[1].split("\n\n", 1)[0]
    assert "위 2개 파일만" in memo_block
    assert "SENTINEL_NEXT" not in memo_block
    assert "절단" not in memo_block
