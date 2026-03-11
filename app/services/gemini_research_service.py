"""
AADS-185: Gemini Deep Research 서비스
Gemini Interactions API (deep-research-pro-preview) 비동기 리서치.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

GEMINI_API_KEY = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY", "")
_BASE = "https://generativelanguage.googleapis.com"
_AGENT = "deep-research-pro-preview-12-2025"


@dataclass
class ResearchStatus:
    interaction_id: str
    status: str  # 'pending' | 'running' | 'done' | 'failed'
    progress: int = 0  # 0~100
    summary: str = ""
    report: str = ""
    sources: List[Dict[str, Any]] = field(default_factory=list)


class GeminiResearchService:
    """
    Gemini Deep Research API.
    start_research_stream(): SSE 이벤트 생성기로 프론트엔드에 진행 상황 전달.
    비용: $2~$5/건
    """

    def __init__(self) -> None:
        self._api_key = GEMINI_API_KEY

    async def start_research_stream(
        self,
        prompt: str,
        session_id: str,
        db_conn=None,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """
        Deep Research 시작 → SSE 이벤트 생성기.
        Yields:
          {"type": "research.start", "interaction_id": "..."}
          {"type": "research.progress", "progress": 30, "summary": "..."}
          {"type": "research.complete", "report": "...", "sources": [...]}
          {"type": "done", "intent": "deep_research", ...}
        """
        if not self._api_key:
            # API 키 없으면 폴백 텍스트 생성
            yield {"type": "delta", "content": f"[Deep Research 비활성 — GEMINI_API_KEY 미설정]\n\n질문: {prompt}\n\n요약 응답을 제공합니다."}
            yield {"type": "done", "intent": "deep_research", "model": "fallback", "cost": "0"}
            return

        interaction_id = str(uuid.uuid4())

        # 1. 리서치 시작
        yield {"type": "research.start", "interaction_id": interaction_id}

        try:
            # Interactions API 시작 요청
            start_resp = await self._start_research(prompt)
            if "error" in start_resp:
                raise ValueError(start_resp["error"])

            real_id = start_resp.get("name", interaction_id).split("/")[-1]
            interaction_id = real_id

            # research_archive 저장
            if db_conn:
                try:
                    await db_conn.execute(
                        """
                        INSERT INTO research_archive (topic, query, status, interaction_id)
                        VALUES ($1, $2, 'running', $3)
                        ON CONFLICT DO NOTHING
                        """,
                        prompt[:100],
                        prompt,
                        interaction_id,
                    )
                except Exception as e:
                    logger.debug(f"research_archive insert error: {e}")

            # 2. 폴링으로 진행 상황 모니터링 (최대 5분)
            for attempt in range(60):  # 5초 * 60 = 5분
                await asyncio.sleep(5)

                status = await self._get_status(interaction_id)
                state = status.get("state", "")
                progress = status.get("progressPercentage", attempt * 2)

                yield {
                    "type": "research.progress",
                    "progress": min(progress, 95),
                    "summary": status.get("thinkingSummary", f"리서치 진행 중... ({attempt*5}초)"),
                }

                if state == "DONE":
                    report = status.get("response", {}).get("text", "")
                    sources = self._extract_sources(status)

                    # DB 업데이트
                    if db_conn:
                        try:
                            await db_conn.execute(
                                """
                                UPDATE research_archive
                                SET status = 'done', report = $1, updated_at = NOW()
                                WHERE interaction_id = $2
                                """,
                                report[:10000],
                                interaction_id,
                            )
                        except Exception as e:
                            logger.debug(f"research_archive update error: {e}")

                    yield {"type": "research.complete", "report": report, "sources": sources}
                    yield {
                        "type": "done",
                        "intent": "deep_research",
                        "model": "gemini-deep-research",
                        "cost": "3.0",  # 평균 $3/건
                        "interaction_id": interaction_id,
                    }
                    return

                elif state == "FAILED":
                    raise ValueError(f"Deep Research 실패: {status.get('error', 'unknown')}")

            # 타임아웃
            yield {"type": "delta", "content": "[Deep Research 타임아웃 (5분). 현재까지 수집된 정보로 답변합니다.]"}
            yield {"type": "done", "intent": "deep_research", "model": "gemini-deep-research", "cost": "1.0"}

        except Exception as e:
            logger.error(f"gemini_research_service error: {e}")
            yield {"type": "error", "content": f"Deep Research 오류: {str(e)[:200]}"}
            raise

    async def _start_research(self, prompt: str) -> Dict[str, Any]:
        """Gemini Interactions API 시작 요청."""
        url = f"{_BASE}/v1beta/models/{_AGENT}:generateContent?key={self._api_key}"
        payload = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.7},
        }
        async with httpx.AsyncClient(timeout=30.0) as c:
            r = await c.post(url, json=payload)
            if r.status_code != 200:
                # 모델 미지원 시 폴백
                logger.warning(f"deep_research start failed {r.status_code}: {r.text[:200]}")
                return {"error": f"API {r.status_code}"}
            return r.json()

    async def _get_status(self, interaction_id: str) -> Dict[str, Any]:
        """폴링으로 리서치 상태 조회."""
        url = f"{_BASE}/v1beta/operations/{interaction_id}?key={self._api_key}"
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get(url)
            if r.status_code != 200:
                return {"state": "RUNNING"}
            data = r.json()
            if data.get("done"):
                return {"state": "DONE", "response": data.get("response", {})}
            return {"state": "RUNNING", "progressPercentage": data.get("metadata", {}).get("progress", 50)}

    def _extract_sources(self, status: Dict[str, Any]) -> List[Dict[str, Any]]:
        sources = []
        grounding = status.get("response", {}).get("groundingMetadata", {})
        for chunk in grounding.get("groundingChunks", [])[:10]:
            web = chunk.get("web", {})
            if web:
                sources.append({
                    "url": web.get("uri", ""),
                    "title": web.get("title", ""),
                    "favicon": f"https://www.google.com/s2/favicons?domain={web.get('uri', '')}",
                })
        return sources
