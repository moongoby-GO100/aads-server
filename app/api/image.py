"""AADS media generation API."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services.media_generation_service import media_generation_service

router = APIRouter()


class ImageRequest(BaseModel):
    prompt: str
    size: str = "1024x1024"
    model_id: str | None = None
    provider: str | None = None
    requested_by: str | None = None
    session_id: str | None = None


class EditImageRequest(BaseModel):
    prompt: str
    image_path: str | None = None
    image_url: str | None = None
    image_data: str | None = None
    mask_path: str | None = None
    size: str = "1024x1024"
    model_id: str | None = "gpt-image-2"
    provider: str | None = None
    requested_by: str | None = None
    session_id: str | None = None


class VideoRequest(BaseModel):
    prompt: str
    input_refs: dict = Field(default_factory=dict)
    model_id: str | None = "sora-2"
    provider: str | None = None
    requested_by: str | None = None
    session_id: str | None = None


class VideoDownloadRequest(BaseModel):
    output_dir: str | None = None


@router.post("/generate")
async def generate_image(req: ImageRequest):
    """채팅창에서 이미지 생성"""
    if not req.prompt.strip():
        raise HTTPException(status_code=400, detail="프롬프트를 입력하세요")
    try:
        result = await media_generation_service.generate_image(
            req.prompt,
            req.size,
            model_id=req.model_id,
            provider=req.provider,
            requested_by=req.requested_by,
            session_id=req.session_id,
        )
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"이미지 생성 실패: {e}")


@router.post("/edit")
async def edit_image(req: EditImageRequest):
    """이미지 편집 job 생성/실행."""
    if not req.prompt.strip():
        raise HTTPException(status_code=400, detail="프롬프트를 입력하세요")
    input_refs = {
        key: value
        for key, value in {
            "image_path": req.image_path,
            "image_url": req.image_url,
            "image_data": req.image_data,
            "mask_path": req.mask_path,
        }.items()
        if value
    }
    try:
        return await media_generation_service.edit_image(
            req.prompt,
            input_refs=input_refs,
            size=req.size,
            model_id=req.model_id,
            provider=req.provider,
            requested_by=req.requested_by,
            session_id=req.session_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"이미지 편집 실패: {e}")


@router.post("/video/generate")
async def generate_video(req: VideoRequest):
    """비동기 동영상 생성 job 생성."""
    if not req.prompt.strip():
        raise HTTPException(status_code=400, detail="프롬프트를 입력하세요")
    try:
        return await media_generation_service.generate_video(
            req.prompt,
            input_refs=req.input_refs,
            model_id=req.model_id,
            provider=req.provider,
            requested_by=req.requested_by,
            session_id=req.session_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"동영상 생성 실패: {e}")


@router.get("/video/{job_id}/status")
async def video_status(job_id: str):
    """동영상 생성 job 상태 조회."""
    return await media_generation_service.video_status(job_id)


@router.post("/video/{job_id}/download")
async def video_download(job_id: str, req: VideoDownloadRequest | None = None):
    """동영상 생성 결과를 안전 경로에 저장하고 메타데이터 반환."""
    return await media_generation_service.video_download(
        job_id,
        output_dir=req.output_dir if req else None,
    )
