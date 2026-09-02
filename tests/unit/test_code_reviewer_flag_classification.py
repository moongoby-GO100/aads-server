import asyncio
import importlib.util
import sys
import types
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
            diff="diff --git a/a.py b/a.py\nindex 1111111..2222222 100644\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-print('a')\n+raise RuntimeError('x')\n",
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
            diff="diff --git a/a.py b/a.py\nindex 1111111..2222222 100644\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-print('a')\n+print('b')\n",
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
            diff="diff --git a/a.py b/a.py\nindex 1111111..2222222 100644\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-print('a')\n+print('b')\n",
            instruction="테스트",
            files_changed=["a.py"],
        )

    assert verdict.verdict == "FLAG"
    assert verdict.flag_category == "REVIEW_PARSER_FAILURE"
    assert verdict.failure_stage == "review_json_parse"
    assert verdict.needs_retry is True
    mock_save.assert_awaited_once()
