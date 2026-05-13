"""로컬 미디어 API 라우터 — 임베딩·OCR (Whisper는 PC 별도 설치 후 추가)"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Union

from app.core.local_embedding_bridge import embed
from app.core.local_ocr_bridge import ocr_extract

router = APIRouter(prefix="/api/v1/local", tags=["local-media"])


class EmbedRequest(BaseModel):
    input: Union[str, list[str]]
    model: str = "qwen3-embedding:0.6b"


class OCRRequest(BaseModel):
    image_url: str | None = None
    image_base64: str | None = None
    language: str = "kor+eng"


@router.post("/embed")
async def local_embed(req: EmbedRequest):
    try:
        result = await embed(req.input, req.model)
        return result
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/ocr")
async def local_ocr(req: OCRRequest):
    try:
        result = await ocr_extract(req.image_url, req.image_base64, req.language)
        return result
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.get("/status")
async def local_status():
    """로컬 PC 브릿지 상태 확인"""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            r = await client.get("http://127.0.0.1:8102/api/v1/pc-agent/health")
            data = r.json()
            return {
                "pc_agent_connected": data.get("connected", 0) > 0,
                "agents": data.get("agents", []),
                "services": ["embed (qwen3-embedding:0.6b)", "embed (bge-m3)", "ocr"],
            }
    except Exception as e:
        return {"pc_agent_connected": False, "error": str(e)}
