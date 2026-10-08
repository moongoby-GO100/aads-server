from __future__ import annotations

import base64
from types import SimpleNamespace

import pytest

import app.services.media_generation_service as mgs
from app.services.media_generation_service import (
    MediaGenerationService,
    _classify_provider_exception,
)

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
_MODERATION_MSG = (
    "Error code: 400 - {'error': {'message': 'Your request was rejected by the safety system. "
    "If you believe this is an error, contact us at help.openai.com and include the request ID "
    "req_abc123. safety_violations=[sexual]', 'type': 'image_generation_user_error', "
    "'param': None, 'code': 'moderation_blocked'}}"
)


class _ProviderError(Exception):
    """openai.BadRequestError 와 같은 속성(body/code/request_id)만 흉내낸다."""

    def __init__(self, message, *, body=None, code=None, request_id=None):
        super().__init__(message)
        self.body = body
        self.code = code
        self.request_id = request_id


def _moderation_error(**kw):
    body = {
        "message": "Your request was rejected by the safety system.",
        "type": "image_generation_user_error",
        "code": "moderation_blocked",
        "moderation_stage": "output",
        "moderation_details": {"categories": ["sexual"]},
    }
    return _ProviderError(_MODERATION_MSG, body=body, code="moderation_blocked", **kw)


def test_classify_moderation_body_extracts_details_without_prompt():
    code, extra = _classify_provider_exception(_moderation_error(request_id="req_abc123"))

    assert code == "MODERATION_BLOCKED"
    assert extra == {
        "provider_error_code": "moderation_blocked",
        "moderation_stage": "output",
        "moderation_categories": ["sexual"],
        "provider_request_id": "req_abc123",
    }


def test_classify_moderation_message_only_falls_back_to_regex():
    code, extra = _classify_provider_exception(Exception(_MODERATION_MSG))

    assert code == "MODERATION_BLOCKED"
    assert extra["moderation_categories"] == ["sexual"]
    assert extra["provider_request_id"] == "req_abc123"
    assert "moderation_stage" not in extra


def test_classify_nested_error_body_and_safety_system_wording():
    exc = _ProviderError(
        "rejected by the safety system",
        body={"error": {"code": "moderation_blocked", "moderation_stage": "input"}},
    )
    code, extra = _classify_provider_exception(exc)

    assert code == "MODERATION_BLOCKED"
    assert extra["moderation_stage"] == "input"


@pytest.mark.parametrize(
    "exc",
    [
        RuntimeError("connection reset"),
        ValueError("No image generated from OpenAI"),
        _ProviderError("Error code: 400 - invalid size", body={"code": "invalid_value"}, code="invalid_value"),
        _ProviderError("Error code: 429 - rate limit", code="rate_limit_exceeded"),
    ],
)
def test_classify_other_errors_stay_provider_unavailable(exc):
    assert _classify_provider_exception(exc) == ("PROVIDER_UNAVAILABLE", {})


class _Svc(MediaGenerationService):
    def __init__(self):
        super().__init__(settings_obj=SimpleNamespace(OPENAI_API_KEY="sk-test", GOOGLE_API_KEY=""))
        self.updates: list[dict] = []

    async def _insert_job(self, **kwargs):
        return {"job_id": "job-1", "kind": kwargs["kind"], "provider": kwargs["provider"], "model_id": kwargs["model_id"]}

    async def update_job_status(self, job_id, status, **kwargs):
        self.updates.append({"job_id": job_id, "status": status, **kwargs})
        return {"job_id": job_id, "status": status}


def _route(kind):
    return mgs.MediaRoute(
        kind=kind, provider="openai", model_id="gpt-image-2.5-sunburst", configured=True, supported=True
    )


def _patch_route(monkeypatch, svc, kind):
    async def _resolve(*a, **k):
        return _route(kind)

    monkeypatch.setattr(svc, "resolve_route", _resolve)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error, expected",
    [
        (_moderation_error(request_id="req_abc123"), "MODERATION_BLOCKED"),
        (RuntimeError("upstream timeout"), "PROVIDER_UNAVAILABLE"),
    ],
)
async def test_edit_image_error_code(monkeypatch, error, expected):
    svc = _Svc()
    _patch_route(monkeypatch, svc, "edit_image")

    async def _boom(self, **kwargs):
        raise error

    monkeypatch.setattr(MediaGenerationService, "_edit_openai_image", _boom)
    image_data = base64.b64encode(_PNG).decode()

    result = await svc.edit_image("probe", input_refs={"image_data": image_data})

    assert result["status"] == "failed"
    assert result["error"] == expected
    assert result["error_code"] == expected
    failed = svc.updates[-1]
    assert failed["status"] == "failed"
    assert failed["result_metadata"]["error_code"] == expected
    if expected == "MODERATION_BLOCKED":
        assert failed["result_metadata"]["moderation_stage"] == "output"
        assert failed["result_metadata"]["moderation_categories"] == ["sexual"]
        assert "probe" not in str(failed["result_metadata"])
    else:
        assert "moderation_stage" not in failed["result_metadata"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error, expected",
    [
        (_moderation_error(), "MODERATION_BLOCKED"),
        (RuntimeError("upstream timeout"), "PROVIDER_UNAVAILABLE"),
    ],
)
async def test_generate_image_error_code(monkeypatch, error, expected):
    svc = _Svc()

    async def _boom(self, *a, **k):
        raise error

    monkeypatch.setattr(MediaGenerationService, "_generate_image_with_route", _boom)
    route = mgs.MediaRoute(
        kind="image", provider="openai", model_id="gpt-image-2.5-sunburst",
        configured=True, supported=True, source="explicit",
    )

    async def _resolve(*a, **k):
        return route

    monkeypatch.setattr(svc, "resolve_route", _resolve)

    result = await svc.generate_image("probe")

    assert result["status"] == "failed"
    assert result["error_code"] == expected
    assert svc.updates[-1]["result_metadata"]["error_code"] == expected
