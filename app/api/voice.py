"""AADS voice command API."""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from app.auth import require_internal_admin
from app.services.voice_service import (
    VoiceProviderNotConfigured,
    VoiceServiceError,
    VoiceValidationError,
    voice_service,
)

router = APIRouter(prefix="/voice", dependencies=[Depends(require_internal_admin)])


class SpeechRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=4096)
    voice: str | None = Field(default=None, max_length=64)
    response_format: str = Field(default="mp3", max_length=16)


@router.get("/health")
async def voice_health():
    """Return masked STT/TTS readiness without exposing API keys."""
    return await voice_service.health()


@router.post("/transcribe")
async def transcribe_audio(
    audio: UploadFile = File(...),
    language: str | None = Form(default=None),
    prompt: str | None = Form(default=None),
):
    """Transcribe uploaded audio into text for the existing chat pipeline."""
    try:
        payload = await audio.read()
        return await voice_service.transcribe(
            filename=audio.filename or "audio.webm",
            content_type=audio.content_type,
            payload=payload,
            language=language,
            prompt=prompt,
        )
    except VoiceValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except VoiceProviderNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except VoiceServiceError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/speech")
async def speech(req: SpeechRequest):
    """Generate speech audio from assistant text."""
    try:
        return await voice_service.speech(
            text=req.text,
            voice=req.voice,
            response_format=req.response_format,
        )
    except VoiceValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except VoiceProviderNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except VoiceServiceError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
