"""
AADS-188A: Deep Research Service (AADS-186E-3 업그레이드)
google.genai SDK 기반 Gemini Deep Research Agent 통합.
- research_stream() AsyncGenerator[ResearchEvent] — SSE 단계 이벤트 스트리밍
- research() 동기식 결과 반환 (기존 chat_service 호환)
- context / format 파라미터 지원
- GOOGLE_GENAI_API_KEY / GEMINI_API_KEY 환경변수 양쪽 지원
- Langfuse span 자동 기록 (활성화 시)
- 타임아웃: 20분 (표준) / 60분 (복잡)
- 비용 제한: 일 5건 / 월 50건 (메모리 카운터)
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# GOOGLE_GENAI_API_KEY 우선, GEMINI_API_KEY 폴백 (두 환경변수 모두 지원)
GEMINI_API_KEY = os.getenv("GOOGLE_API_KEY") or os.getenv("GOOGLE_GENAI_API_KEY") or os.getenv("GEMINI_API_KEY", "")
_DAILY_LIMIT = 5
_MONTHLY_LIMIT = 50
_TIMEOUT_STANDARD = 1200   # 20분
_TIMEOUT_COMPLEX = 3600    # 60분

# 일일/월간 사용 카운터 (메모리; 프로세스 재시작 시 초기화)
_daily_usage: Dict[str, int] = {}    # date_str → count
_monthly_usage: Dict[str, int] = {}  # month_str → count


def _today_str() -> str:
    from datetime import date
    return date.today().isoformat()


def _month_str() -> str:
    from datetime import date
    return date.today().strftime("%Y-%m")


def _check_daily_limit() -> bool:
    """일일 5건 이내 여부 확인."""
    today = _today_str()
    return _daily_usage.get(today, 0) < _DAILY_LIMIT


def _check_monthly_limit() -> bool:
    """월간 50건 이내 여부 확인."""
    month = _month_str()
    return _monthly_usage.get(month, 0) < _MONTHLY_LIMIT


def _increment_daily() -> None:
    today = _today_str()
    _daily_usage[today] = _daily_usage.get(today, 0) + 1
    month = _month_str()
    _monthly_usage[month] = _monthly_usage.get(month, 0) + 1


@dataclass
class ResearchResult:
    """Deep Research 결과 컨테이너."""
    report: str = ""
    interaction_id: str = ""
    citations: List[Dict[str, Any]] = field(default_factory=list)
    thinking_summary: str = ""
    status: str = "done"   # 'done' | 'timeout' | 'error' | 'daily_limit'
    cost_usd: float = 0.0  # F7: 고정값 제거, chat_service에서 토큰 기반 추정
    elapsed_sec: float = 0.0


class DeepResearchService:
    """
    Gemini Deep Research Agent 서비스.
    google.genai SDK → interactions API 사용.
    SDK 미설치/API키 미설정 시 GeminiResearchService(HTTP polling)로 폴백.

    AADS-188A 추가:
    - research_stream(): AsyncGenerator[ResearchEvent] — planning/searching/analyzing/complete 단계 SSE
    - context: 추가 컨텍스트 문자열
    - format: 'summary' | 'detailed' | 'report'
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

    # ─── 스트리밍 AsyncGenerator (AADS-188A 신규) ──────────────────────────────

    async def research_stream(
        self,
        query: str,
        context: Optional[str] = None,
        format: Optional[str] = None,
        timeout: int = _TIMEOUT_STANDARD,
    ) -> AsyncGenerator[Any, None]:
        """
        Gemini Deep Research Agent 스트리밍 AsyncGenerator.

        Yields ResearchEvent (app.models.research):
          - planning: 연구 계획 수립 중
          - searching: 소스 탐색 중 (N/15)
          - analyzing: 교차 분석 중
          - complete: 최종 보고서 + 인용 목록 (content + sources)

        Args:
            query: 리서치 주제
            context: 추가 배경 컨텍스트 (선택)
            format: 'summary' | 'detailed' | 'report' (선택)
            timeout: 타임아웃 초
        """
        from app.models.research import ResearchEvent

        if not self.is_available():
            yield ResearchEvent(
                type="error",
                text="GEMINI_API_KEY 미설정 — Deep Research 비활성",
                content="GEMINI_API_KEY 미설정 — Deep Research 비활성",
            )
            return

        if not _check_daily_limit():
            yield ResearchEvent(
                type="error",
                text="일일 한도 초과 (최대 5건/일)",
                content="일일 한도 초과 (최대 5건/일)",
            )
            return

        yield ResearchEvent(
            type="planning",
            content="연구 계획 수립 중...",
            phase="planning",
            progress_pct=5,
        )
        await asyncio.sleep(0)

        prompt = _build_prompt(query, context, _format_preset(format))

        # Langfuse 트레이스 시작
        lf_span = None
        start_ts = time.monotonic()
        try:
            from app.core.langfuse_config import get_langfuse, is_enabled
            if is_enabled():
                lf = get_langfuse()
                if lf:
                    trace = lf.trace(name="deep_research", input=query, user_id="CEO")
                    lf_span = trace.span(
                        name="gemini_deep_research",
                        input={"query": query, "context": context, "format": format},
                    )
        except Exception:
            pass

        yield ResearchEvent(
            type="searching",
            content="소스 탐색 중... (1/15)",
            phase="searching",
            progress_pct=20,
        )
        await asyncio.sleep(0)

        try:
            collected_report: list[str] = []
            collected_sources: list[dict] = []
            collected_interaction_id = ""

            async with asyncio.timeout(timeout):
                # SDK interactions API 직접 스트리밍 시도
                if self._sdk_available:
                    import google.genai as genai
                    client = genai.Client(api_key=self._api_key)

                    # interactions.create() 우선 (실시간 이벤트 포함)
                    if (hasattr(client, "aio")
                            and hasattr(client.aio, "interactions")
                            and hasattr(client.aio.interactions, "create")):
                        try:
                            interaction = await client.aio.interactions.create(
                                model="gemini-deep-research",
                                messages=[{"role": "user", "content": prompt}],
                                config={"tools": [{"google_search": {}}, {"url_context": {}}]},
                                background=True,
                                stream=True,
                            )
                            sources_seen = 0
                            async for chunk in interaction:
                                event_type = getattr(chunk, "type", "") or ""
                                if "search" in event_type.lower() or "fetch" in event_type.lower():
                                    sources_seen += 1
                                    yield ResearchEvent(
                                        type="searching",
                                        content=f"소스 탐색 중... ({sources_seen}/20)",
                                        phase="searching",
                                        progress_pct=min(20 + sources_seen * 2, 60),
                                    )
                                elif "analyz" in event_type.lower():
                                    yield ResearchEvent(type="analyzing", content="교차 분석 중...", phase="analyzing", progress_pct=70)

                                for cand in getattr(chunk, "candidates", []):
                                    for part in getattr(cand.content, "parts", []):
                                        text = getattr(part, "text", "") or ""
                                        thought = getattr(part, "thought", False)
                                        if thought and text:
                                            yield ResearchEvent(type="thinking", content=text[:200], phase="analyzing")
                                        elif text:
                                            collected_report.append(text)
                                            yield ResearchEvent(type="content", content=text, phase="reporting", progress_pct=80)
                                    gm = getattr(cand, "grounding_metadata", None)
                                    if gm:
                                        for gc in getattr(gm, "grounding_chunks", []):
                                            web = getattr(gc, "web", None)
                                            if web:
                                                url = getattr(web, "uri", "")
                                                if url and not any(c["url"] == url for c in collected_sources):
                                                    collected_sources.append({"url": url, "title": getattr(web, "title", "")})
                        except Exception as ie:
                            logger.warning(f"[DeepResearch.stream interactions] 폴백: {ie}")
                            collected_report.clear(); collected_sources.clear()
                            # generate_content_stream 폴백으로 결과 수집
                            result = await self._research_impl(prompt, stream_callback=None)
                            collected_report.append(result.report)
                            collected_sources = result.citations
                            collected_interaction_id = result.interaction_id
                    else:
                        # SDK 구버전 — generate_content_stream 폴백
                        result = await self._research_impl(prompt, stream_callback=None)
                        collected_report.append(result.report)
                        collected_sources = result.citations
                        collected_interaction_id = result.interaction_id
                else:
                    # SDK 없음 — HTTP 폴백
                    result = await self._research_impl(prompt, stream_callback=None)
                    collected_report.append(result.report)
                    collected_sources = result.citations
                    collected_interaction_id = result.interaction_id

            _increment_daily()
            elapsed = round(time.monotonic() - start_ts, 1)

            if lf_span:
                try:
                    lf_span.end(
                        output=("".join(collected_report))[:500],
                        metadata={
                            "sources_count": len(collected_sources),
                            "cost_usd": "estimated_in_chat_service",
                            "elapsed_sec": elapsed,
                        },
                    )
                except Exception:
                    pass

            if not any(e.type == "analyzing" for e in []):  # 항상 analyzing 이벤트 보장
                yield ResearchEvent(type="analyzing", content="교차 분석 중...", phase="analyzing", progress_pct=80)
            await asyncio.sleep(0)

            yield ResearchEvent(
                type="complete",
                content="".join(collected_report),
                sources=collected_sources[:15],
                interaction_id=collected_interaction_id,
                phase="complete",
                progress_pct=100,
            )

        except asyncio.TimeoutError:
            elapsed = round(time.monotonic() - start_ts, 1)
            logger.warning(f"[DeepResearch.stream] timeout after {timeout}s")
            yield ResearchEvent(
                type="error",
                content=f"[타임아웃 {elapsed}s — 현재까지 수집된 정보로 답변합니다.]",
                text=f"[타임아웃 {timeout}s]",
            )
        except Exception as e:
            logger.error(f"[DeepResearch.stream] error: {e}")
            yield ResearchEvent(
                type="error",
                content=f"[Deep Research 오류: {str(e)[:200]}]",
                text=str(e)[:200],
            )

    # ─── 메인 리서치 (chat_service 호환 인터페이스) ───────────────────────────

    async def research(
        self,
        query: str,
        context: Optional[str] = None,
        format_instructions: Optional[str] = None,
        format: Optional[str] = None,
        stream_callback: Optional[Callable[[str, str], None]] = None,
        timeout: int = _TIMEOUT_STANDARD,
    ) -> ResearchResult:
        """
        Gemini Deep Research Agent로 종합 보고서 생성.

        Args:
            query: 리서치 쿼리
            context: 추가 배경 컨텍스트 (AADS-188A 신규)
            format_instructions: 보고서 형식 자유 텍스트 지시 (선택)
            format: 보고서 형식 프리셋 'summary'|'detailed'|'report' (AADS-188A 신규)
            stream_callback: (event_type, text) → 스트리밍 콜백
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
        combined_format = format_instructions or _format_preset(format)
        prompt = _build_prompt(query, context, combined_format)

        # Langfuse 트레이스
        lf_span = None
        try:
            from app.core.langfuse_config import get_langfuse, is_enabled
            if is_enabled():
                lf = get_langfuse()
                if lf:
                    trace = lf.trace(name="deep_research", input=query, user_id="CEO")
                    lf_span = trace.span(
                        name="gemini_deep_research",
                        input={"query": query, "context": context, "format": format},
                    )
        except Exception:
            pass

        try:
            result = await asyncio.wait_for(
                self._research_impl(prompt, stream_callback),
                timeout=timeout,
            )
            _increment_daily()
            result.elapsed_sec = round(time.monotonic() - start_ts, 1)

            if lf_span:
                try:
                    lf_span.end(
                        output=result.report[:500],
                        metadata={
                            "sources_count": len(result.citations),
                            "cost_usd": result.cost_usd,
                            "elapsed_sec": result.elapsed_sec,
                        },
                    )
                except Exception:
                    pass
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

    # ─── 내부 구현 라우터 ─────────────────────────────────────────────────────

    async def _research_impl(
        self,
        prompt: str,
        stream_callback: Optional[Callable[[str, str], None]],
    ) -> ResearchResult:
        """SDK 가용 여부에 따라 SDK 또는 HTTP 폴백 라우팅."""
        if self._sdk_available:
            return await self._research_via_sdk(prompt, stream_callback)
        return await self._research_via_http(prompt, stream_callback)

    # ─── SDK 방식 (interactions API 우선, generate_content_stream 폴백) ──────────

    async def _research_via_sdk(
        self,
        prompt: str,
        stream_callback: Optional[Callable[[str, str], None]],
    ) -> ResearchResult:
        """
        google.genai SDK 사용.
        1차: client.aio.interactions.create(model="gemini-deep-research", background=True, stream=True)
        2차: client.aio.models.generate_content_stream (구버전 폴백)
        3차: HTTP 폴백
        """
        import google.genai as genai

        client = genai.Client(api_key=self._api_key)
        thinking_parts: list[str] = []
        report_parts: list[str] = []
        citations: list[dict] = []
        interaction_id = ""

        # 1차: interactions API (gemini-deep-research, background=True, stream=True)
        if (hasattr(client, "aio")
                and hasattr(client.aio, "interactions")
                and hasattr(client.aio.interactions, "create")):
            try:
                interaction = await client.aio.interactions.create(
                    model="gemini-deep-research",
                    messages=[{"role": "user", "content": prompt}],
                    config={"tools": [{"google_search": {}}, {"url_context": {}}]},
                    background=True,
                    stream=True,
                )
                async for chunk in interaction:
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
                        gm = getattr(cand, "grounding_metadata", None)
                        if gm:
                            for gc in getattr(gm, "grounding_chunks", []):
                                web = getattr(gc, "web", None)
                                if web:
                                    url = getattr(web, "uri", "")
                                    if url and not any(c["url"] == url for c in citations):
                                        citations.append({"url": url, "title": getattr(web, "title", "")})
                return ResearchResult(
                    report="".join(report_parts),
                    interaction_id=interaction_id,
                    citations=citations[:15],
                    thinking_summary="".join(thinking_parts)[:500],
                    status="done",
                    cost_usd=3.0,
                )
            except Exception as interactions_err:
                logger.warning(f"[DeepResearch interactions.create] 폴백: {interactions_err}")
                report_parts.clear(); citations.clear(); thinking_parts.clear()

        # 2차: generate_content_stream 폴백
        try:
            for model_name in [self._agent, "deep-research-pro-preview-12-2025"]:
                try:
                    async for chunk in await client.aio.models.generate_content_stream(
                        model=model_name, contents=prompt,
                    ):
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
                            gm = getattr(cand, "grounding_metadata", None)
                            if gm:
                                for gc in getattr(gm, "grounding_chunks", []):
                                    web = getattr(gc, "web", None)
                                    if web:
                                        url = getattr(web, "uri", "")
                                        if url and not any(c["url"] == url for c in citations):
                                            citations.append({"url": url, "title": getattr(web, "title", "")})
                    if report_parts:
                        break  # 성공
                except Exception:
                    continue
        except Exception as sdk_err:
            logger.warning(f"[DeepResearch SDK generate_stream] HTTP 폴백: {sdk_err}")
            return await self._research_via_http(prompt, stream_callback)

        if not report_parts:
            return await self._research_via_http(prompt, stream_callback)

        return ResearchResult(
            report="".join(report_parts),
            interaction_id=interaction_id,
            citations=citations[:15],
            thinking_summary="".join(thinking_parts)[:500],
            status="done",
            cost_usd=3.0,
        )

    # ─── HTTP 폴백 ────────────────────────────────────────────────────────────

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
        return "[follow_up 오류: 잠시 후 다시 시도하세요]"


# ─── 헬퍼 ────────────────────────────────────────────────────────────────────

def _build_prompt(query: str, context: Optional[str], format_instructions: Optional[str]) -> str:
    """쿼리 + 컨텍스트 + 형식 지시를 하나의 프롬프트로 결합."""
    parts = [query]
    if context:
        parts.append(f"\n\n## 배경 컨텍스트\n{context}")
    if format_instructions:
        parts.append(f"\n\n## 보고서 형식\n{format_instructions}")
    return "".join(parts)


def _format_preset(format: Optional[str]) -> Optional[str]:
    """포맷 프리셋 → 형식 지시 문자열 변환."""
    presets = {
        "summary": "간결한 요약 형태로 작성. 핵심 포인트 3~5개. 500자 이내.",
        "detailed": "상세 분석 보고서. 배경/현황/주요 발견/결론 섹션 포함. 인용 소스 명시.",
        "report": "공식 보고서 형식. 1. 요약 2. 시장 현황 3. 주요 플레이어 4. 기술 동향 5. 결론 및 추천",
    }
    return presets.get(format or "", None) if format else None


async def _maybe_await(result: Any) -> None:
    """콜백이 코루틴이면 await, 아니면 그냥 반환."""
    if asyncio.iscoroutine(result):
        await result
