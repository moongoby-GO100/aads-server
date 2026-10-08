from __future__ import annotations

import base64
import sys
import types as pytypes
from types import SimpleNamespace

import pytest

import app.services.media_generation_service as mgs
from app.services.media_generation_service import MediaGenerationService

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
_OUT = b"\x89PNG\r\n\x1a\n" + b"\x01" * 16
_DATA_URI = "data:image/png;base64," + base64.b64encode(_PNG).decode()
_MODEL = "gemini-3.1-flash-image-preview"


class _FakeGenai:
    """google.genai / google.genai.types 를 대신하는 가짜. 호출 기록만 남긴다."""

    def __init__(self, response=None, error=None):
        self.parts: list[tuple[bytes, str]] = []
        self.contents: list = []
        self.response = response
        self.error = error
        outer = self

        class _Part:
            @staticmethod
            def from_bytes(*, data, mime_type):
                outer.parts.append((data, mime_type))
                return ("PART", data, mime_type)

        class _Models:
            def generate_content(self, *, model, contents, config):
                outer.contents = list(contents)
                if outer.error:
                    raise outer.error
                return outer.response

        class _Client:
            def __init__(self, api_key=None):
                self.models = _Models()

        types_mod = pytypes.ModuleType("google.genai.types")
        types_mod.Part = _Part
        types_mod.ImageConfig = lambda **kw: SimpleNamespace(**kw)
        types_mod.GenerateContentConfig = lambda **kw: SimpleNamespace(**kw)
        genai_mod = pytypes.ModuleType("google.genai")
        genai_mod.Client = _Client
        genai_mod.types = types_mod
        google_mod = pytypes.ModuleType("google")
        google_mod.__path__ = []
        google_mod.genai = genai_mod
        self.modules = {"google": google_mod, "google.genai": genai_mod, "google.genai.types": types_mod}


def _image_response():
    part = SimpleNamespace(inline_data=SimpleNamespace(mime_type="image/png", data=_OUT))
    candidate = SimpleNamespace(content=SimpleNamespace(parts=[part]), finish_reason="STOP")
    return SimpleNamespace(candidates=[candidate], prompt_feedback=None)


def _install(monkeypatch, fake: _FakeGenai):
    for name, mod in fake.modules.items():
        monkeypatch.setitem(sys.modules, name, mod)


class _Svc(MediaGenerationService):
    def __init__(self, google_key="g-key"):
        super().__init__(settings_obj=SimpleNamespace(OPENAI_API_KEY="sk-test", GOOGLE_API_KEY=google_key))
        self.updates: list[dict] = []
        self.openai_calls: list[tuple] = []

    async def _insert_job(self, **kwargs):
        return {"job_id": "job-1", "kind": kwargs["kind"], "provider": kwargs["provider"], "model_id": kwargs["model_id"]}

    async def update_job_status(self, job_id, status, **kwargs):
        self.updates.append({"job_id": job_id, "status": status, **kwargs})
        return {"job_id": job_id, "status": status}

    async def _generate_openai_image(self, *args, **kwargs):
        self.openai_calls.append((args, kwargs))
        return {"url": "data:image/png;base64,T1BFTkFJ", "provider": "gpt-image-1", "prompt": args[1]}


class _NoHttp:
    """data URI 경로에서 httpx 가 호출되면 실패시킨다."""

    calls = 0

    def __init__(self, *a, **k):
        _NoHttp.calls += 1
        raise AssertionError("httpx.AsyncClient must not be used for data: URIs")


def _gemini_route(kind):
    return mgs.MediaRoute(
        kind=kind, provider="gemini", model_id=_MODEL, configured=True, supported=True, source="explicit"
    )


def _patch_route(monkeypatch, svc, kind):
    async def _resolve(*a, **k):
        return _gemini_route(kind)

    monkeypatch.setattr(svc, "resolve_route", _resolve)


@pytest.mark.asyncio
async def test_data_uri_reference_goes_to_part_from_bytes_without_http(monkeypatch):
    fake = _FakeGenai(response=_image_response())
    _install(monkeypatch, fake)
    monkeypatch.setattr(mgs.httpx, "AsyncClient", _NoHttp)
    svc = _Svc()

    result = await svc._generate_gemini_native_image(
        "retouch", "retouch", _MODEL, reference_images=[_DATA_URI]
    )

    assert fake.parts == [(_PNG, "image/png")]
    assert _NoHttp.calls == 0
    assert result["url"].startswith("data:image/png;base64,")
    assert svc.openai_calls == []


@pytest.mark.asyncio
async def test_data_uri_without_mime_defaults_to_jpeg(monkeypatch):
    fake = _FakeGenai(response=_image_response())
    _install(monkeypatch, fake)
    svc = _Svc()
    bare = "data:;base64," + base64.b64encode(_PNG).decode()

    await svc._generate_gemini_native_image("p", "p", _MODEL, reference_images=[bare])

    assert fake.parts == [(_PNG, "image/jpeg")]


@pytest.mark.asyncio
async def test_reference_gemini_error_does_not_fall_back_to_openai(monkeypatch):
    fake = _FakeGenai(error=RuntimeError("403 CONSUMER_SUSPENDED"))
    _install(monkeypatch, fake)
    svc = _Svc()
    _patch_route(monkeypatch, svc, "image")

    result = await svc.generate_image("retouch", reference_images=[_DATA_URI], model_id=_MODEL, provider="gemini")

    assert svc.openai_calls == []
    assert result["status"] == "failed"
    assert result["error_code"] == "PROVIDER_UNAVAILABLE"
    assert svc.updates[-1]["status"] == "failed"


@pytest.mark.asyncio
async def test_reference_without_google_key_fails_instead_of_openai(monkeypatch):
    fake = _FakeGenai(response=_image_response())
    _install(monkeypatch, fake)
    svc = _Svc(google_key="")

    with pytest.raises(RuntimeError):
        await svc._generate_gemini_native_image("p", "p", _MODEL, reference_images=[_DATA_URI])

    assert svc.openai_calls == []


@pytest.mark.asyncio
async def test_reference_not_loadable_raises_without_calling_gemini(monkeypatch):
    fake = _FakeGenai(response=_image_response())
    _install(monkeypatch, fake)
    svc = _Svc()

    with pytest.raises(ValueError):
        await svc._generate_gemini_native_image("p", "p", _MODEL, reference_images=["data:image/png;base64,!!!"])

    assert fake.contents == []
    assert svc.openai_calls == []


@pytest.mark.asyncio
async def test_reference_safety_block_is_moderation_blocked(monkeypatch):
    candidate = SimpleNamespace(content=None, finish_reason=SimpleNamespace(name="IMAGE_SAFETY"))
    fake = _FakeGenai(response=SimpleNamespace(candidates=[candidate], prompt_feedback=None))
    _install(monkeypatch, fake)
    svc = _Svc()
    _patch_route(monkeypatch, svc, "image")

    result = await svc.generate_image("retouch", reference_images=[_DATA_URI], model_id=_MODEL, provider="gemini")

    assert svc.openai_calls == []
    assert result["status"] == "failed"
    assert result["error_code"] == "MODERATION_BLOCKED"
    assert svc.updates[-1]["result_metadata"]["moderation_categories"] == ["IMAGE_SAFETY"]


@pytest.mark.asyncio
async def test_text_only_generation_still_falls_back_to_openai(monkeypatch):
    fake = _FakeGenai(error=RuntimeError("boom"))
    _install(monkeypatch, fake)
    svc = _Svc()

    result = await svc._generate_gemini_native_image("draw a cat", "draw a cat", _MODEL)

    assert len(svc.openai_calls) == 1
    assert result["provider"] == "gpt-image-1"


@pytest.mark.asyncio
async def test_text_only_without_key_still_falls_back_to_openai(monkeypatch):
    fake = _FakeGenai(response=_image_response())
    _install(monkeypatch, fake)
    svc = _Svc(google_key="")

    await svc._generate_gemini_native_image("draw a cat", "draw a cat", _MODEL)

    assert len(svc.openai_calls) == 1


@pytest.mark.asyncio
async def test_edit_image_gemini_route_passes_original_bytes_and_succeeds(monkeypatch):
    fake = _FakeGenai(response=_image_response())
    _install(monkeypatch, fake)
    svc = _Svc()
    _patch_route(monkeypatch, svc, "edit_image")

    async def _no_openai_edit(self, **kwargs):
        raise AssertionError("_edit_openai_image must not be used for gemini route")

    monkeypatch.setattr(MediaGenerationService, "_edit_openai_image", _no_openai_edit)
    mask = base64.b64encode(_PNG).decode()

    result = await svc.edit_image(
        "retouch",
        input_refs={"image_data": base64.b64encode(_PNG).decode(), "mask_data": mask},
    )

    assert fake.parts == [(_PNG, "image/png")]
    assert svc.openai_calls == []
    assert result["status"] == "succeeded"
    done = svc.updates[-1]
    assert done["status"] == "succeeded"
    assert done["result_metadata"]["mask_ignored"] is True


@pytest.mark.asyncio
async def test_edit_image_gemini_failure_ends_failed_without_openai(monkeypatch):
    fake = _FakeGenai(error=RuntimeError("403 CONSUMER_SUSPENDED"))
    _install(monkeypatch, fake)
    svc = _Svc()
    _patch_route(monkeypatch, svc, "edit_image")

    result = await svc.edit_image("retouch", input_refs={"image_data": base64.b64encode(_PNG).decode()})

    assert svc.openai_calls == []
    assert result["status"] == "failed"
    assert result["error_code"] == "PROVIDER_UNAVAILABLE"


def test_route_supports_gemini_for_edit_image():
    svc = _Svc()
    assert svc._route_supported("edit_image", "gemini", _MODEL) is True
    assert svc._route_supported("edit_image", "gemini", "gemini-pro-chat") is False
