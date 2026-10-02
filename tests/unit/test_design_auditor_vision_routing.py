"""디자인 채점 비전 호출 경로(Claude=anthropic_client 1순위, Gemini=LiteLLM 2순위)와
채점기 장애 시 QA 판정 테스트. 네트워크 호출은 전부 mock."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import design_auditor as da
from app.services import qa_pipeline as qp

PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg=="

_GOOD = json.dumps({
    "scores": {
        k: {"score": 8, "issues": [], "fixes": []}
        for k in ("visual_consistency", "accessibility", "interaction_clarity", "brand_coherence", "polish")
    },
    "total_score": 40,
    "verdict": "PASS",
    "summary": "ok",
    "critical_issues": [],
})


@pytest.fixture
def shot(tmp_path):
    import base64

    p = tmp_path / "a.png"
    p.write_bytes(base64.b64decode(PNG_B64))
    return str(p)


@pytest.fixture(autouse=True)
def _no_memory(monkeypatch):
    monkeypatch.setattr(da.DesignAuditor, "_save_to_memory", AsyncMock(return_value=None))


async def test_claude_success_does_not_call_gemini(monkeypatch, shot):
    llm = AsyncMock(return_value=_GOOD)
    monkeypatch.setattr("app.core.anthropic_client.call_llm_with_fallback", llm)
    gemini = AsyncMock(return_value=_GOOD)
    monkeypatch.setattr(da, "_call_gemini_vision", gemini)

    result = await da.DesignAuditor().audit_screenshot(shot, "ctx")

    assert result.verdict == "PASS"
    gemini.assert_not_awaited()
    kwargs = llm.await_args.kwargs
    assert kwargs["model"] == da.CLAUDE_VISION_MODEL
    block = kwargs["images"][0]
    assert block["type"] == "image" and block["source"]["data"] == PNG_B64
    assert block["source"]["media_type"] == "image/png"
    assert "ctx" in kwargs["prompt"]


async def test_provider_label_is_actual_model_for_all_three_audits(monkeypatch, shot):
    monkeypatch.setattr("app.core.anthropic_client.call_llm_with_fallback", AsyncMock(return_value=_GOOD))
    monkeypatch.setattr(da, "_call_gemini_vision", AsyncMock(return_value=_GOOD))
    saved = AsyncMock(return_value=None)
    monkeypatch.setattr(da.DesignAuditor, "_save_to_memory", saved)
    auditor = da.DesignAuditor()

    await auditor.audit_screenshot(shot)
    assert saved.await_args.args[2] == da.CLAUDE_VISION_MODEL

    img = await auditor.audit_product_image(shot)
    assert img["provider_used"] == da.CLAUDE_VISION_MODEL

    mob = await auditor.audit_mobile_screen(shot)
    assert mob["provider_used"] == da.CLAUDE_VISION_MODEL


async def test_gemini_is_second_and_labelled_gemini(monkeypatch, shot):
    monkeypatch.setattr(
        "app.core.anthropic_client.call_llm_with_fallback", AsyncMock(return_value=None)
    )
    gemini = AsyncMock(return_value=_GOOD)
    monkeypatch.setattr(da, "_call_gemini_vision", gemini)

    img = await da.DesignAuditor().audit_product_image(shot)

    gemini.assert_awaited_once()
    assert img["provider_used"] == da.GEMINI_VISION_MODEL


async def test_both_providers_fail_gives_error_and_not_auto_fail(monkeypatch, shot):
    monkeypatch.setattr(
        "app.core.anthropic_client.call_llm_with_fallback",
        AsyncMock(side_effect=RuntimeError("oauth_not_allowed_for_organization")),
    )
    monkeypatch.setattr(
        da, "_call_gemini_vision", AsyncMock(side_effect=RuntimeError("403 CONSUMER_SUSPENDED"))
    )
    monkeypatch.setattr(
        qp.visual_qa_service,
        "capture_screenshots",
        AsyncMock(return_value=[SimpleNamespace(success=True, path=shot, page="/", page_name="home")]),
    )
    monkeypatch.setattr(qp, "_save_to_context", AsyncMock(return_value=None))

    out = await qp.run_full_qa("P", "https://x.test", ["/"])

    assert out["design_verdict"] == "ERROR"
    assert out["design_score"] == 0
    assert out["verdict"] != qp.VERDICT_AUTO_FAIL
    assert out["verdict"] == qp.VERDICT_CONDITIONAL


async def test_test_fail_is_still_auto_fail_even_when_grader_down(monkeypatch, shot):
    monkeypatch.setattr(
        "app.core.anthropic_client.call_llm_with_fallback", AsyncMock(side_effect=RuntimeError("x"))
    )
    monkeypatch.setattr(da, "_call_gemini_vision", AsyncMock(side_effect=RuntimeError("y")))
    monkeypatch.setattr(
        qp.visual_qa_service,
        "capture_screenshots",
        AsyncMock(return_value=[SimpleNamespace(success=True, path=shot, page="/", page_name="home")]),
    )
    monkeypatch.setattr(qp, "_save_to_context", AsyncMock(return_value=None))

    out = await qp.run_full_qa(
        "P", "https://x.test", ["/"], existing_test_results=[{"status": "fail"}]
    )

    assert out["design_verdict"] == "ERROR"
    assert out["verdict"] == qp.VERDICT_AUTO_FAIL


def test_calc_verdict_low_real_score_still_auto_fail():
    assert qp._calc_verdict("PASS", "PASS", 20, "FAIL") == qp.VERDICT_AUTO_FAIL
    assert qp._calc_verdict("PASS", "PASS", 20) == qp.VERDICT_AUTO_FAIL
    assert qp._calc_verdict("PASS", "PASS", 40, "PASS") == qp.VERDICT_AUTO_PASS
    assert qp._calc_verdict("FAIL", "PASS", 0, "ERROR") == qp.VERDICT_AUTO_FAIL
    assert qp._calc_verdict("PASS", "PASS", 0, "ERROR") == qp.VERDICT_CONDITIONAL
