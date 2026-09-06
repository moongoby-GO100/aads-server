"""P0: review_hold 무한 대기 방지 — code_reviewer.review_code_diff() JSON 파싱 재시도 검증.

scripts/pipeline-runner.sh (실제 운영 러너)는 /api/v1/review/code-diff 호출 결과가
verdict=FLAG + flag_category=REVIEW_PARSER_FAILURE 이면 재시도 없이 즉시 review_hold로
넘긴다. 따라서 파싱 재시도는 review_code_diff() 내부에서 이뤄져야 review_hold 진입
자체를 줄일 수 있다.
"""
import asyncio
import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[2]


def _load_reviewer():
    module_name = "code_reviewer_under_test_parse_retry"
    spec = importlib.util.spec_from_file_location(
        module_name,
        ROOT / "app" / "services" / "code_reviewer.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_review_parse_max_attempts_is_three():
    reviewer = _load_reviewer()
    assert reviewer._REVIEW_PARSE_MAX_ATTEMPTS == 3


def test_review_code_diff_retries_parse_failure_then_recovers():
    asyncio.run(_review_code_diff_retries_parse_failure_then_recovers())


async def _review_code_diff_retries_parse_failure_then_recovers():
    reviewer = _load_reviewer()

    valid_response = """{
      "verdict": "APPROVE",
      "correctness": 0.9,
      "security": 0.9,
      "scope_compliance": 0.9,
      "preservation": 0.9,
      "quality": 0.9,
      "issues": [],
      "summary": "정상"
    }"""
    call_llm = AsyncMock(side_effect=["not json", "still not json", valid_response])

    anthropic_mod = types.ModuleType("app.core.anthropic_client")
    anthropic_mod.call_llm_with_fallback = call_llm
    with patch.dict(
        sys.modules,
        {
            "app": types.ModuleType("app"),
            "app.core": types.ModuleType("app.core"),
            "app.core.anthropic_client": anthropic_mod,
        },
    ), patch.object(
        reviewer, "_get_review_models", new=AsyncMock(return_value=["qwen-turbo"]),
    ), patch.object(
        reviewer, "_save_review_result", new=AsyncMock(),
    ) as mock_save, patch.object(
        reviewer.asyncio, "sleep", new=AsyncMock(),
    ):
        verdict = await reviewer.review_code_diff(
            project="AADS",
            job_id="runner-test-parse-recovers",
            diff="diff --git a/a.py b/a.py\nindex 1111111..2222222 100644\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-print('a')\n+print('b')\n",
            instruction="테스트",
            files_changed=["a.py"],
        )

    assert call_llm.await_count == 3
    assert verdict.verdict == "APPROVE"
    assert verdict.flag_category is None
    mock_save.assert_awaited_once()


def test_review_code_diff_gives_up_after_max_parse_attempts():
    asyncio.run(_review_code_diff_gives_up_after_max_parse_attempts())


async def _review_code_diff_gives_up_after_max_parse_attempts():
    reviewer = _load_reviewer()

    call_llm = AsyncMock(return_value="not json")

    anthropic_mod = types.ModuleType("app.core.anthropic_client")
    anthropic_mod.call_llm_with_fallback = call_llm
    with patch.dict(
        sys.modules,
        {
            "app": types.ModuleType("app"),
            "app.core": types.ModuleType("app.core"),
            "app.core.anthropic_client": anthropic_mod,
        },
    ), patch.object(
        reviewer, "_get_review_models", new=AsyncMock(return_value=["qwen-turbo"]),
    ), patch.object(
        reviewer, "_save_review_result", new=AsyncMock(),
    ) as mock_save, patch.object(
        reviewer.asyncio, "sleep", new=AsyncMock(),
    ):
        verdict = await reviewer.review_code_diff(
            project="AADS",
            job_id="runner-test-parse-exhausted",
            diff="diff --git a/a.py b/a.py\nindex 1111111..2222222 100644\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-print('a')\n+print('b')\n",
            instruction="테스트",
            files_changed=["a.py"],
        )

    # 첫 실패에 즉시 포기하지 않고 정확히 _REVIEW_PARSE_MAX_ATTEMPTS회 호출해야 한다.
    assert call_llm.await_count == reviewer._REVIEW_PARSE_MAX_ATTEMPTS
    assert verdict.verdict == "FLAG"
    assert verdict.flag_category == "REVIEW_PARSER_FAILURE"
    assert verdict.needs_retry is True
    assert verdict.feedback.get("parse_attempts") == reviewer._REVIEW_PARSE_MAX_ATTEMPTS
    mock_save.assert_awaited_once()
