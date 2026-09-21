"""P0: 검수 인프라 예산 가드 — 2026-09-15 review_hold 누적 사고의 회귀 방지.

사고 요약. 시도당 상한이 프록시 한도에서 거꾸로 계산돼 25초까지 내려갔는데,
실제 검수 지연은 유휴 상태에서도 claude-opus-5 20.0초 / claude-haiku-4-5 25.5초였다
(runner-2a202a8e 의 실제 리뷰 프롬프트 10,602자로 실측). 릴레이에 세션이 쌓이면
등록된 네 모델이 전부 상한에 잘려 REVIEW_MODEL_NO_RESPONSE 가 됐고, 재검수
스위퍼는 첫 실패에서 배치를 멈추고 재시도 카운터도 올리지 않아 GO100 5건이
최대 200분 묶였다.

여기서 지키는 것은 셋이다.
  1. 시도당 상한은 실측 지연보다 넉넉해야 한다.
  2. 남은 예산이 있으면 버리지 말고 다음 모델에 쓴다.
  3. 인프라 실패는 재시도 예산을 실제로 소비하고, 회로는 연속 실패에만 열린다.
"""
import asyncio
import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[2]

VALID_REVIEW_JSON = """{
  "verdict": "APPROVE",
  "correctness": 0.9,
  "security": 0.9,
  "scope_compliance": 0.9,
  "preservation": 0.9,
  "quality": 0.9,
  "issues": [],
  "summary": "second model answered inside the remaining budget"
}"""

SAMPLE_DIFF = (
    "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n"
    "@@ -1 +1,2 @@\n a = 1\n+b = 2\n"
)


def _load_reviewer():
    module_name = "code_reviewer_under_test_budget_guards"
    spec = importlib.util.spec_from_file_location(
        module_name,
        ROOT / "app" / "services" / "code_reviewer.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_attempt_timeout_clears_measured_review_latency():
    """25초는 실측 지연(opus 20.0s / haiku 25.5s)과 겹친다. 다시 내리지 마라."""
    reviewer = _load_reviewer()
    assert reviewer._REVIEW_LLM_TIMEOUT_SEC >= 45
    assert reviewer._REVIEW_MIN_ATTEMPT_SEC <= reviewer._REVIEW_LLM_TIMEOUT_SEC


def test_async_path_gets_a_longer_deadline_than_the_proxy_bound_sync_path():
    """비동기 경로는 202+폴링이라 Cloudflare 마감에 묶이지 않는다."""
    reviewer = _load_reviewer()
    assert reviewer._REVIEW_ASYNC_DEADLINE_SEC > reviewer._REVIEW_TOTAL_DEADLINE_SEC


def test_async_review_request_passes_the_longer_deadline():
    source = _read("app/api/code_review.py")
    assert "deadline_sec=_REVIEW_ASYNC_DEADLINE_SEC" in source


def test_cli_review_models_use_the_configured_model_relay():
    asyncio.run(_cli_review_models_use_the_configured_model_relay())


async def _cli_review_models_use_the_configured_model_relay():
    reviewer = _load_reviewer()
    relay = AsyncMock(return_value="ok")
    central = AsyncMock(return_value="wrong-route")
    directive_module = types.ModuleType("app.services.directive_draft_service")
    directive_module._call_configured_model = relay
    anthropic_module = types.ModuleType("app.core.anthropic_client")
    anthropic_module.call_llm_with_fallback = central

    with patch.dict(
        sys.modules,
        {
            "app.services.directive_draft_service": directive_module,
            "app.core.anthropic_client": anthropic_module,
        },
    ):
        result = await reviewer._call_review_model(
            model="codex:gpt-5.6-sol", prompt="review", system="system", max_tokens=64,
        )

    assert result == "ok"
    relay.assert_awaited_once_with(
        model_candidate="codex:gpt-5.6-sol",
        prompt="review",
        max_tokens=64,
        system="system",
        tenant_id=None,
        user_id=None,
    )
    central.assert_not_awaited()


def test_litellm_review_models_stay_on_central_r_auth():
    asyncio.run(_litellm_review_models_stay_on_central_r_auth())


async def _litellm_review_models_stay_on_central_r_auth():
    reviewer = _load_reviewer()
    central = AsyncMock(return_value="ok")
    anthropic_module = types.ModuleType("app.core.anthropic_client")
    anthropic_module.call_llm_with_fallback = central

    with patch.dict(sys.modules, {"app.core.anthropic_client": anthropic_module}):
        result = await reviewer._call_review_model(
            model="litellm:gemini-2.5-flash-lite", prompt="review", system="system", max_tokens=64,
        )

    assert result == "ok"
    central.assert_awaited_once_with(
        prompt="review",
        model="gemini-2.5-flash-lite",
        system="system",
        max_tokens=64,
    )


def test_review_failover_reaches_independent_provider_before_cli_budget_is_exhausted():
    """The 240s async budget must reach central R-AUTH after one 90s timeout."""
    reviewer = _load_reviewer()

    models = reviewer._review_attempt_models(
        [
            "codex:gpt-5.6-luna",
            "claude-sonnet-5",
            "claude-haiku",
            "codex:gpt-5.6-sol",
            "groq-gpt-oss-120b",
        ],
        "",
    )

    assert models[:3] == [
        "codex:gpt-5.6-luna",
        "claude-haiku-4-5-20251001",
        "litellm:gemini-2.5-flash-lite",
    ]


def test_review_failover_deduplicates_configured_litellm_candidate():
    reviewer = _load_reviewer()

    models = reviewer._review_attempt_models(
        ["codex:gpt-5.6-luna", "litellm:gemini-2.5-flash-lite", "claude-sonnet-5"],
        "",
    )

    assert models.count("litellm:gemini-2.5-flash-lite") == 1


def test_remaining_budget_is_spent_instead_of_breaking_early():
    asyncio.run(_remaining_budget_is_spent_instead_of_breaking_early())


async def _remaining_budget_is_spent_instead_of_breaking_early():
    """첫 모델이 상한을 다 쓴 뒤에도 남은 예산으로 두 번째 모델을 부른다.

    예전 가드는 `남은 시간 < 시도당 상한` 이면 바로 break 했다. 아래 조건
    (상한 2초, 마감 3초)에서 첫 시도가 2초를 태우면 옛 코드는 2+2>3 이라 두 번째
    모델을 부르지 않고 무응답으로 닫혔다. 새 가드는 남은 ~1초를 그대로 쓴다.
    """
    reviewer = _load_reviewer()

    attempted: list[str] = []

    async def call_model(*, model, **_kwargs):
        attempted.append(model)
        if len(attempted) == 1:
            # 첫 모델은 시도당 상한을 통째로 태운다.
            await asyncio.sleep(10)
        return VALID_REVIEW_JSON

    with patch.object(reviewer, "_REVIEW_LLM_TIMEOUT_SEC", 2), patch.object(
        reviewer, "_REVIEW_MIN_ATTEMPT_SEC", 0.5
    ), patch.object(
        reviewer,
        "_get_review_models",
        new=AsyncMock(return_value=["slow-model", "fast-model"]),
    ), patch.object(
        reviewer, "_save_review_result", new=AsyncMock()
    ), patch.object(
        reviewer, "_call_review_model", new=call_model
    ):
        verdict = await reviewer.review_code_diff(
            project="AADS",
            job_id="runner-test-budget-guard",
            diff=SAMPLE_DIFF,
            instruction="test",
            files_changed=["a.py"],
            deadline_sec=3,
        )

    assert attempted == ["slow-model", "claude-haiku-4-5-20251001"]
    assert verdict.verdict == "APPROVE"


def test_first_model_cannot_consume_the_entire_sync_deadline():
    asyncio.run(_first_model_cannot_consume_the_entire_sync_deadline())


async def _first_model_cannot_consume_the_entire_sync_deadline():
    reviewer = _load_reviewer()
    attempted: list[str] = []

    async def call_model(*, model, **_kwargs):
        attempted.append(model)
        if model == "slow-model":
            await asyncio.sleep(10)
        return VALID_REVIEW_JSON

    with patch.object(reviewer, "_REVIEW_LLM_TIMEOUT_SEC", 2), patch.object(
        reviewer, "_REVIEW_MIN_ATTEMPT_SEC", 1
    ), patch.object(
        reviewer,
        "_get_review_models",
        new=AsyncMock(return_value=["slow-model", "fast-model"]),
    ), patch.object(
        reviewer, "_save_review_result", new=AsyncMock()
    ), patch.object(
        reviewer, "_call_review_model", new=call_model
    ):
        verdict = await reviewer.review_code_diff(
            project="AADS",
            job_id="runner-test-fallback-reserve",
            diff=SAMPLE_DIFF,
            instruction="test",
            files_changed=["a.py"],
            deadline_sec=4,
        )

    assert attempted == ["slow-model", "claude-haiku-4-5-20251001"]
    assert verdict.verdict == "APPROVE"


def test_sweeper_consumes_retry_budget_on_infra_failure():
    """카운터를 올리지 않으면 백오프도 상한도 영원히 오지 않는다."""
    script = _read("scripts/review-hold-sweeper.sh")
    infra_retry = script[script.index("infra_retry() {"):script.index(
        "# 모델 재검수가 상한에 닿으면"
    )]

    assert "review_retry_count=${nxt}" in infra_retry
    # 옛 로그 문구. 이게 다시 보이면 예산을 보존한 채 배치를 멈추고 있다는 뜻이다
    # — 21회 연속 scanned=1 retried=0 을 만든 그 경로다. HTTP 자체가 도달하지
    # 못한 별도 분기는 예산을 보존하므로 infra_retry 함수만 검사한다.
    assert "retry budget preserved; batch stopped" not in infra_retry


def test_sweeper_circuit_opens_only_after_consecutive_infra_failures():
    script = _read("scripts/review-hold-sweeper.sh")

    assert "SWEEP_INFRA_CIRCUIT" in script
    assert '"$consec_infra" -ge "$SWEEP_INFRA_CIRCUIT"' in script
    # 판정을 받아냈으면 연속 실패 카운터는 초기화된다.
    assert "consec_infra=0" in script
    # 인프라 실패 한 건이 배치 전체를 끝내지 않는다.
    infra_block = script[script.index("if [[ -n \"$infra_reason\" ]]; then"):]
    assert "continue" in infra_block[: infra_block.index("# 판정을 받아냈다")]
