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


def test_review_code_diff_reaches_later_healthy_model():
    asyncio.run(_review_code_diff_reaches_later_healthy_model())


async def _review_code_diff_reaches_later_healthy_model():
    reviewer = _load_reviewer()
    valid_response = """{
      "verdict": "APPROVE",
      "correctness": 0.9,
      "security": 0.9,
      "scope_compliance": 0.9,
      "preservation": 0.9,
      "quality": 0.9,
      "issues": [],
      "summary": "fourth model recovered"
    }"""
    call_model = AsyncMock(side_effect=["", "", "", valid_response])
    with patch.object(
        reviewer,
        "_get_review_models",
        new=AsyncMock(return_value=["bad-1", "bad-2", "bad-3", "codex:gpt-5.6-sol"]),
    ), patch.object(
        reviewer, "_save_review_result", new=AsyncMock(),
    ), patch.object(
        reviewer, "_call_review_model", new=call_model,
    ):
        verdict = await reviewer.review_code_diff(
            project="AADS",
            job_id="runner-test-later-model",
            diff="diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1,2 @@\n a = 1\n+b = 2\n",
            instruction="test",
            files_changed=["a.py"],
        )

    assert call_model.await_count == 4
    assert [call.kwargs["model"] for call in call_model.await_args_list] == [
        "bad-1", "bad-2", "bad-3", "codex:gpt-5.6-sol",
    ]
    assert verdict.verdict == "APPROVE"
    assert verdict.model_used == "codex:gpt-5.6-sol"


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
            diff="diff --git a/a.py b/a.py\nindex 1111111..2222222 100644\n--- a/a.py\n+++ b/a.py\n@@ -1 +1,2 @@\n print('a')\n+print('b')\n",
            instruction="테스트",
            files_changed=["a.py"],
        )

    assert call_llm.await_count == 3
    assert verdict.verdict == "APPROVE"
    assert verdict.flag_category is None
    mock_save.assert_awaited_once()


def test_review_code_diff_preserves_parser_failure_when_later_attempt_has_no_response():
    """P0 회귀: 첫 시도가 파싱 불가 텍스트를 반환하고 마지막 시도가 무응답이면
    REVIEW_MODEL_NO_RESPONSE로 잘못 분류되지 않고 REVIEW_PARSER_FAILURE를 유지해야 한다.

    재시도 루프가 매 시도마다 result_text를 덮어쓰기만 하던 시절에는, 뒤 시도의
    빈 응답이 앞 시도의 "응답은 받았지만 파싱 실패" 사실을 지워버려 최종 판정이
    REVIEW_MODEL_NO_RESPONSE로 오분류됐다.
    """
    asyncio.run(_review_code_diff_preserves_parser_failure_when_later_attempt_has_no_response())


async def _review_code_diff_preserves_parser_failure_when_later_attempt_has_no_response():
    reviewer = _load_reviewer()

    # 시도1: 모델이 응답은 했지만 JSON이 아님 (파싱 실패).
    # 시도2,3: 이후 시도는 타임아웃/빈 응답 ("" -> not result_text 로 skip).
    call_llm = AsyncMock(side_effect=["not json", "", ""])

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
            job_id="runner-test-mixed-response-then-silence",
            diff="diff --git a/a.py b/a.py\nindex 1111111..2222222 100644\n--- a/a.py\n+++ b/a.py\n@@ -1 +1,2 @@\n print('a')\n+print('b')\n",
            instruction="테스트",
            files_changed=["a.py"],
        )

    assert call_llm.await_count == 3
    assert verdict.verdict == "FLAG"
    # 핵심 회귀 assertion: 응답을 한 번이라도 받았으므로 REVIEW_MODEL_NO_RESPONSE가
    # 아니라 REVIEW_PARSER_FAILURE여야 한다.
    assert verdict.flag_category == "REVIEW_PARSER_FAILURE"
    assert verdict.failure_stage == "review_json_parse"
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
            diff="diff --git a/a.py b/a.py\nindex 1111111..2222222 100644\n--- a/a.py\n+++ b/a.py\n@@ -1 +1,2 @@\n print('a')\n+print('b')\n",
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
