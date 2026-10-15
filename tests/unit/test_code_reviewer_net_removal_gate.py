"""보존 하드 게이트 비율 조건이 제자리 수정(순삭제 소량)을 거짓 차단하던 것의 회귀 고정.

2026-10-16 runner-99f7a85a / runner-2320802f / runner-73754a2a 가 21추가/22삭제
(순삭제 1줄)로 연속 차단됐다. 허용 순삭제 = max(3, 변경 총량의 10%).
"""

import pytest

import app.services.code_reviewer as cr

CODE_PATH = "app/services/foo.py"


def _diff(additions: int, deletions: int, deleted_prefix: str = "old_") -> str:
    lines = [
        f"diff --git a/{CODE_PATH} b/{CODE_PATH}",
        f"--- a/{CODE_PATH}",
        f"+++ b/{CODE_PATH}",
        f"@@ -1,{deletions} +1,{additions} @@",
    ]
    lines += [f"-{deleted_prefix}{i} = {i}" for i in range(deletions)]
    lines += [f"+new_{i} = {i}" for i in range(additions)]
    return "\n".join(lines) + "\n"


def _gate(diff: str):
    return cr._precheck_preservation_gate(diff, "", None)


@pytest.mark.parametrize("additions,deletions", [(21, 22), (18, 19), (1, 1), (10, 13), (40, 44)])
def test_in_place_edit_with_small_net_removal_passes(additions, deletions):
    assert _gate(_diff(additions, deletions)) is None


@pytest.mark.parametrize(
    "additions,deletions",
    [(200, 400), (32, 405), (15, 464), (12, 445), (48, 374), (21, 26), (1, 6)],
)
def test_large_net_removal_stays_blocked(additions, deletions):
    verdict = _gate(_diff(additions, deletions))
    assert verdict is not None
    assert verdict.flag_category == "PRESERVATION_HARD_GATE"
    assert verdict.failure_stage == "pre_llm_preservation_gate"


def test_pure_deletion_stays_blocked():
    verdict = _gate(_diff(0, 5))
    assert verdict is not None
    assert verdict.flag_category == "PRESERVATION_HARD_GATE"


def test_public_function_deletion_stays_blocked_even_with_zero_net_removal():
    diff = (
        f"diff --git a/{CODE_PATH} b/{CODE_PATH}\n"
        f"--- a/{CODE_PATH}\n+++ b/{CODE_PATH}\n"
        "@@ -1,2 +1,2 @@\n"
        "-def public_api(x):\n-    return x\n"
        "+value = 1\n+other = 2\n"
    )
    verdict = _gate(diff)
    assert verdict is not None
    assert verdict.flag_category == "PRESERVATION_HARD_GATE"
    assert "def public_api" in verdict.feedback["deleted_symbols"]


def test_llm_system_prompt_matches_precheck_gate():
    """사전 게이트를 통과하는 21추가/22삭제가 본 LLM 리뷰 프롬프트의 구 50% 규칙으로 반려되면 안 된다."""
    prompt = cr._REVIEW_SYSTEM_PROMPT
    assert "__RATIO_RULE__" not in prompt
    assert cr.PRESERVATION_RATIO_RULE_TEXT in prompt
    assert "순삭제" in prompt
    assert "max(3줄, 변경 총량의 10%)" in prompt
    assert "제자리 수정" in prompt
    # 비율만으로 차단하는 무조건 규칙이 남아 있으면 안 된다.
    assert "50%를 초과하면 preservation" not in prompt
    assert _gate(_diff(21, 22)) is None
