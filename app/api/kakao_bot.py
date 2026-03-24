"""카카오톡 자동 응답용 AI 엔드포인트 (C안 Phase 1) + PC Agent 배포 API."""
from __future__ import annotations

import io
import logging
import os
import zipfile
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.core.anthropic_client import call_llm_with_fallback

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/kakao-bot", tags=["kakao-bot"])

# PC Agent 배포 관련 상수
PC_AGENT_DIR = Path(__file__).resolve().parent.parent.parent / "pc_agent"
PC_AGENT_VERSION_FILE = PC_AGENT_DIR / "VERSION"
# zip 제외 패턴
_ZIP_EXCLUDE_DIRS = {"__pycache__", ".git", "build_tmp", "dist", ".mypy_cache", ".pytest_cache"}
_ZIP_EXCLUDE_EXTS = {".pyc", ".pyo", ".exe", ".spec"}
# 에이전트 토큰 (환경변수에서 로드, 실제로는 DB 기반으로 확장 가능)
PC_AGENT_SECRET = os.environ.get("PC_AGENT_SECRET", "")


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


# ── PC Agent 배포 API ──────────────────────────────────────────────────


@router.get("/agent/version")
async def agent_version():
    """PC Agent 최신 버전 정보 반환."""
    version = "0.0.0"
    if PC_AGENT_VERSION_FILE.exists():
        version = PC_AGENT_VERSION_FILE.read_text(encoding="utf-8").strip()
    return {
        "version": version,
        "download_url": "/api/v1/kakao-bot/agent/download",
    }


def _build_agent_zip() -> bytes:
    """pc_agent/ 디렉토리를 메모리 내 zip으로 압축.

    __pycache__, .pyc, .git, dist, build_tmp 등 제외.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for file_path in sorted(PC_AGENT_DIR.rglob("*")):
            if file_path.is_dir():
                continue
            # 제외 디렉토리 체크
            rel = file_path.relative_to(PC_AGENT_DIR)
            if any(part in _ZIP_EXCLUDE_DIRS for part in rel.parts):
                continue
            # 제외 확장자 체크
            if file_path.suffix in _ZIP_EXCLUDE_EXTS:
                continue
            # RESULT_ 리포트 파일 제외
            if file_path.name.startswith("RESULT_"):
                continue
            zf.write(file_path, arcname=str(rel))
    return buf.getvalue()


@router.get("/agent/download")
async def agent_download():
    """PC Agent 코드를 zip으로 다운로드.

    pc_agent/ 디렉토리를 압축하여 스트리밍 응답.
    """
    if not PC_AGENT_DIR.exists():
        raise HTTPException(status_code=404, detail="pc_agent 디렉토리가 없습니다")

    try:
        zip_bytes = _build_agent_zip()
    except Exception as e:
        logger.error("agent_download zip 생성 실패: %s", e)
        raise HTTPException(status_code=500, detail="zip 생성 실패")

    version = "unknown"
    if PC_AGENT_VERSION_FILE.exists():
        version = PC_AGENT_VERSION_FILE.read_text(encoding="utf-8").strip()

    return StreamingResponse(
        io.BytesIO(zip_bytes),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="kakaobot-agent-{version}.zip"',
            "Content-Length": str(len(zip_bytes)),
        },
    )


class AgentRegisterRequest(BaseModel):
    """에이전트 등록 요청."""
    agent_token: str = Field(..., description="대시보드에서 발급받은 토큰")
    hostname: str = Field(default="", description="PC 호스트명")
    os_info: str = Field(default="", description="OS 정보")


@router.post("/agent/register")
async def agent_register(req: AgentRegisterRequest):
    """사용자 PC Agent 등록 (토큰 검증).

    토큰이 유효하면 에이전트 정보를 등록하고 WebSocket URL 반환.
    """
    # 토큰 검증
    if not req.agent_token:
        raise HTTPException(status_code=400, detail="토큰이 비어있습니다")

    if PC_AGENT_SECRET and req.agent_token != PC_AGENT_SECRET:
        raise HTTPException(status_code=401, detail="유효하지 않은 토큰입니다")

    logger.info(
        "agent_registered hostname=%s os=%s",
        req.hostname, req.os_info,
    )

    return {
        "status": "registered",
        "websocket_url": "wss://aads.newtalk.kr/api/v1/pc-agent/ws",
        "message": "등록 완료. WebSocket으로 연결하세요.",
    }
