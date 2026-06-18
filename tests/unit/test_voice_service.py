from __future__ import annotations

import os
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key")

from app.api.voice import router as voice_router
from app.auth import require_internal_admin
from app.services.voice_service import (
    VoiceProviderNotConfigured,
    VoiceService,
    VoiceValidationError,
)


class _FakeTranscriptions:
    async def create(self, **kwargs):
        assert kwargs["model"]
        assert kwargs["file"].read() == b"audio-bytes"
        return SimpleNamespace(text="음성 지시 테스트")


class _FakeSpeechResponse:
    def read(self):
        return b"mp3-bytes"


class _FakeSpeech:
    async def create(self, **kwargs):
        assert kwargs["input"] == "답변 테스트"
        assert kwargs["response_format"] == "mp3"
        return _FakeSpeechResponse()


class _FakeAudio:
    transcriptions = _FakeTranscriptions()
    speech = _FakeSpeech()


class _FakeOpenAIClient:
    audio = _FakeAudio()


@pytest.mark.asyncio
async def test_voice_health_masks_provider_configuration():
    svc = VoiceService(key_provider=lambda: "")

    result = await svc.health()

    assert result["ok"] is True
    assert result["provider"] == "openai"
    assert result["stt"]["configured"] is False
    assert result["tts"]["configured"] is False
    assert "allowed_mime_types" in result["limits"]


@pytest.mark.asyncio
async def test_transcribe_rejects_unsupported_audio_type():
    svc = VoiceService(key_provider=lambda: "sk-test")

    with pytest.raises(VoiceValidationError, match="unsupported_audio_type"):
        await svc.transcribe(
            filename="upload.txt",
            content_type="text/plain",
            payload=b"hello",
        )


@pytest.mark.asyncio
async def test_transcribe_requires_configured_provider():
    svc = VoiceService(key_provider=lambda: "")

    with pytest.raises(VoiceProviderNotConfigured):
        await svc.transcribe(
            filename="voice.webm",
            content_type="audio/webm",
            payload=b"audio-bytes",
        )


@pytest.mark.asyncio
async def test_transcribe_and_speech_use_openai_client_factory():
    svc = VoiceService(
        key_provider=lambda: "sk-test",
        openai_client_factory=lambda _api_key: _FakeOpenAIClient(),
    )

    transcript = await svc.transcribe(
        filename="voice.webm",
        content_type="audio/webm",
        payload=b"audio-bytes",
    )
    speech = await svc.speech(text="답변 테스트")

    assert transcript["text"] == "음성 지시 테스트"
    assert transcript["provider"] == "openai"
    assert speech["audio_base64"] == "bXAzLWJ5dGVz"
    assert speech["content_type"] == "audio/mpeg"


def test_voice_api_health_uses_internal_admin_dependency_override(monkeypatch):
    class _FakeVoiceService:
        async def health(self):
            return {"ok": True, "stt": {"configured": False}, "tts": {"configured": False}}

    import app.api.voice as voice_api

    monkeypatch.setattr(voice_api, "voice_service", _FakeVoiceService())
    app = FastAPI()
    app.dependency_overrides[require_internal_admin] = lambda: {"is_internal_admin": True}
    app.include_router(voice_router, prefix="/api/v1")

    response = TestClient(app).get("/api/v1/voice/health")

    assert response.status_code == 200
    assert response.json()["ok"] is True
