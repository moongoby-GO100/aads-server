"""Voice STT/TTS service for AADS chat.

The service intentionally does not send chat messages by itself. It only turns
audio into text and text into audio, so the existing chat pipeline keeps owning
model routing, tool execution, tenant checks, and persistence.
"""
from __future__ import annotations

import base64
import inspect
import os
from dataclasses import dataclass
from io import BytesIO
from types import SimpleNamespace
from typing import Any, Awaitable, Callable

from pydantic import SecretStr

try:
    from app.config import settings as app_settings
except Exception:  # pragma: no cover - only used in partial import test envs
    app_settings = SimpleNamespace(OPENAI_API_KEY=SecretStr(""))


DEFAULT_ALLOWED_MIME_TYPES = {
    "audio/mpeg",
    "audio/mp3",
    "audio/mp4",
    "audio/m4a",
    "audio/wav",
    "audio/x-wav",
    "audio/webm",
    "audio/ogg",
}

DEFAULT_MAX_UPLOAD_BYTES = 15 * 1024 * 1024
DEFAULT_MAX_TTS_CHARS = 4096


class VoiceServiceError(Exception):
    """Base class for voice service errors."""


class VoiceProviderNotConfigured(VoiceServiceError):
    """Raised when no usable STT/TTS provider is configured."""


class VoiceValidationError(VoiceServiceError):
    """Raised when the voice request is invalid."""


@dataclass(frozen=True)
class VoiceProviderState:
    provider: str
    stt_configured: bool
    tts_configured: bool
    stt_model: str
    tts_model: str
    max_upload_bytes: int
    max_tts_chars: int
    allowed_mime_types: list[str]


def _secret_value(settings_obj: Any, name: str) -> str:
    value = getattr(settings_obj, name, "")
    if isinstance(value, SecretStr):
        return value.get_secret_value()
    return str(value or "")


async def _default_openai_key_provider() -> str:
    try:
        from app.core.llm_key_provider import get_api_key

        return await get_api_key("OPENAI_API_KEY", "OPENAI_API_KEY")
    except Exception:
        return os.getenv("OPENAI_API_KEY", "")


class VoiceService:
    def __init__(
        self,
        *,
        settings_obj: Any = app_settings,
        key_provider: Callable[[], str | Awaitable[str]] | None = None,
        openai_client_factory: Callable[[str], Any] | None = None,
    ) -> None:
        self.settings = settings_obj
        self._custom_key_provider = key_provider is not None
        self.key_provider = key_provider or _default_openai_key_provider
        self.openai_client_factory = openai_client_factory

    @property
    def stt_model(self) -> str:
        return os.getenv("AADS_VOICE_STT_MODEL", "whisper-1").strip() or "whisper-1"

    @property
    def tts_model(self) -> str:
        return os.getenv("AADS_VOICE_TTS_MODEL", "tts-1").strip() or "tts-1"

    @property
    def tts_voice(self) -> str:
        return os.getenv("AADS_VOICE_TTS_VOICE", "alloy").strip() or "alloy"

    @property
    def max_upload_bytes(self) -> int:
        try:
            return max(1, int(os.getenv("AADS_VOICE_MAX_UPLOAD_BYTES", DEFAULT_MAX_UPLOAD_BYTES)))
        except ValueError:
            return DEFAULT_MAX_UPLOAD_BYTES

    @property
    def max_tts_chars(self) -> int:
        try:
            return max(1, int(os.getenv("AADS_VOICE_MAX_TTS_CHARS", DEFAULT_MAX_TTS_CHARS)))
        except ValueError:
            return DEFAULT_MAX_TTS_CHARS

    @property
    def allowed_mime_types(self) -> set[str]:
        configured = os.getenv("AADS_VOICE_ALLOWED_MIME_TYPES", "")
        if not configured.strip():
            return set(DEFAULT_ALLOWED_MIME_TYPES)
        values = {item.strip().lower() for item in configured.split(",") if item.strip()}
        return values or set(DEFAULT_ALLOWED_MIME_TYPES)

    async def health(self) -> dict[str, Any]:
        configured = bool(await self._get_openai_key())
        state = VoiceProviderState(
            provider="openai",
            stt_configured=configured,
            tts_configured=configured,
            stt_model=self.stt_model,
            tts_model=self.tts_model,
            max_upload_bytes=self.max_upload_bytes,
            max_tts_chars=self.max_tts_chars,
            allowed_mime_types=sorted(self.allowed_mime_types),
        )
        return {
            "ok": True,
            "provider": state.provider,
            "stt": {
                "configured": state.stt_configured,
                "model": state.stt_model,
            },
            "tts": {
                "configured": state.tts_configured,
                "model": state.tts_model,
                "voice": self.tts_voice,
            },
            "limits": {
                "max_upload_bytes": state.max_upload_bytes,
                "max_tts_chars": state.max_tts_chars,
                "allowed_mime_types": state.allowed_mime_types,
            },
        }

    async def transcribe(
        self,
        *,
        filename: str,
        content_type: str | None,
        payload: bytes,
        language: str | None = None,
        prompt: str | None = None,
    ) -> dict[str, Any]:
        content_type = self._validate_audio(filename, content_type, payload)
        api_key = await self._get_openai_key()
        if not api_key:
            raise VoiceProviderNotConfigured("voice_provider_not_configured")

        client = self._make_openai_client(api_key)
        audio_file = BytesIO(payload)
        audio_file.name = filename or "audio.webm"
        kwargs: dict[str, Any] = {
            "model": self.stt_model,
            "file": audio_file,
        }
        if language:
            kwargs["language"] = language
        if prompt:
            kwargs["prompt"] = prompt[:512]

        result = await client.audio.transcriptions.create(**kwargs)
        text = getattr(result, "text", None)
        if text is None and isinstance(result, dict):
            text = result.get("text")
        return {
            "text": str(text or "").strip(),
            "provider": "openai",
            "model": self.stt_model,
            "content_type": content_type,
            "bytes": len(payload),
        }

    async def speech(
        self,
        *,
        text: str,
        voice: str | None = None,
        response_format: str = "mp3",
    ) -> dict[str, Any]:
        sanitized = str(text or "").strip()
        if not sanitized:
            raise VoiceValidationError("text_required")
        if len(sanitized) > self.max_tts_chars:
            raise VoiceValidationError("text_too_long")

        fmt = (response_format or "mp3").strip().lower()
        if fmt not in {"mp3", "opus", "aac", "flac", "wav", "pcm"}:
            raise VoiceValidationError("unsupported_response_format")

        api_key = await self._get_openai_key()
        if not api_key:
            raise VoiceProviderNotConfigured("voice_provider_not_configured")

        client = self._make_openai_client(api_key)
        result = await client.audio.speech.create(
            model=self.tts_model,
            voice=(voice or self.tts_voice),
            input=sanitized,
            response_format=fmt,
        )
        audio_bytes = await self._read_binary_response(result)
        return {
            "audio_base64": base64.b64encode(audio_bytes).decode("ascii"),
            "content_type": self._content_type_for_format(fmt),
            "provider": "openai",
            "model": self.tts_model,
            "voice": voice or self.tts_voice,
            "format": fmt,
            "bytes": len(audio_bytes),
        }

    async def _get_openai_key(self) -> str:
        provided = self.key_provider()
        value = await provided if inspect.isawaitable(provided) else provided
        if self._custom_key_provider:
            return str(value or "").strip()
        return str(value or _secret_value(self.settings, "OPENAI_API_KEY") or "").strip()

    def _make_openai_client(self, api_key: str) -> Any:
        if self.openai_client_factory:
            return self.openai_client_factory(api_key)
        try:
            from openai import AsyncOpenAI
        except Exception as exc:  # pragma: no cover - depends on runtime image
            raise VoiceProviderNotConfigured("openai_sdk_not_installed") from exc
        return AsyncOpenAI(api_key=api_key)

    def _validate_audio(
        self,
        filename: str,
        content_type: str | None,
        payload: bytes,
    ) -> str:
        if not payload:
            raise VoiceValidationError("audio_required")
        if len(payload) > self.max_upload_bytes:
            raise VoiceValidationError("audio_too_large")

        normalized_type = str(content_type or "").split(";")[0].strip().lower()
        allowed = self.allowed_mime_types
        if normalized_type and normalized_type in allowed:
            return normalized_type

        suffix = os.path.splitext(filename or "")[1].strip(".").lower()
        suffix_to_type = {
            "mp3": "audio/mpeg",
            "mpeg": "audio/mpeg",
            "mp4": "audio/mp4",
            "m4a": "audio/m4a",
            "wav": "audio/wav",
            "webm": "audio/webm",
            "ogg": "audio/ogg",
        }
        inferred = suffix_to_type.get(suffix, "")
        if inferred and inferred in allowed:
            return inferred
        raise VoiceValidationError("unsupported_audio_type")

    @staticmethod
    async def _read_binary_response(result: Any) -> bytes:
        if hasattr(result, "read"):
            value = result.read()
            if inspect.isawaitable(value):
                value = await value
            return bytes(value or b"")
        content = getattr(result, "content", None)
        if content is not None:
            return bytes(content)
        if isinstance(result, (bytes, bytearray)):
            return bytes(result)
        raise VoiceServiceError("empty_audio_response")

    @staticmethod
    def _content_type_for_format(fmt: str) -> str:
        return {
            "mp3": "audio/mpeg",
            "opus": "audio/opus",
            "aac": "audio/aac",
            "flac": "audio/flac",
            "wav": "audio/wav",
            "pcm": "audio/pcm",
        }.get(fmt, "application/octet-stream")


voice_service = VoiceService()
