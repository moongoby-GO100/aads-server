"""디자인 채점 비전 체인 Claude → Codex CLI(릴레이 /codex-stream) → Gemini 테스트.
네트워크(httpx)·DB 는 전부 mock."""
import base64
import json
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import design_auditor as da
from app.services import model_selector as ms
from app.services import qa_pipeline as qp

PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg=="
CODEX_MODEL = "gpt-5.5"

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


class _FakeRelayResponse:
    def __init__(self, status_code, lines):
        self.status_code = status_code
        self._lines = lines

    async def aread(self):
        return b"relay-body"

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeRelayClient:
    """httpx.AsyncClient 대역 — /health GET 과 /codex-stream POST 를 기록한다."""

    posts: list = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, **kwargs):
        return SimpleNamespace(status_code=200)

    def stream(self, method, url, json=None, **kwargs):
        type(self).posts.append({"method": method, "url": url, "json": json})
        return type(self).response


def _relay_ok(text):
    lines = [
        json.dumps({"type": "assistant", "subtype": "text", "text": text[:50]}),
        json.dumps({"type": "assistant", "subtype": "text", "text": text[50:]}),
        json.dumps({"type": "result", "result": text, "input_tokens": 10, "output_tokens": 5}),
    ]
    return _FakeRelayResponse(200, lines)


@pytest.fixture
def shot(tmp_path):
    p = tmp_path / "a.png"
    p.write_bytes(base64.b64decode(PNG_B64))
    return str(p)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    monkeypatch.setattr(da.DesignAuditor, "_save_to_memory", AsyncMock(return_value=None))
    monkeypatch.setattr(ms, "_get_codex_cli_fallback_model_from_db", AsyncMock(return_value=CODEX_MODEL))
    monkeypatch.setattr(ms, "_resolve_codex_project", AsyncMock(return_value="AADS"))
    monkeypatch.setattr(ms, "_codex_account_order_for_project", AsyncMock(return_value=None))
    monkeypatch.setattr(ms, "_log_oauth_usage", lambda **kw: None)
    monkeypatch.setattr(ms.httpx, "AsyncClient", _FakeRelayClient)
    _FakeRelayClient.posts = []
    _FakeRelayClient.response = _relay_ok(_GOOD)


async def test_claude_success_does_not_call_codex(monkeypatch, shot):
    monkeypatch.setattr("app.core.anthropic_client.call_llm_with_fallback", AsyncMock(return_value=_GOOD))
    gemini = AsyncMock(return_value=_GOOD)
    monkeypatch.setattr(da, "_call_gemini_vision", gemini)

    result = await da.DesignAuditor().audit_screenshot(shot, "ctx")

    assert result.verdict == "PASS"
    assert result.vision_route == "claude"
    assert result.provider_used == da.CLAUDE_VISION_MODEL
    assert _FakeRelayClient.posts == []
    gemini.assert_not_awaited()


async def test_claude_exception_goes_to_codex_stream_with_image(monkeypatch, shot):
    monkeypatch.setattr(
        "app.core.anthropic_client.call_llm_with_fallback",
        AsyncMock(side_effect=RuntimeError("oauth_not_allowed_for_organization")),
    )
    gemini = AsyncMock(return_value=_GOOD)
    monkeypatch.setattr(da, "_call_gemini_vision", gemini)

    result = await da.DesignAuditor().audit_screenshot(shot, "ctx")

    assert result.verdict == "PASS"
    assert result.vision_route == "codex_cli"
    assert result.provider_used == CODEX_MODEL
    assert result.to_dict()["vision_route"] == "codex_cli"
    gemini.assert_not_awaited()

    assert len(_FakeRelayClient.posts) == 1
    post = _FakeRelayClient.posts[0]
    assert post["url"].endswith("/codex-stream")
    body = post["json"]
    assert body["model"] == CODEX_MODEL
    assert body["image_attachments"] == [
        {"name": "screenshot", "media_type": "image/png", "data": PNG_B64}
    ]
    assert "ctx" in body["messages_text"]


async def test_claude_empty_response_falls_to_codex_for_image_audit(monkeypatch, shot):
    monkeypatch.setattr("app.core.anthropic_client.call_llm_with_fallback", AsyncMock(return_value=None))

    img = await da.DesignAuditor().audit_product_image(shot)

    assert img["error"] is None
    assert img["vision_route"] == "codex_cli"
    assert img["provider_used"] == CODEX_MODEL
    assert len(_FakeRelayClient.posts) == 1


async def test_claude_unparseable_falls_to_codex_for_mobile_audit(monkeypatch, shot):
    monkeypatch.setattr(
        "app.core.anthropic_client.call_llm_with_fallback", AsyncMock(return_value="판독 불가, JSON 없음")
    )

    mob = await da.DesignAuditor().audit_mobile_screen(shot)

    assert mob["error"] is None
    assert mob["vision_route"] == "codex_cli"
    assert len(_FakeRelayClient.posts) == 1


async def test_codex_relay_error_falls_to_gemini(monkeypatch, shot):
    monkeypatch.setattr(
        "app.core.anthropic_client.call_llm_with_fallback", AsyncMock(side_effect=RuntimeError("x"))
    )
    _FakeRelayClient.response = _FakeRelayResponse(500, [])
    monkeypatch.setattr(da, "_call_gemini_vision", AsyncMock(return_value=_GOOD))

    img = await da.DesignAuditor().audit_product_image(shot)

    assert len(_FakeRelayClient.posts) == 1
    assert img["vision_route"] == "gemini"
    assert img["provider_used"] == da.GEMINI_VISION_MODEL


async def test_all_fail_is_unscored_not_auto_fail(monkeypatch, shot):
    monkeypatch.setattr(
        "app.core.anthropic_client.call_llm_with_fallback", AsyncMock(side_effect=RuntimeError("x"))
    )
    _FakeRelayClient.response = _FakeRelayResponse(500, [])
    monkeypatch.setattr(da, "_call_gemini_vision", AsyncMock(side_effect=RuntimeError("403 CONSUMER_SUSPENDED")))
    monkeypatch.setattr(
        qp.visual_qa_service,
        "capture_screenshots",
        AsyncMock(return_value=[SimpleNamespace(success=True, path=shot, page="/", page_name="home")]),
    )
    monkeypatch.setattr(qp, "_save_to_context", AsyncMock(return_value=None))

    out = await qp.run_full_qa("P", "https://x.test", ["/"])

    assert len(_FakeRelayClient.posts) == 1
    assert out["design_verdict"] == "ERROR"
    assert out["design_score"] == 0
    assert out["verdict"] != qp.VERDICT_AUTO_FAIL
    assert out["verdict"] == qp.VERDICT_CONDITIONAL


async def test_codex_oauth_module_is_never_touched(monkeypatch, shot):
    class _Tripwire(ModuleType):
        def __getattr__(self, name):
            raise AssertionError(f"codex_oauth.{name} 접근 금지")

    monkeypatch.setitem(sys.modules, "app.core.codex_oauth", _Tripwire("app.core.codex_oauth"))
    monkeypatch.setattr(
        "app.core.anthropic_client.call_llm_with_fallback", AsyncMock(side_effect=RuntimeError("x"))
    )

    result = await da.DesignAuditor().audit_screenshot(shot)

    assert result.vision_route == "codex_cli"
    with open(da.__file__, encoding="utf-8") as f:
        assert "codex_oauth" not in f.read()
