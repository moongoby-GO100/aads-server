import pytest
from unittest.mock import AsyncMock, patch


@pytest.mark.asyncio
async def test_review_code_diff_classifies_runner_auth_failure_without_llm():
    from app.services.code_reviewer import review_code_diff

    with patch(
        "app.services.code_reviewer._save_review_result",
        new=AsyncMock(),
    ) as mock_save:
        verdict = await review_code_diff(
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


@pytest.mark.asyncio
async def test_review_code_diff_classifies_invalid_non_diff_input_without_llm():
    from app.services.code_reviewer import review_code_diff

    with patch(
        "app.services.code_reviewer._save_review_result",
        new=AsyncMock(),
    ) as mock_save:
        verdict = await review_code_diff(
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


@pytest.mark.asyncio
async def test_review_code_diff_marks_low_score_as_code_quality_flag():
    from app.services.code_reviewer import review_code_diff

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

    with patch(
        "app.core.anthropic_client.call_llm_with_fallback",
        new=AsyncMock(return_value=llm_response),
    ), patch(
        "app.services.code_reviewer._get_review_models",
        new=AsyncMock(return_value=["qwen-turbo"]),
    ), patch(
        "app.services.code_reviewer._save_review_result",
        new=AsyncMock(),
    ) as mock_save:
        verdict = await review_code_diff(
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
