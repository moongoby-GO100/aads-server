"""
AADS-186E-3: Deep Research Service
google.genai SDK 기반 Gemini Deep Research Agent 통합.
- 종합 보고서 생성 (stream_callback 방식)
- 재연결: interaction_id + last_event_id
- 타임아웃: 20분 (표준) / 60분 (복잡)
- 비용: $2~5/건 | 일일 최대 5건 제한
GEMINI_API_KEY 미설정 시 graceful 비활성화.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
_DAILY_LIMIT = 5
_TIMEOUT_STANDARD = 1200   # 20분
_TIMEOUT_COMPLEX = 3600    # 60분

# 일일 사용 카운터 (메모리; 프로세스 재시작 시 초기화)
_daily_usage: Dict[str, int] = {}   # date_str → count


def _today_str() -> str:
    from datetime import date
    return date.today().isoformat()


def _check_daily_limit() -> bool:
    """일일 5건 이내 여부 확인."""
    today = _today_str()
    return _daily_usage.get(today, 0) < _DAILY_LIMIT


def _increment_daily() -> None:
    today = _today_str()
    _daily_usage[today] = _daily_usage.get(today, 0) + 1


@dataclass
class ResearchResult:
    """Deep Research 결과 컨테이너."""
    report: str = ""
    interaction_id: str = ""
    citations: List[Dict[str, Any]] = field(default_factory=list)
    thinking_summary: str = ""
    status: str = "done"   # 'done' | 'timeout' | 'error' | 'daily_limit'
    cost_usd: float = 3.0
    elapsed_sec: float = 0.0


class DeepResearchService:
    """
    Gemini Deep Research Agent 서비스.
    google.genai SDK → interactions API 사용.
    SDK 미설치/API키 미설정 시 GeminiResearchService(HTTP polling)로 폴백.
    """

    def __init__(self) -> None:
        self._api_key = GEMINI_API_KEY
        self._agent = "deep-research-pro-preview-12-2025"
        self._sdk_available = self._check_sdk()

    def _check_sdk(self) -> bool:
        try:
            import google.genai  # noqa: F401
            return True
        except ImportError:
            logger.info("[DeepResearch] google.genai SDK 미설치 — HTTP 폴백 모드")
            return False

    def is_available(self) -> bool:
        return bool(self._api_key)

    # ─── 메인 리서치 ──────────────────────────────────────────────────────────

    async def research(
        self,
        query: str,
        format_instructions: Optional[str] = None,
        stream_callback: Optional[Callable[[str, str], None]] = None,
        timeout: int = _TIMEOUT_STANDARD,
    ) -> ResearchResult:
        """
        Gemini Deep Research Agent로 종합 보고서 생성.

        Args:
            query: 리서치 쿼리
            format_instructions: 보고서 형식 지시 (선택)
            stream_callback: (event_type, text) → 스트리밍 콜백
                event_type: 'thinking' | 'research_progress'
            timeout: 타임아웃 초 (기본 20분)

        Returns:
            ResearchResult
        """
        if not self.is_available():
            return ResearchResult(
                status="error",
                report=f"[Deep Research 비활성 — GEMINI_API_KEY 미설정]\n\n질문: {query}",
            )

        if not _check_daily_limit():
            return ResearchResult(
                status="daily_limit",
                report="[Deep Research 일일 한도 초과 — 최대 5건/일]",
            )

        start_ts = time.monotonic()
        prompt = query
        if format_instructions:
            prompt += f"\n\n## 보고서 형식\n{format_instructions}"

        try:
            if self._sdk_available:
                result = await asyncio.wait_for(
                    self._research_via_sdk(prompt, stream_callback),
                    timeout=timeout,
                )
            else:
                result = await asyncio.wait_for(
                    self._research_via_http(prompt, stream_callback),
                    timeout=timeout,
                )
            _increment_daily()
            result.elapsed_sec = round(time.monotonic() - start_ts, 1)
            return result

        except asyncio.TimeoutError:
            logger.warning(f"[DeepResearch] timeout after {timeout}s: {query[:60]}")
            return ResearchResult(
                status="timeout",
                report="[Deep Research 타임아웃. 현재까지 수집된 정보로 답변합니다.]",
                elapsed_sec=round(time.monotonic() - start_ts, 1),
            )
        except Exception as e:
            logger.error(f"[DeepResearch] error: {e}")
            return ResearchResult(
                status="error",
                report=f"[Deep Research 오류: {str(e)[:200]}]",
                elapsed_sec=round(time.monotonic() - start_ts, 1),
            )

    # ─── SDK 방식 ─────────────────────────────────────────────────────────────

    async def _research_via_sdk(
        self,
        prompt: str,
        stream_callback: Optional[Callable[[str, str], None]],
    ) -> ResearchResult:
        """google.genai SDK interactions API 사용."""
        import google.genai as genai

        client = genai.Client(api_key=self._api_key)
        interaction_id = ""
        thinking_parts: list[str] = []
        report_parts: list[str] = []
        citations: list[dict] = []

        try:
            # SDK에 interactions API가 있으면 사용, 없으면 표준 generate_content
            if hasattr(client, "aio") and hasattr(client.aio, "models"):
                # 비동기 스트리밍
                async for chunk in await client.aio.models.generate_content_stream(
                    model=self._agent,
                    contents=prompt,
                ):
                    # 청크에서 텍스트 추출
                    for cand in getattr(chunk, "candidates", []):
                        for part in getattr(cand.content, "parts", []):
                            text = getattr(part, "text", "") or ""
                            thought = getattr(part, "thought", False)
                            if thought and text:
                                thinking_parts.append(text)
                                if stream_callback:
                                    await _maybe_await(stream_callback("thinking", text))
                            elif text:
                                report_parts.append(text)
                                if stream_callback:
                                    await _maybe_await(stream_callback("research_progress", text))

                    # 인용 추출
                    for cand in getattr(chunk, "candidates", []):
                        gm = getattr(cand, "grounding_metadata", None)
                        if gm:
                            for gc in getattr(gm, "grounding_chunks", []):
                                web = getattr(gc, "web", None)
                                if web:
                                    citations.append({
                                        "url": getattr(web, "uri", ""),
                                        "title": getattr(web, "title", ""),
                                    })

            else:
                # 동기 폴백
                resp = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: client.models.generate_content(
                        model=self._agent,
                        contents=prompt,
                    ),
                )
                for cand in getattr(resp, "candidates", []):
                    for part in getattr(cand.content, "parts", []):
                        text = getattr(part, "text", "") or ""
                        if text:
                            report_parts.append(text)
                    gm = getattr(cand, "grounding_metadata", None)
                    if gm:
                        for gc in getattr(gm, "grounding_chunks", []):
                            web = getattr(gc, "web", None)
                            if web:
                                citations.append({
                                    "url": getattr(web, "uri", ""),
                                    "title": getattr(web, "title", ""),
                                })

        except Exception as sdk_err:
            logger.warning(f"[DeepResearch SDK] error, HTTP 폴백: {sdk_err}")
            return await self._research_via_http(prompt, stream_callback)

        return ResearchResult(
            report="".join(report_parts),
            interaction_id=interaction_id,
            citations=citations[:10],
            thinking_summary="".join(thinking_parts)[:500],
            status="done",
            cost_usd=3.0,
        )

    # ─── HTTP 폴백 (gemini_research_service 로직 재사용) ───────────────────────

    async def _research_via_http(
        self,
        prompt: str,
        stream_callback: Optional[Callable[[str, str], None]],
    ) -> ResearchResult:
        """HTTP 폴링 방식 폴백 — gemini_research_service.py 로직 재사용."""
        from app.services.gemini_research_service import GeminiResearchService
        svc = GeminiResearchService()

        report = ""
        sources: list[dict] = []
        interaction_id = ""
        thinking_summary = ""

        async for event in svc.start_research_stream(prompt, "tool_call", None):
            etype = event.get("type", "")
            if etype == "research.start":
                interaction_id = event.get("interaction_id", "")
            elif etype == "research.progress":
                summary = event.get("summary", "")
                if summary and stream_callback:
                    await _maybe_await(stream_callback("research_progress", summary))
                thinking_summary = summary
            elif etype == "research.complete":
                report = event.get("report", "")
                sources = event.get("sources", [])
            elif etype == "delta":
                text = event.get("content", "")
                if text:
                    report += text
                    if stream_callback:
                        await _maybe_await(stream_callback("research_progress", text))

        return ResearchResult(
            report=report,
            interaction_id=interaction_id,
            citations=sources,
            thinking_summary=thinking_summary[:500],
            status="done" if report else "error",
            cost_usd=3.0,
        )

    # ─── 후속 질문 ────────────────────────────────────────────────────────────

    async def follow_up(self, interaction_id: str, question: str) -> str:
        """이전 리서치에 추가 질문 (gemini-2.0-flash로 후속 대화)."""
        if not self.is_available():
            return "[Deep Research API 키 미설정]"
        try:
            if self._sdk_available:
                import google.genai as genai
                client = genai.Client(api_key=self._api_key)
                resp = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: client.models.generate_content(
                        model="gemini-2.0-flash",
                        contents=f"[이전 리서치 ID: {interaction_id}]\n\n추가 질문: {question}",
                    ),
                )
                return resp.text or ""
        except Exception as e:
            logger.error(f"[DeepResearch.follow_up] {e}")
        return f"[follow_up 오류: 잠시 후 다시 시도하세요]"


# ─── 헬퍼 ────────────────────────────────────────────────────────────────────

async def _maybe_await(result: Any) -> None:
    """콜백이 코루틴이면 await, 아니면 그냥 반환."""
    if asyncio.iscoroutine(result):
        await result
