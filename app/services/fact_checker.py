"""
AADS 팩트 체크 엔진 — 웹 검색 + 서버 DB 교차 검증
=====================================================
3단계 Defense-in-Depth:
  1단계: 서버 DB 검증 (memory_facts, ai_observations, chat_messages)
  2단계: 외부 웹 검색 교차 검증 (Google + Naver 병렬)
  3단계: 판정 + 신뢰도 점수 반환

판정 결과:
  VERIFIED   — DB + 웹 2개 이상 출처 일치
  DB_ONLY    — DB에서 확인됨, 웹 미확인
  WEB_ONLY   — 웹에서 확인됨, DB 없음
  UNCERTAIN  — 단일 소스만 확인
  DISPUTED   — 소스 간 모순 감지
  UNVERIFIED — 확인 불가
"""
from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Dict, Any

import httpx
import structlog

logger = structlog.get_logger(__name__)

KST = timezone(timedelta(hours=9))

# ── 환경변수 ──────────────────────────────────────────────────────────────────
GOOGLE_API_KEY   = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY", "")
GOOGLE_CSE_ID    = os.getenv("GOOGLE_CSE_ID", "")
NAVER_CLIENT_ID  = os.getenv("NAVER_CLIENT_ID", "")
NAVER_SECRET     = os.getenv("NAVER_CLIENT_SECRET", "")


# ── 데이터 모델 ───────────────────────────────────────────────────────────────

@dataclass
class DBEvidence:
    """서버 DB에서 찾은 근거."""
    source_table: str          # memory_facts / ai_observations / chat_messages
    subject: str
    detail: str
    confidence: float
    created_at: Optional[str] = None
    match_score: float = 0.0   # 키워드 일치율 0.0~1.0


@dataclass
class WebEvidence:
    """웹 검색에서 찾은 근거."""
    engine: str                # google / naver
    title: str
    snippet: str
    url: str
    supports: bool = True      # True=지지, False=반박


@dataclass
class FactCheckResult:
    """팩트 체크 최종 결과."""
    claim: str
    verdict: str               # VERIFIED / DB_ONLY / WEB_ONLY / UNCERTAIN / DISPUTED / UNVERIFIED
    confidence: float          # 0.0~1.0
    db_evidences: List[DBEvidence] = field(default_factory=list)
    web_evidences: List[WebEvidence] = field(default_factory=list)
    summary: str = ""
    checked_at: str = ""

    def to_dict(self) -> dict:
        return {
            "claim": self.claim,
            "verdict": self.verdict,
            "confidence": round(self.confidence, 3),
            "db_count": len(self.db_evidences),
            "web_count": len(self.web_evidences),
            "db_evidences": [
                {
                    "source": e.source_table,
                    "subject": e.subject,
                    "detail": e.detail[:200],
                    "confidence": e.confidence,
                    "match_score": e.match_score,
                }
                for e in self.db_evidences
            ],
            "web_evidences": [
                {
                    "engine": e.engine,
                    "title": e.title,
                    "snippet": e.snippet[:200],
                    "url": e.url,
                    "supports": e.supports,
                }
                for e in self.web_evidences
            ],
            "summary": self.summary,
            "checked_at": self.checked_at,
        }


# ── 팩트 체크 엔진 ─────────────────────────────────────────────────────────────

class FactChecker:
    """
    웹 검색 + 서버 DB 교차 검증 팩트 체크 엔진.
    """

    def __init__(self, pool=None):
        self.pool = pool

    # ── 공개 API ──────────────────────────────────────────────────────────────

    async def check(self, claim: str, session_id: Optional[str] = None) -> FactCheckResult:
        """
        주어진 주장(claim)을 DB + 웹에서 교차 검증.

        Args:
            claim: 검증할 주장/사실 문자열
            session_id: 현재 세션 ID (DB 검색 범위 확장용)

        Returns:
            FactCheckResult
        """
        now_kst = datetime.now(tz=KST).isoformat()
        keywords = self._extract_keywords(claim)

        # 1단계 + 2단계 병렬 실행
        db_task  = asyncio.create_task(self._check_db(claim, keywords, session_id))
        web_task = asyncio.create_task(self._check_web(claim, keywords))

        db_evidences, web_evidences = await asyncio.gather(db_task, web_task, return_exceptions=True)

        if isinstance(db_evidences, Exception):
            logger.warning("fact_checker_db_error", error=str(db_evidences))
            db_evidences = []
        if isinstance(web_evidences, Exception):
            logger.warning("fact_checker_web_error", error=str(web_evidences))
            web_evidences = []

        # 3단계: 판정
        verdict, confidence, summary = self._verdict(
            claim, db_evidences, web_evidences
        )

        return FactCheckResult(
            claim=claim,
            verdict=verdict,
            confidence=confidence,
            db_evidences=db_evidences,
            web_evidences=web_evidences,
            summary=summary,
            checked_at=now_kst,
        )

    async def check_multiple(
        self, claims: List[str], session_id: Optional[str] = None
    ) -> List[FactCheckResult]:
        """여러 주장을 병렬 검증."""
        tasks = [self.check(c, session_id) for c in claims]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        out = []
        for c, r in zip(claims, results):
            if isinstance(r, Exception):
                out.append(FactCheckResult(
                    claim=c, verdict="UNVERIFIED", confidence=0.0,
                    summary=f"검증 오류: {r}", checked_at=datetime.now(tz=KST).isoformat()
                ))
            else:
                out.append(r)
        return out

    # ── 1단계: 서버 DB 검증 ───────────────────────────────────────────────────

    async def _check_db(
        self, claim: str, keywords: List[str], session_id: Optional[str]
    ) -> List[DBEvidence]:
        """memory_facts + ai_observations + chat_messages DB 교차 검증."""
        if not self.pool:
            return []

        evidences: List[DBEvidence] = []

        try:
            async with self.pool.acquire() as conn:
                # ── memory_facts 검색 ──────────────────────────────────────
                mf_rows = await conn.fetch("""
                    SELECT subject, detail, confidence, category,
                           created_at, referenced_count
                    FROM memory_facts
                    WHERE superseded_by IS NULL
                      AND confidence > 0.3
                      AND (
                          subject ILIKE ANY($1)
                          OR detail ILIKE ANY($1)
                      )
                    ORDER BY confidence DESC, referenced_count DESC
                    LIMIT 5
                """, [f"%{kw}%" for kw in keywords[:8]])

                for row in mf_rows:
                    score = self._keyword_match_score(
                        claim, f"{row['subject']} {row['detail']}"
                    )
                    if score > 0.2:
                        evidences.append(DBEvidence(
                            source_table="memory_facts",
                            subject=row["subject"] or "",
                            detail=row["detail"] or "",
                            confidence=float(row["confidence"] or 0.7),
                            created_at=str(row["created_at"]) if row["created_at"] else None,
                            match_score=score,
                        ))

                # ── ai_observations 검색 ───────────────────────────────────
                ao_rows = await conn.fetch("""
                    SELECT key, value, confidence, category, created_at
                    FROM ai_observations
                    WHERE confidence > 0.5
                      AND (
                          key ILIKE ANY($1)
                          OR value ILIKE ANY($1)
                      )
                    ORDER BY confidence DESC, last_confirmed_at DESC NULLS LAST
                    LIMIT 5
                """, [f"%{kw}%" for kw in keywords[:8]])

                for row in ao_rows:
                    score = self._keyword_match_score(
                        claim, f"{row['key']} {row['value']}"
                    )
                    if score > 0.2:
                        evidences.append(DBEvidence(
                            source_table="ai_observations",
                            subject=row["key"] or "",
                            detail=row["value"] or "",
                            confidence=float(row["confidence"] or 0.5),
                            created_at=str(row["created_at"]) if row["created_at"] else None,
                            match_score=score,
                        ))

                # ── chat_messages 시맨틱 검색 (최근 2000개 범위) ───────────
                cm_rows = await conn.fetch("""
                    SELECT role, content, created_at, quality_score
                    FROM chat_messages
                    WHERE role = 'assistant'
                      AND quality_score IS NOT NULL
                      AND quality_score > 0.5
                      AND (
                          content ILIKE ANY($1)
                      )
                    ORDER BY created_at DESC
                    LIMIT 3
                """, [f"%{kw}%" for kw in keywords[:5]])

                for row in cm_rows:
                    score = self._keyword_match_score(claim, row["content"] or "")
                    if score > 0.3:
                        evidences.append(DBEvidence(
                            source_table="chat_messages",
                            subject="이전 AI 응답",
                            detail=(row["content"] or "")[:300],
                            confidence=float(row["quality_score"] or 0.5),
                            created_at=str(row["created_at"]) if row["created_at"] else None,
                            match_score=score,
                        ))

        except Exception as e:
            logger.warning("fact_checker_db_query_error", error=str(e))

        # match_score 내림차순 정렬
        evidences.sort(key=lambda x: x.match_score, reverse=True)
        return evidences[:8]

    # ── 2단계: 웹 검색 교차 검증 ─────────────────────────────────────────────

    async def _check_web(
        self, claim: str, keywords: List[str]
    ) -> List[WebEvidence]:
        """Google + Naver 병렬 검색."""
        query = " ".join(keywords[:5])
        tasks = []

        if GOOGLE_API_KEY:
            tasks.append(self._search_google(claim, query))
        if NAVER_CLIENT_ID and NAVER_SECRET:
            tasks.append(self._search_naver(claim, query))

        if not tasks:
            logger.warning("fact_checker_no_search_api")
            return []

        results = await asyncio.gather(*tasks, return_exceptions=True)
        evidences: List[WebEvidence] = []
        for r in results:
            if isinstance(r, list):
                evidences.extend(r)

        return evidences[:10]

    async def _search_google(self, claim: str, query: str) -> List[WebEvidence]:
        """Google Custom Search API."""
        if not GOOGLE_CSE_ID:
            # Gemini Grounding 방식으로 폴백
            return await self._search_gemini_grounding(claim, query)

        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.get(
                    "https://www.googleapis.com/customsearch/v1",
                    params={
                        "key": GOOGLE_API_KEY,
                        "cx": GOOGLE_CSE_ID,
                        "q": query,
                        "num": 5,
                    }
                )
                if resp.status_code != 200:
                    return []
                data = resp.json()
                items = data.get("items", [])
                evidences = []
                for item in items:
                    snippet = item.get("snippet", "")
                    supports = self._text_supports_claim(claim, snippet)
                    evidences.append(WebEvidence(
                        engine="google",
                        title=item.get("title", ""),
                        snippet=snippet,
                        url=item.get("link", ""),
                        supports=supports,
                    ))
                return evidences
        except Exception as e:
            logger.warning("fact_checker_google_error", error=str(e))
            return []

    async def _search_gemini_grounding(self, claim: str, query: str) -> List[WebEvidence]:
        """Gemini API를 이용한 웹 검색 (Google Search grounding)."""
        try:
            from google import genai as google_genai
            from google.genai import types as genai_types

            client = google_genai.Client(api_key=GOOGLE_API_KEY)
            loop = asyncio.get_running_loop()

            def _call():
                return client.models.generate_content(
                    model="gemini-2.0-flash",
                    contents=f"다음 주장이 사실인지 검색해서 확인해줘: {claim}\n검색어: {query}",
                    config=genai_types.GenerateContentConfig(
                        tools=[genai_types.Tool(google_search=genai_types.GoogleSearch())],
                        temperature=0.1,
                    )
                )

            response = await asyncio.wait_for(
                loop.run_in_executor(None, _call), timeout=10.0
            )

            evidences = []
            # grounding metadata에서 소스 추출
            if hasattr(response, 'candidates') and response.candidates:
                candidate = response.candidates[0]
                if hasattr(candidate, 'grounding_metadata') and candidate.grounding_metadata:
                    gm = candidate.grounding_metadata
                    chunks = getattr(gm, 'grounding_chunks', []) or []
                    for chunk in chunks[:5]:
                        web = getattr(chunk, 'web', None)
                        if web:
                            title = getattr(web, 'title', '') or ''
                            uri   = getattr(web, 'uri', '') or ''
                            supports = self._text_supports_claim(
                                claim, response.text or ""
                            )
                            evidences.append(WebEvidence(
                                engine="google",
                                title=title,
                                snippet=(response.text or "")[:200],
                                url=uri,
                                supports=supports,
                            ))

            # grounding 없으면 텍스트 응답을 단일 evidence로
            if not evidences and response.text:
                supports = self._text_supports_claim(claim, response.text)
                evidences.append(WebEvidence(
                    engine="google",
                    title="Gemini 검색 결과",
                    snippet=response.text[:300],
                    url="",
                    supports=supports,
                ))

            return evidences

        except Exception as e:
            logger.warning("fact_checker_gemini_error", error=str(e))
            return []

    async def _search_naver(self, claim: str, query: str) -> List[WebEvidence]:
        """Naver 검색 API (webkr + news 병렬)."""
        evidences: List[WebEvidence] = []
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                headers = {
                    "X-Naver-Client-Id": NAVER_CLIENT_ID,
                    "X-Naver-Client-Secret": NAVER_SECRET,
                }
                # webkr + news 동시 검색
                tasks = [
                    client.get(
                        f"https://openapi.naver.com/v1/search/{stype}.json",
                        headers=headers,
                        params={"query": query, "display": 3, "sort": "sim"}
                    )
                    for stype in ["webkr", "news"]
                ]
                responses = await asyncio.gather(*tasks, return_exceptions=True)

                for resp in responses:
                    if isinstance(resp, Exception):
                        continue
                    if resp.status_code != 200:
                        continue
                    items = resp.json().get("items", [])
                    for item in items:
                        title   = re.sub(r"<[^>]+>", "", item.get("title", ""))
                        snippet = re.sub(r"<[^>]+>", "", item.get("description", ""))
                        url     = item.get("originallink") or item.get("link", "")
                        supports = self._text_supports_claim(claim, f"{title} {snippet}")
                        evidences.append(WebEvidence(
                            engine="naver",
                            title=title,
                            snippet=snippet,
                            url=url,
                            supports=supports,
                        ))
        except Exception as e:
            logger.warning("fact_checker_naver_error", error=str(e))

        return evidences

    # ── 3단계: 판정 ───────────────────────────────────────────────────────────

    def _verdict(
        self,
        claim: str,
        db_evidences: List[DBEvidence],
        web_evidences: List[WebEvidence],
    ) -> tuple[str, float, str]:
        """DB + 웹 근거 종합 → 판정 + 신뢰도 + 요약."""

        db_count  = len(db_evidences)
        web_support = [e for e in web_evidences if e.supports]
        web_dispute = [e for e in web_evidences if not e.supports]
        web_support_count = len(web_support)
        web_dispute_count = len(web_dispute)

        # 모순 감지
        if web_dispute_count > 0 and (db_count > 0 or web_support_count > 0):
            verdict = "DISPUTED"
            confidence = 0.3
            summary = (
                f"⚠️ 모순 감지: {web_support_count}개 소스 지지, "
                f"{web_dispute_count}개 소스 반박. DB 근거 {db_count}건."
            )
            return verdict, confidence, summary

        # VERIFIED: DB + 웹 모두 확인
        if db_count >= 1 and web_support_count >= 2:
            verdict = "VERIFIED"
            avg_db_conf = sum(e.confidence for e in db_evidences) / db_count
            confidence = min(0.95, 0.6 + avg_db_conf * 0.2 + web_support_count * 0.05)
            summary = (
                f"✅ 검증됨: DB {db_count}건 + 웹 {web_support_count}개 출처 일치. "
                f"DB 평균 신뢰도 {avg_db_conf:.2f}"
            )
            return verdict, confidence, summary

        # DB_ONLY: DB에서만 확인
        if db_count >= 1 and web_support_count == 0:
            verdict = "DB_ONLY"
            avg_db_conf = sum(e.confidence for e in db_evidences) / db_count
            confidence = min(0.75, avg_db_conf)
            summary = (
                f"🗄️ DB 내부 확인: {db_count}건 (평균 신뢰도 {avg_db_conf:.2f}). "
                f"웹 외부 검증 미확인."
            )
            return verdict, confidence, summary

        # WEB_ONLY: 웹에서만 확인
        if db_count == 0 and web_support_count >= 2:
            verdict = "WEB_ONLY"
            confidence = min(0.70, 0.4 + web_support_count * 0.1)
            summary = (
                f"🌐 웹 검증: {web_support_count}개 출처 확인. "
                f"내부 DB 기록 없음."
            )
            return verdict, confidence, summary

        # UNCERTAIN: 단일 소스
        if db_count == 1 or web_support_count == 1:
            verdict = "UNCERTAIN"
            confidence = 0.4
            summary = f"⚠️ 불확실: 단일 소스만 확인됨 (DB {db_count}건, 웹 {web_support_count}건)."
            return verdict, confidence, summary

        # UNVERIFIED
        verdict = "UNVERIFIED"
        confidence = 0.1
        summary = "❓ 미확인: DB 및 웹 검색 모두에서 근거를 찾을 수 없습니다."
        return verdict, confidence, summary

    # ── 유틸리티 ──────────────────────────────────────────────────────────────

    def _extract_keywords(self, text: str) -> List[str]:
        """텍스트에서 핵심 키워드 추출 (불용어 제거)."""
        stop_words = {
            "이", "가", "은", "는", "을", "를", "의", "에", "와", "과",
            "도", "로", "으로", "에서", "하다", "있다", "되다", "그", "이다",
            "a", "an", "the", "is", "are", "was", "were", "be", "been",
            "and", "or", "but", "in", "on", "at", "to", "for", "of",
        }
        # 한글+영문+숫자 토큰 추출
        tokens = re.findall(r"[가-힣]{2,}|[a-zA-Z]{3,}|\d{4,}", text)
        keywords = [t for t in tokens if t.lower() not in stop_words]
        # 중복 제거 + 순서 유지
        seen = set()
        result = []
        for k in keywords:
            if k not in seen:
                seen.add(k)
                result.append(k)
        return result[:10]

    def _keyword_match_score(self, claim: str, text: str) -> float:
        """claim의 키워드가 text에 얼마나 포함됐는지 0.0~1.0 점수."""
        if not text:
            return 0.0
        keywords = self._extract_keywords(claim)
        if not keywords:
            return 0.0
        text_lower = text.lower()
        matched = sum(1 for k in keywords if k.lower() in text_lower)
        return matched / len(keywords)

    def _text_supports_claim(self, claim: str, text: str) -> bool:
        """
        텍스트가 주장을 지지하는지 반박하는지 판단.
        부정어 패턴 감지 → 반박, 키워드 일치 → 지지.
        """
        text_lower = text.lower()
        claim_lower = claim.lower()

        # 부정어 패턴
        negate_patterns = [
            r"(사실이\s*아니|거짓|틀렸|오류|잘못|없다|존재하지\s*않)",
            r"(false|incorrect|wrong|not\s+true|disputed|debunked)",
        ]
        for pat in negate_patterns:
            if re.search(pat, text_lower):
                # 부정어가 claim의 키워드 근처에 있으면 반박
                keywords = self._extract_keywords(claim)
                for kw in keywords[:3]:
                    if kw.lower() in text_lower:
                        return False

        # 키워드 일치율로 지지 여부 판단
        score = self._keyword_match_score(claim, text)
        return score > 0.2


# ── 싱글턴 ───────────────────────────────────────────────────────────────────

_fact_checker: Optional[FactChecker] = None


def get_fact_checker(pool=None) -> FactChecker:
    """FactChecker 싱글턴 반환."""
    global _fact_checker
    if _fact_checker is None or (pool is not None and _fact_checker.pool is None):
        _fact_checker = FactChecker(pool=pool)
    return _fact_checker


def init_fact_checker(pool) -> FactChecker:
    """앱 시작 시 pool과 함께 초기화."""
    global _fact_checker
    _fact_checker = FactChecker(pool=pool)
    return _fact_checker
