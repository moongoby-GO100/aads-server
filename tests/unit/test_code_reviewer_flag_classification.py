import asyncio
import importlib.util
import sys
import types
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, patch


ROOT = Path(__file__).resolve().parents[2]


def _load_reviewer():
    module_name = "code_reviewer_under_test"
    spec = importlib.util.spec_from_file_location(
        module_name,
        ROOT / "app" / "services" / "code_reviewer.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_review_code_diff_classifies_runner_auth_failure_without_llm():
    asyncio.run(_review_code_diff_classifies_runner_auth_failure_without_llm())


async def _review_code_diff_classifies_runner_auth_failure_without_llm():
    reviewer = _load_reviewer()

    with patch.object(reviewer, "_save_review_result", new=AsyncMock()) as mock_save:
        verdict = await reviewer.review_code_diff(
            project="AADS",
            job_id="runner-test-auth",
            diff='Failed to authenticate. API Error: 401 {"type":"error","error":{"type":"authentication_error","message":"OAuth authentication is currently not supported."}}',
            instruction="테스트",
            files_changed=[],
        )

    assert verdict.verdict == "FLAG"
    assert verdict.flag_category == "RUNNER_AUTH_FAILURE"
    assert verdict.failure_stage == "runner_execution"
    assert verdict.needs_retry is True
    mock_save.assert_awaited_once()


def test_review_code_diff_classifies_invalid_non_diff_input_without_llm():
    asyncio.run(_review_code_diff_classifies_invalid_non_diff_input_without_llm())


async def _review_code_diff_classifies_invalid_non_diff_input_without_llm():
    reviewer = _load_reviewer()

    with patch.object(reviewer, "_save_review_result", new=AsyncMock()) as mock_save:
        verdict = await reviewer.review_code_diff(
            project="AADS",
            job_id="runner-test-invalid-input",
            diff="review failed: no structured diff payload was provided",
            instruction="테스트",
            files_changed=[],
        )

    assert verdict.verdict == "FLAG"
    assert verdict.flag_category == "INVALID_REVIEW_INPUT"
    assert verdict.failure_stage == "input_validation"
    assert verdict.needs_retry is False
    mock_save.assert_awaited_once()


def test_review_code_diff_marks_low_score_as_code_quality_flag():
    asyncio.run(_review_code_diff_marks_low_score_as_code_quality_flag())


async def _review_code_diff_marks_low_score_as_code_quality_flag():
    reviewer = _load_reviewer()

    llm_response = """{
      "verdict": "FLAG",
      "correctness": 0.1,
      "security": 0.2,
      "scope_compliance": 0.2,
      "preservation": 0.2,
      "quality": 0.1,
      "issues": ["실제 코드 문제"],
      "summary": "코드 품질 문제"
    }"""

    anthropic_mod = types.ModuleType("app.core.anthropic_client")
    anthropic_mod.call_llm_with_fallback = AsyncMock(return_value=llm_response)
    with patch.dict(
        sys.modules,
        {
            "app": types.ModuleType("app"),
            "app.core": types.ModuleType("app.core"),
            "app.core.anthropic_client": anthropic_mod,
        },
    ), patch.object(
        reviewer,
        "_get_review_models",
        new=AsyncMock(return_value=["qwen-turbo"]),
    ), patch.object(reviewer, "_save_review_result", new=AsyncMock()) as mock_save:
        verdict = await reviewer.review_code_diff(
            project="AADS",
            job_id="runner-test-quality",
            diff="diff --git a/a.py b/a.py\nindex 1111111..2222222 100644\n--- a/a.py\n+++ b/a.py\n@@ -1 +1,2 @@\n print('a')\n+raise RuntimeError('x')\n",
            instruction="테스트",
            files_changed=["a.py"],
        )

    assert verdict.verdict == "FLAG"
    assert verdict.flag_category == "CODE_QUALITY"
    assert verdict.failure_stage == "review_analysis"
    assert verdict.model_used == "qwen-turbo"
    mock_save.assert_awaited_once()


def test_review_code_diff_holds_when_review_models_return_no_response():
    asyncio.run(_review_code_diff_holds_when_review_models_return_no_response())


async def _review_code_diff_holds_when_review_models_return_no_response():
    reviewer = _load_reviewer()

    anthropic_mod = types.ModuleType("app.core.anthropic_client")
    anthropic_mod.call_llm_with_fallback = AsyncMock(return_value="")
    with patch.dict(
        sys.modules,
        {
            "app": types.ModuleType("app"),
            "app.core": types.ModuleType("app.core"),
            "app.core.anthropic_client": anthropic_mod,
        },
    ), patch.object(
        reviewer,
        "_get_review_models",
        new=AsyncMock(return_value=["qwen-turbo"]),
    ), patch.object(reviewer, "_save_review_result", new=AsyncMock()) as mock_save:
        verdict = await reviewer.review_code_diff(
            project="AADS",
            job_id="runner-test-no-review-response",
            diff="diff --git a/a.py b/a.py\nindex 1111111..2222222 100644\n--- a/a.py\n+++ b/a.py\n@@ -1 +1,2 @@\n print('a')\n+print('b')\n",
            instruction="테스트",
            files_changed=["a.py"],
        )

    assert verdict.verdict == "FLAG"
    assert verdict.flag_category == "REVIEW_MODEL_NO_RESPONSE"
    assert verdict.failure_stage == "review_llm"
    assert verdict.needs_retry is True
    mock_save.assert_awaited_once()


def test_review_code_diff_holds_when_review_response_is_unparseable():
    asyncio.run(_review_code_diff_holds_when_review_response_is_unparseable())


async def _review_code_diff_holds_when_review_response_is_unparseable():
    reviewer = _load_reviewer()

    anthropic_mod = types.ModuleType("app.core.anthropic_client")
    anthropic_mod.call_llm_with_fallback = AsyncMock(return_value="not json")
    with patch.dict(
        sys.modules,
        {
            "app": types.ModuleType("app"),
            "app.core": types.ModuleType("app.core"),
            "app.core.anthropic_client": anthropic_mod,
        },
    ), patch.object(
        reviewer,
        "_get_review_models",
        new=AsyncMock(return_value=["qwen-turbo"]),
    ), patch.object(reviewer, "_save_review_result", new=AsyncMock()) as mock_save:
        verdict = await reviewer.review_code_diff(
            project="AADS",
            job_id="runner-test-parser-failure",
            diff="diff --git a/a.py b/a.py\nindex 1111111..2222222 100644\n--- a/a.py\n+++ b/a.py\n@@ -1 +1,2 @@\n print('a')\n+print('b')\n",
            instruction="테스트",
            files_changed=["a.py"],
        )

    assert verdict.verdict == "FLAG"
    assert verdict.flag_category == "REVIEW_PARSER_FAILURE"
    assert verdict.failure_stage == "review_json_parse"
    assert verdict.needs_retry is True
    mock_save.assert_awaited_once()


def test_preservation_gate_ignores_incidental_instruction_paths():
    reviewer = _load_reviewer()
    instruction = """Use PRD: docs/reports/20260908_langsmith_self_hosted_ohvis_prd.md.
Preserve unrelated dirty files and untracked scripts/reports.
Update HANDOVER.md after implementation.
"""
    diff = """diff --git a/app/main.py b/app/main.py
index 1111111..2222222 100644
--- a/app/main.py
+++ b/app/main.py
@@ -1 +1,2 @@
 existing = True
+feature = True
"""

    verdict = reviewer._precheck_preservation_gate(
        diff,
        instruction,
        ["app/main.py", "HANDOVER.md"],
    )

    assert verdict is None


def test_preservation_gate_uses_explicit_authorized_files_including_root_files():
    reviewer = _load_reviewer()
    instruction = """EXACT AUTHORIZED FILES:
app/main.py
HANDOVER.md
"""
    diff = """diff --git a/HANDOVER.md b/HANDOVER.md
index 1111111..2222222 100644
--- a/HANDOVER.md
+++ b/HANDOVER.md
@@ -1 +1,2 @@
 existing
+new entry
"""

    accepted = reviewer._precheck_preservation_gate(
        diff,
        instruction,
        ["app/main.py", "HANDOVER.md"],
    )
    rejected = reviewer._precheck_preservation_gate(
        diff,
        instruction,
        ["app/main.py", "HANDOVER.md", "app/secret.py"],
    )

    assert accepted is None
    assert rejected is not None
    assert rejected.flag_category == "PRESERVATION_HARD_GATE"
    assert rejected.feedback["allowed_paths"] == ["HANDOVER.md", "app/main.py"]
    assert rejected.feedback["out_of_scope_files"] == ["app/secret.py"]


def _symbol_diff(old, new, path="app/main.py"):
    return (
        f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
        "@@ -1,1 +1,4 @@\n"
        f"-{old}\n+{new}\n+    owner = epoch\n+    alive = True\n+    return owner\n"
    )


@pytest.mark.parametrize("old,new", [
    ("    def _on_resume_done(task):", "    def _on_resume_done(task, epoch=epoch):"),
    ("    def _on_auto_resume_done(task):", "    def _on_auto_resume_done(task, epoch=epoch):"),
    ("        async def _resume_lease_pump():", "    async def _resume_lease_pump():"),
])
def test_private_callback_edit_is_not_symbol_removal(old, new):
    reviewer = _load_reviewer()
    assert reviewer._precheck_preservation_gate(_symbol_diff(old, new), "", None) is None


# 2026-09-18 정책 변경 — 같은 파일 diff 에서 한 번 사라지고 한 번 다시 생긴 **선언**은
# public 이어도 삭제가 아니다(시그니처 재작성). 종전에는 아래 세 쌍이 이 목록에 있었다.
#   ("def public_api():", "def public_api(epoch):")
#   ("class PublicType:", "class PublicType(Base):")
#   ("def __call__(self):", "def __call__(self, epoch):")
# 그 때문에 runner-1f09e9e5(오비서 O3, 553추가/81삭제) · runner-3eeda0e6 ·
# runner-2872d735 가 LLM 리뷰를 받아보지도 못하고 FLAG 0.3 으로 통째로 폐기됐다.
# 인자 추가는 계약 변경이지만 "구현 삭제" 가 아니므로 하드 게이트가 아니라 리뷰가 볼 몫이다.
# 면제 범위는 tests/unit/test_code_reviewer_public_signature_preservation.py 가 고정한다.
# 리네임(+ 쪽 이름 다름) · @router 경로 변경 · async 여부 변경은 여기 그대로 남긴다.
@pytest.mark.parametrize("old,new", [
    ("def _removed():", "def _different():"),
    ('@router.get("/old")', '@router.get("/new")'),
    ("async def _worker():", "def _worker():"),
])
def test_real_or_public_contract_changes_remain_gated(old, new):
    reviewer = _load_reviewer()
    verdict = reviewer._precheck_preservation_gate(_symbol_diff(old, new), "", None)
    assert verdict is not None
    assert verdict.flag_category == "PRESERVATION_HARD_GATE"


def test_private_readdition_in_another_file_cannot_hide_deletion():
    reviewer = _load_reviewer()
    diff = _symbol_diff("def _worker():", "value = 1")
    diff += _symbol_diff("value = 0", "def _worker():", "app/other.py")
    assert reviewer._removed_preservation_symbols(diff) == ["def _worker"]


def test_duplicate_private_names_are_ambiguous_and_remain_gated():
    reviewer = _load_reviewer()
    diff = _symbol_diff("def _worker():", "def _worker(epoch):")
    diff += "-    def _worker():\n+    def _worker(epoch):\n"
    assert reviewer._removed_preservation_symbols(diff) == ["def _worker", "def _worker"]


def test_private_edits_do_not_bypass_deletion_ratio_or_scope_gates():
    reviewer = _load_reviewer()
    diff = _symbol_diff("def _worker():", "def _worker(epoch):")
    # _symbol_diff 는 4추가/1삭제다. 비율 게이트는 순삭제가 있을 때만 걸리므로
    # 삭제 6줄을 더해 4추가/7삭제(순삭제 3줄)로 만든다.
    net_removal = diff + "-old = 1\n-old = 2\n-old = 3\n-old = 4\n-old = 5\n-old = 6\n"
    for candidate, instruction in [
        (net_removal, ""),
        (diff, "EXACT AUTHORIZED FILES: app/other.py"),
    ]:
        verdict = reviewer._precheck_preservation_gate(candidate, instruction, None)
        assert verdict is not None
        assert verdict.flag_category == "PRESERVATION_HARD_GATE"


def test_surgical_in_place_edit_is_not_blocked_by_deletion_ratio():
    """1추가/1삭제 외과적 수정은 기존 구현을 지운 것이 아니므로 통과해야 한다.

    2026-09-16: `deletions > additions * 0.5` 만 보던 시절 `1 > 0.5` 로 항상 참이 돼
    GO100 P0 핫픽스 러너가 연속 차단됐다(runner-ae30c8d2 / runner-8c04cd98).
    """
    reviewer = _load_reviewer()
    one_line = (
        "diff --git a/app/main.py b/app/main.py\n"
        "--- a/app/main.py\n+++ b/app/main.py\n"
        "@@ -1,1 +1,1 @@\n"
        "-    if int(card_id or 0) != 119:\n"
        "+    if int(card_id or 0) not in (119, 310):\n"
    )

    assert reviewer._precheck_preservation_gate(one_line, "", None) is None


def test_balanced_in_place_rewrite_is_not_blocked_by_deletion_ratio():
    """추가 == 삭제 인 제자리 재작성도 순삭제가 없으므로 통과해야 한다."""
    reviewer = _load_reviewer()
    body = "".join(f"-old_{i} = {i}\n+new_{i} = {i}\n" for i in range(10))
    diff = (
        "diff --git a/app/main.py b/app/main.py\n"
        "--- a/app/main.py\n+++ b/app/main.py\n"
        "@@ -1,10 +1,10 @@\n" + body
    )

    assert reviewer._precheck_preservation_gate(diff, "", None) is None


def test_net_removal_still_triggers_deletion_ratio_gate():
    """순삭제가 1줄이라도 있으면 종전대로 차단한다(게이트를 무력화하지 않는다)."""
    reviewer = _load_reviewer()
    diff = (
        "diff --git a/app/main.py b/app/main.py\n"
        "--- a/app/main.py\n+++ b/app/main.py\n"
        "@@ -1,4 +1,3 @@\n"
        "-old_a = 1\n-old_b = 2\n-old_c = 3\n-old_d = 4\n+new_a = 1\n"
    )

    verdict = reviewer._precheck_preservation_gate(diff, "", None)

    assert verdict is not None
    assert verdict.flag_category == "PRESERVATION_HARD_GATE"


def test_private_signature_change_still_requires_semantic_review():
    async def run():
        reviewer = _load_reviewer()
        client = types.ModuleType("app.core.anthropic_client")
        client.call_llm_with_fallback = AsyncMock(return_value='{"verdict":"FLAG",'
            '"correctness":0.1,"security":0.1,"quality":0.1,'
            '"issues":["behavior changed"],"summary":"reject"}')
        with patch.dict(sys.modules, {
            "app": types.ModuleType("app"),
            "app.core": types.ModuleType("app.core"),
            "app.core.anthropic_client": client,
        }), patch.object(reviewer, "_get_review_models", new=AsyncMock(return_value=["qwen-turbo"])), \
                patch.object(reviewer, "_save_review_result", new=AsyncMock()):
            verdict = await reviewer.review_code_diff(
                "AADS", "runner-test-signature-review",
                _symbol_diff("def _worker():", "def _worker(epoch):"), "", ["app/main.py"],
            )
        client.call_llm_with_fallback.assert_awaited_once()
        assert verdict.verdict == "FLAG"
        assert verdict.flag_category == "CODE_QUALITY"

    asyncio.run(run())


def _file_diff(body: str) -> str:
    return (
        "diff --git a/scripts/go100/recalculate_confidence.py"
        " b/scripts/go100/recalculate_confidence.py\n"
        "index 1111111..2222222 100644\n"
        "--- a/scripts/go100/recalculate_confidence.py\n"
        "+++ b/scripts/go100/recalculate_confidence.py\n"
        "@@ -1,10 +1,6 @@\n"
        f"{body}"
    )


def test_deleted_constant_block_does_not_flag_next_context_declaration():
    """상수 블록과 빈 줄만 삭제해도 다음 context 선언이 삭제로 오인되면 안 된다."""
    reviewer = _load_reviewer()
    diff = _file_diff(
        "-DB_CONFIG = {\n"
        '-    "host": "localhost",\n'
        "-}\n"
        "-\n"
        " def get_indicators_from_params(params):\n"
        "     return params\n"
    )

    assert reviewer._removed_preservation_symbols(diff) == []


def test_deleted_constant_block_does_not_trigger_preservation_hard_gate():
    reviewer = _load_reviewer()
    diff = _file_diff(
        "-DB_CONFIG = {\n"
        '-    "host": "localhost",\n'
        "-}\n"
        "-\n"
        " def get_indicators_from_params(params):\n"
        "+    params = dict(params)\n"
        "+    params.setdefault('window', 20)\n"
        "+    params.setdefault('mode', 'fast')\n"
        "+    params.setdefault('source', 'db')\n"
        "+    params.setdefault('debug', False)\n"
        "+    params.setdefault('retry', 3)\n"
        "+    params.setdefault('timeout', 10)\n"
        "+    params.setdefault('limit', 100)\n"
        "+    params.setdefault('offset', 0)\n"
        "     return params\n"
    )

    verdict = reviewer._precheck_preservation_gate(diff, "", None)

    assert verdict is None


@pytest.mark.parametrize("deleted_line, expected", [
    ("-def get_indicators_from_params(params):\n", "def get_indicators_from_params"),
    ("-class ConfidenceRecalculator:\n", "class ConfidenceRecalculator"),
    ("-    async def run_batch(self):\n", "async def run_batch"),
    ('-@router.get("/indicators")\n', "@router.get"),
])
def test_real_symbol_deletions_are_still_detected(deleted_line, expected):
    reviewer = _load_reviewer()

    symbols = reviewer._removed_preservation_symbols(_file_diff(deleted_line))

    assert symbols == [expected]


def _test_file_diff(body, path="tests/unit/test_card310_guard.py"):
    return (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n+++ b/{path}\n"
        "@@ -1,6 +1,6 @@\n"
        f"{body}"
    )


def test_test_function_rename_is_not_symbol_removal():
    """테스트 함수 리네임은 삭제가 아니다.

    2026-09-16: runner-27087189 이 326추가/20삭제 diff 에서
    def test_card310_is_unchanged_by_card119_global_buy_evaluator 하나가
    리네임됐다는 이유로 FLAG(0.30) 처리돼 GO100 #310 P0 작업이 장중 지연됐다.
    """
    reviewer = _load_reviewer()
    diff = _test_file_diff(
        "-def test_card310_is_unchanged_by_card119_global_buy_evaluator():\n"
        "+def test_card310_is_unchanged_by_card119_buy_evaluator():\n"
        "     assert True\n"
    )

    assert reviewer._removed_preservation_symbols(diff) == []
    assert reviewer._precheck_preservation_gate(diff, "", None) is None


def test_test_function_deletion_without_replacement_stays_gated():
    """짝이 되는 새 테스트 함수가 없으면 종전대로 삭제로 본다."""
    reviewer = _load_reviewer()
    diff = _test_file_diff(
        "-def test_card310_is_unchanged_by_card119_global_buy_evaluator():\n"
        "-    assert True\n"
        "+value = 1\n"
    )

    verdict = reviewer._precheck_preservation_gate(diff, "", None)

    assert verdict is not None
    assert verdict.flag_category == "PRESERVATION_HARD_GATE"
    assert verdict.feedback["deleted_symbols"] == [
        "def test_card310_is_unchanged_by_card119_global_buy_evaluator"
    ]


def test_production_public_rename_is_not_treated_as_rename():
    """운영 코드의 public 심볼 리네임은 호출부를 깨므로 계속 게이트에 남는다."""
    reviewer = _load_reviewer()
    verdict = reviewer._precheck_preservation_gate(
        _symbol_diff("def public_api():", "def public_api_v2():"), "", None
    )

    assert verdict is not None
    assert verdict.flag_category == "PRESERVATION_HARD_GATE"


def test_small_diff_below_absolute_floor_is_not_blocked_by_deletion_ratio():
    """1추가/2삭제처럼 줄을 합치는 소규모 수정은 하한 임계치로 면제한다."""
    reviewer = _load_reviewer()
    diff = (
        "diff --git a/app/main.py b/app/main.py\n"
        "--- a/app/main.py\n+++ b/app/main.py\n"
        "@@ -1,2 +1,1 @@\n"
        "-    if a:\n"
        "-        return b\n"
        "+    return b if a else None\n"
    )

    assert reviewer._precheck_preservation_gate(diff, "", None) is None


def test_absolute_floor_does_not_cover_diffs_above_line_budget():
    """하한 임계치는 아주 작은 diff 에만 적용된다(5추가/7삭제=12줄은 종전대로 차단)."""
    reviewer = _load_reviewer()
    body = "".join(f"-old_{i} = {i}\n" for i in range(7))
    body += "".join(f"+new_{i} = {i}\n" for i in range(5))
    diff = (
        "diff --git a/app/main.py b/app/main.py\n"
        "--- a/app/main.py\n+++ b/app/main.py\n"
        "@@ -1,7 +1,5 @@\n" + body
    )

    verdict = reviewer._precheck_preservation_gate(diff, "", None)

    assert verdict is not None
    assert verdict.flag_category == "PRESERVATION_HARD_GATE"
