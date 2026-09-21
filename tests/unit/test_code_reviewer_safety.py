"""Safety regressions for review routing, finite scoring, and redacted evidence."""
import asyncio
import importlib.util
import math
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, patch


ROOT = Path(__file__).resolve().parents[2]


def _load_reviewer():
    module_name = "code_reviewer_under_test_safety"
    spec = importlib.util.spec_from_file_location(
        module_name, ROOT / "app" / "services" / "code_reviewer.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _valid_details(**overrides):
    details = {
        "verdict": "APPROVE",
        "correctness": 0.9,
        "security": 0.9,
        "scope_compliance": 0.9,
        "preservation": 0.9,
        "quality": 0.9,
        "issues": [],
    }
    details.update(overrides)
    return details


def test_review_details_reject_non_finite_and_non_numeric_scores():
    reviewer = _load_reviewer()
    for value in (math.nan, math.inf, -math.inf, "0.9", True, None):
        assert reviewer._validate_review_details(_valid_details(correctness=value)) is None
    assert reviewer._validate_review_details(_valid_details()) is not None


def test_response_evidence_redacts_synthetic_bearer_token_but_keeps_diagnostics():
    reviewer = _load_reviewer()
    secret = "Authorization: Bearer synthetic-test-token"
    text, evidence = reviewer._extract_review_text(secret)

    assert text == secret
    assert "synthetic-test-token" not in evidence["raw_preview"]
    assert evidence["response_chars"] == len(secret)
    assert len(evidence["response_sha256"]) == 64


def test_review_feedback_redacts_nested_synthetic_credentials():
    reviewer = _load_reviewer()
    verdict = reviewer._build_review_verdict(
        verdict="FLAG",
        score=0.2,
        summary="Authorization: Bearer synthetic-test-token",
        issues=["ANTHROPIC_AUTH_TOKEN=synthetic-test-token"],
        feedback={"nested": {"error": "sk-ant-synthetic-test-token"}},
    )

    serialized = str(verdict.feedback) + str(verdict.issues)
    assert "synthetic-test-token" not in serialized
    assert "[REDACTED]" in serialized


def test_review_evidence_redacts_common_header_env_and_oauth_secret_forms():
    reviewer = _load_reviewer()
    samples = (
        "x-api-key: synthetic-x-api-key-value",
        "ANTHROPIC_API_KEY_FALLBACK=synthetic-fallback-value",
        "accessToken=synthetic-access-token-value",
        "refresh_token: synthetic-refresh-token-value",
        "password=synthetic-password-value",
        "Bearer synthetic-oauth-token-value",
        "sk-synthetic-openai-token-value",
        "-----BEGIN OPENSSH PRIVATE KEY-----\nsynthetic-private-key-value",
    )

    for sample in samples:
        sanitized = reviewer._sanitize_review_text(sample, limit=2_000)
        assert "synthetic" not in sanitized
        assert "REDACTED" in sanitized


def test_provider_prefixes_keep_their_transport_contract():
    asyncio.run(_provider_prefixes_keep_their_transport_contract())


async def _provider_prefixes_keep_their_transport_contract():
    reviewer = _load_reviewer()
    configured_call = AsyncMock(return_value="configured")
    directive_mod = types.ModuleType("app.services.directive_draft_service")
    directive_mod._call_configured_model = configured_call
    with patch.dict(sys.modules, {
        "app": types.ModuleType("app"),
        "app.services": types.ModuleType("app.services"),
        "app.services.directive_draft_service": directive_mod,
    }):
        assert await reviewer._call_review_model(model="codex:gpt-5.6-sol", prompt="p", system="s", max_tokens=1) == "configured"
        assert await reviewer._call_review_model(model="claude:claude-haiku", prompt="p", system="s", max_tokens=1) == "configured"
        try:
            await reviewer._call_review_model(model="litellm:gemini-2.5-flash-lite", prompt="p", system="s", max_tokens=1)
        except ValueError as exc:
            assert "not CLI-backed" in str(exc)
        else:
            raise AssertionError("LiteLLM review route must be rejected")

    assert [call.kwargs["model_candidate"] for call in configured_call.await_args_list] == [
        "codex:gpt-5.6-sol", "claude:claude-haiku"
    ]


def test_review_attempt_models_keep_db_order_and_drop_non_cli_routes():
    reviewer = _load_reviewer()
    assert reviewer._review_attempt_models(
        [
            "codex:gpt-5.6-luna",
            "litellm:gemini-2.5-flash-lite",
            "groq-gpt-oss-120b",
            "claude-sonnet-5",
            "codex:gpt-5.6-luna",
        ],
        "",
    ) == ["codex:gpt-5.6-luna", "claude-sonnet-5"]


def test_timeout_empty_malformed_responses_fail_closed_until_valid_response():
    asyncio.run(_timeout_empty_malformed_responses_fail_closed_until_valid_response())


async def _timeout_empty_malformed_responses_fail_closed_until_valid_response():
    reviewer = _load_reviewer()
    valid = '{"verdict":"APPROVE","correctness":0.9,"security":0.9,"scope_compliance":0.9,"preservation":0.9,"quality":0.9,"issues":[]}'
    call_model = AsyncMock(side_effect=[asyncio.TimeoutError(), "", "not json", valid])
    with patch.object(reviewer, "_get_review_models", new=AsyncMock(return_value=[
        "codex:gpt-timeout", "codex:gpt-empty", "codex:gpt-malformed", "codex:gpt-valid",
    ])), patch.object(reviewer, "_call_review_model", new=call_model), patch.object(
        reviewer, "_save_review_result", new=AsyncMock()
    ), patch.object(reviewer.asyncio, "sleep", new=AsyncMock()):
        verdict = await reviewer.review_code_diff(
            project="AADS", job_id="synthetic-safety", instruction="test", files_changed=["a.py"],
            diff="diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1,2 @@\n a = 1\n+b = 2\n",
        )

    assert call_model.await_count == 4
    assert verdict.verdict == "APPROVE"
