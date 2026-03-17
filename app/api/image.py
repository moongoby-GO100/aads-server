"""AADS 이미지 생성 API"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services.image_service import image_service

router = APIRouter()


class ImageRequest(BaseModel):
    prompt: str
    size: str = "1024x1024"


@router.post("/generate")
async def generate_image(req: ImageRequest):
    """채팅창에서 이미지 생성"""
    if not req.prompt.strip():
        raise HTTPException(status_code=400, detail="프롬프트를 입력하세요")
    try:
        result = await image_service.generate(req.prompt, req.size)
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"이미지 생성 실패: {e}")
