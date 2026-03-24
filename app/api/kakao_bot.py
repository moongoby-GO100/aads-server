"""카카오톡 자동 응답용 AI 엔드포인트 (C안 Phase 1)."""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.core.anthropic_client import call_llm_with_fallback

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/kakao-bot", tags=["kakao-bot"])


class KakaoBotRequest(BaseModel):
    room: str = Field(..., description="채팅방 이름")
    message: str = Field(..., description="수신 메시지")
    sender: str = Field(default="", description="발신자 이름")
    context: str = Field(default="", description="추가 컨텍스트")
    tone: str = Field(default="friendly", description="톤앤매너: friendly, formal, casual, witty")
    max_length: int = Field(default=200, ge=10, le=500)


class KakaoBotResponse(BaseModel):
    reply: str
    model_used: str
    tokens_used: int


@router.post("/respond", response_model=KakaoBotResponse)
async def kakao_bot_respond(req: KakaoBotRequest):
    """카카오톡 메시지에 대한 AI 응답 생성."""

    tone_prompts = {
        "friendly": "친근하고 따뜻하게",
        "formal": "정중하고 격식있게",
        "casual": "편하고 자연스럽게 반말로",
        "witty": "재치있고 유머러스하게",
    }
    tone_desc = tone_prompts.get(req.tone, tone_prompts["friendly"])

    system_prompt = (
        f"너는 카카오톡에서 대화하는 사람이야.\n"
        f"{tone_desc} 답변해.\n"
        f"- 답변은 {req.max_length}자 이내로 짧게\n"
        f"- 이모티콘/이모지 적절히 사용\n"
        f"- AI라는 티를 내지 마\n"
        f"- 자연스러운 한국어 구어체 사용\n"
        f"- 질문에는 구체적으로 답하고, 안부에는 따뜻하게 반응"
    )

    if req.context:
        system_prompt += f"\n\n추가 맥락: {req.context}"

    user_msg = f"[{req.sender}] {req.message}" if req.sender else req.message

    try:
        # call_llm_with_fallback: prompt(str), model, max_tokens, system
        result = await call_llm_with_fallback(
            prompt=user_msg,
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            system=system_prompt,
        )

        if result is None:
            raise HTTPException(status_code=503, detail="AI 응답 생성 실패 (모든 LLM 폴백 소진)")

        return KakaoBotResponse(
            reply=result,
            model_used="haiku",
            tokens_used=0,  # call_llm_with_fallback은 텍스트만 반환
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("kakao_bot AI 응답 실패: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
