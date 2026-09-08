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


@pytest.mark.parametrize("old,new", [
    ("def _removed():", "def _different():"),
    ("def public_api():", "def public_api(epoch):"),
    ("class PublicType:", "class PublicType(Base):"),
    ('@router.get("/old")', '@router.get("/new")'),
    ("async def _worker():", "def _worker():"),
    ("def __call__(self):", "def __call__(self, epoch):"),
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
    for candidate, instruction in [
        (diff + "-old = 1\n-old = 2\n-old = 3\n", ""),
        (diff, "EXACT AUTHORIZED FILES: app/other.py"),
    ]:
        verdict = reviewer._precheck_preservation_gate(candidate, instruction, None)
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
