"""
Business Strategist Agent: 시장조사·경쟁분석·아이템 발굴 자동화.
AADS-125: 10-Agent 풀사이클 확장 — Strategist 추가.
"""
from __future__ import annotations

import os
import json
import structlog
from datetime import datetime, timezone
from typing import Optional, Any
from typing_extensions import TypedDict

from pydantic import BaseModel, Field

# 모듈 레벨 임포트 (테스트 패칭 가능하도록)
try:
    from app.mcp.client import get_mcp_manager
except ImportError:
    get_mcp_manager = None  # type: ignore

try:
    from app.services.model_router import get_llm_for_agent
except ImportError:
    get_llm_for_agent = None  # type: ignore

logger = structlog.get_logger()

# ─── 모델 라우팅 ──────────────────────────────────────────────────────────────

STRATEGIST_COLLECT_MODEL: str = os.getenv(
    "STRATEGIST_COLLECT_MODEL", "gemini-2.5-flash"
)
STRATEGIST_ANALYZE_MODEL: str = os.getenv(
    "STRATEGIST_ANALYZE_MODEL", "claude-opus-4.6"
)

# ─── TypedDict State ─────────────────────────────────────────────────────────


class StrategyState(TypedDict, total=False):
    direction: str
    budget: Optional[str]
    timeline: Optional[str]
    search_results: list[dict]
    strategy_report: Optional[dict]
    candidates: list[dict]
    recommendation: str
    sources: list[dict]


# ─── Pydantic v2 Models ──────────────────────────────────────────────────────


class MarketSize(BaseModel):
    value: str
    sources: list[str] = Field(default_factory=list)


class MarketResearch(BaseModel):
    tam: MarketSize
    sam: MarketSize
    som: MarketSize


class Competitor(BaseModel):
    name: str
    url: str = ""
    pricing: str = ""
    features: list[str] = Field(default_factory=list)
    weaknesses: list[str] = Field(default_factory=list)


class Trend(BaseModel):
    title: str
    description: str
    impact: str = "medium"


class CandidateScore(BaseModel):
    feasibility: float = 0.0
    profitability: float = 0.0
    differentiation: float = 0.0
    total: float = 0.0


class Candidate(BaseModel):
    id: str
    title: str
    tam_sam_som: str = ""
    mvp_cost: str = ""
    mvp_timeline: str = ""
    competitive_edge: str = ""
    risks: list[str] = Field(default_factory=list)
    revenue_model: str = ""
    score: CandidateScore = Field(default_factory=CandidateScore)


class StrategyReport(BaseModel):
    direction: str
    market_research: MarketResearch
    competitors: list[Competitor] = Field(default_factory=list)
    trends: list[Trend] = Field(default_factory=list)
    candidates: list[Candidate] = Field(default_factory=list)
    recommendation: str = ""
    generated_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    total_sources: int = 0


# ─── 점수 계산 ────────────────────────────────────────────────────────────────


def calculate_candidate_score(
    feasibility: float,
    profitability: float,
    differentiation: float,
) -> CandidateScore:
    """아이템 후보 점수 계산: feasibility×0.4 + profitability×0.35 + differentiation×0.25"""
    total = feasibility * 0.4 + profitability * 0.35 + differentiation * 0.25
    return CandidateScore(
        feasibility=feasibility,
        profitability=profitability,
        differentiation=differentiation,
        total=round(total, 4),
    )


# ─── 데이터 수집 (Brave Search + Fetch MCP) ───────────────────────────────────


async def collect_market_data(state: StrategyState) -> StrategyState:
    """
    Brave Search MCP로 시장 데이터 수집.
    모델: STRATEGIST_COLLECT_MODEL (gemini-2.5-flash, 비용 효율)
    """
    direction = state.get("direction", "AI SaaS")
    search_results: list[dict] = list(state.get("search_results", []))

    logger.info("strategist_collect_start", direction=direction, model=STRATEGIST_COLLECT_MODEL)

    # MCP Brave Search 쿼리 목록 (5~10회)
    queries = [
        f"{direction} market size TAM 2026",
        f"{direction} SAM SOM addressable market",
        f"{direction} top competitors comparison 2025 2026",
        f"{direction} market trends growth rate",
        f"{direction} startup funding investment 2025",
        f"{direction} revenue model pricing strategy",
        f"{direction} customer segments B2B B2C",
        f"{direction} technology stack ecosystem",
    ]

    try:
        mcp_manager = get_mcp_manager()
        fetched_urls: list[str] = []

        for query in queries:
            try:
                result = await mcp_manager.call_tool(
                    "brave_search",
                    "brave_web_search",
                    {"query": query, "count": 5},
                )
                if result:
                    raw_results = result if isinstance(result, list) else [result]
                    for item in raw_results[:5]:
                        entry: dict[str, Any] = {
                            "query": query,
                            "title": item.get("title", ""),
                            "url": item.get("url", ""),
                            "snippet": item.get("description", item.get("snippet", "")),
                            "source": "brave_search",
                        }
                        search_results.append(entry)
                        url = item.get("url", "")
                        if url and len(fetched_urls) < 3:
                            fetched_urls.append(url)
            except Exception as e:
                logger.warning("brave_search_query_failed", query=query, error=str(e))
                # 검색 실패 시 더미 결과 추가 (테스트 및 MCP 미연결 환경 대응)
                search_results.append({
                    "query": query,
                    "title": f"[검색 실패] {query}",
                    "url": "",
                    "snippet": f"MCP 검색 실패: {str(e)}",
                    "source": "fallback",
                })

        # 상위 3개 URL 본문 수집 (Fetch MCP)
        for url in fetched_urls[:3]:
            try:
                fetch_result = await mcp_manager.call_tool(
                    "fetch",
                    "fetch",
                    {"url": url, "max_length": 3000},
                )
                if fetch_result:
                    content = fetch_result if isinstance(fetch_result, str) else str(fetch_result)
                    search_results.append({
                        "query": f"fetch:{url}",
                        "title": f"Full content: {url}",
                        "url": url,
                        "snippet": content[:2000],
                        "source": "fetch_mcp",
                    })
            except Exception as e:
                logger.warning("fetch_url_failed", url=url, error=str(e))

    except Exception as e:
        logger.warning("mcp_unavailable_using_fallback", error=str(e))
        # MCP 없는 환경 — 최소 플레이스홀더 결과
        for query in queries[:5]:
            search_results.append({
                "query": query,
                "title": f"[MCP 미연결] {query}",
                "url": "",
                "snippet": "MCP 서버 미연결 상태. 실제 운영 시 Brave Search MCP 연결 필요.",
                "source": "fallback",
            })

    logger.info("strategist_collect_done", results_count=len(search_results))
    return {**state, "search_results": search_results}


# ─── 전략 분석 (Claude Opus 4.6) ─────────────────────────────────────────────


async def analyze_strategy(state: StrategyState) -> StrategyState:
    """
    수집 데이터 기반 전략 분석 + StrategyReport 생성.
    모델: STRATEGIST_ANALYZE_MODEL (claude-opus-4.6)
    """
    direction = state.get("direction", "AI SaaS")
    search_results = state.get("search_results", [])

    logger.info("strategist_analyze_start", direction=direction, model=STRATEGIST_ANALYZE_MODEL)

    # 검색 결과 요약 (토큰 절약)
    snippets = "\n".join(
        f"[{i+1}] {r.get('title','')}: {r.get('snippet','')[:300]}"
        for i, r in enumerate(search_results[:20])
    )

    system_prompt = f"""당신은 시장조사 전문 전략가입니다.
제공된 검색 결과를 바탕으로 "{direction}" 사업의 전략 보고서를 JSON으로 작성하세요.

반드시 아래 JSON 스키마를 준수하세요:
{{
  "direction": "{direction}",
  "market_research": {{
    "tam": {{"value": "숫자+단위+설명", "sources": ["출처1","출처2","출처3"]}},
    "sam": {{"value": "숫자+단위+설명", "sources": ["출처1","출처2","출처3"]}},
    "som": {{"value": "숫자+단위+설명", "sources": ["출처1","출처2","출처3"]}}
  }},
  "competitors": [
    {{"name":"경쟁사명","url":"URL","pricing":"가격정책","features":["기능1"],"weaknesses":["약점1"]}}
  ],
  "trends": [
    {{"title":"트렌드명","description":"설명","impact":"high|medium|low"}}
  ],
  "candidates": [
    {{
      "id":"C001",
      "title":"아이템명",
      "tam_sam_som":"TAM $XB / SAM $XM / SOM $XM",
      "mvp_cost":"$X~$XK",
      "mvp_timeline":"X개월",
      "competitive_edge":"차별화 요소",
      "risks":["리스크1","리스크2"],
      "revenue_model":"수익 모델",
      "score":{{"feasibility":0.0,"profitability":0.0,"differentiation":0.0,"total":0.0}}
    }}
  ],
  "recommendation": "최종 추천 아이템 및 이유",
  "generated_at": "ISO8601",
  "total_sources": 0
}}

규칙:
- TAM/SAM/SOM 각각 sources 배열에 최소 3개 출처 필수
- 경쟁사 상위 5개 매핑 (가격/기능/약점)
- 트렌드 최소 3개 (12개월 기준)
- 아이템 후보 3~5개, 점수: feasibility×0.4 + profitability×0.35 + differentiation×0.25
- JSON만 출력, 추가 설명 없음
"""

    user_message = f"""검색 결과 ({len(search_results)}개):
{snippets}

위 결과를 바탕으로 "{direction}" 시장 전략 보고서 JSON을 작성하세요."""

    try:
        llm, model_config = get_llm_for_agent("strategist_analyze")
        from langchain_core.messages import HumanMessage, SystemMessage

        response = await llm.ainvoke([
            SystemMessage(content=system_prompt),
            HumanMessage(content=user_message),
        ])
        raw_content = response.content if hasattr(response, "content") else str(response)

        # JSON 파싱
        json_str = raw_content.strip()
        if "```json" in json_str:
            json_str = json_str.split("```json")[1].split("```")[0].strip()
        elif "```" in json_str:
            json_str = json_str.split("```")[1].split("```")[0].strip()

        report_dict = json.loads(json_str)

    except Exception as e:
        logger.warning("strategist_analyze_llm_failed", error=str(e))
        # LLM 실패 시 기본 구조 반환
        report_dict = _build_fallback_report(direction, search_results)

    # 후보 점수 재계산 (로직 보장)
    for cand in report_dict.get("candidates", []):
        sc = cand.get("score", {})
        calculated = calculate_candidate_score(
            feasibility=float(sc.get("feasibility", 0.0)),
            profitability=float(sc.get("profitability", 0.0)),
            differentiation=float(sc.get("differentiation", 0.0)),
        )
        cand["score"] = calculated.model_dump()

    # total_sources 집계
    report_dict["total_sources"] = len(search_results)
    report_dict["direction"] = direction

    # Pydantic 검증
    try:
        validated = StrategyReport.model_validate(report_dict)
        report_dict = validated.model_dump()
    except Exception as e:
        logger.warning("strategy_report_validation_warning", error=str(e))

    # candidates 추출
    candidates = report_dict.get("candidates", [])
    recommendation = report_dict.get("recommendation", "")

    # sources 집계
    sources = [
        {"url": r.get("url", ""), "title": r.get("title", ""), "query": r.get("query", "")}
        for r in search_results if r.get("url")
    ]

    logger.info("strategist_analyze_done", candidates_count=len(candidates))

    return {
        **state,
        "strategy_report": report_dict,
        "candidates": candidates,
        "recommendation": recommendation,
        "sources": sources,
    }


def _build_fallback_report(direction: str, search_results: list[dict]) -> dict:
    """LLM 실패 시 기본 구조 생성."""
    placeholder_sources = ["[출처 미수집]", "[출처 미수집]", "[출처 미수집]"]
    return {
        "direction": direction,
        "market_research": {
            "tam": {"value": "데이터 수집 중", "sources": placeholder_sources},
            "sam": {"value": "데이터 수집 중", "sources": placeholder_sources},
            "som": {"value": "데이터 수집 중", "sources": placeholder_sources},
        },
        "competitors": [],
        "trends": [],
        "candidates": [
            {
                "id": "C001",
                "title": f"{direction} 기반 SaaS 서비스",
                "tam_sam_som": "분석 필요",
                "mvp_cost": "$10K~$50K",
                "mvp_timeline": "3~6개월",
                "competitive_edge": "AI 자동화",
                "risks": ["시장 검증 필요"],
                "revenue_model": "구독 모델",
                "score": {"feasibility": 0.7, "profitability": 0.6, "differentiation": 0.7, "total": 0.0},
            }
        ],
        "recommendation": f"{direction} 분야 추가 분석 필요",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_sources": len(search_results),
    }


# ─── DB 저장 ─────────────────────────────────────────────────────────────────


async def save_strategy_report(
    project_id: str,
    report: dict,
    cost_usd: float = 0.0,
    model_used: str = "",
) -> int | None:
    """strategy_reports 테이블에 보고서 저장."""
    import os
    import asyncpg

    db_url = os.getenv("DATABASE_URL", "")
    if not db_url:
        logger.warning("save_strategy_report_no_db_url")
        return None

    try:
        conn = await asyncpg.connect(db_url, timeout=10)
        try:
            row = await conn.fetchrow(
                """
                INSERT INTO strategy_reports
                    (project_id, direction, strategy_report, candidates,
                     recommendation, total_sources, cost_usd, model_used)
                VALUES ($1, $2, $3::jsonb, $4::jsonb, $5, $6, $7, $8)
                RETURNING id
                """,
                project_id,
                report.get("direction", ""),
                json.dumps(report, ensure_ascii=False),
                json.dumps(report.get("candidates", []), ensure_ascii=False),
                report.get("recommendation", ""),
                report.get("total_sources", 0),
                cost_usd,
                model_used or STRATEGIST_ANALYZE_MODEL,
            )
            report_id = row["id"] if row else None
            logger.info("strategy_report_saved", report_id=report_id, project_id=project_id)
            return report_id
        finally:
            await conn.close()
    except Exception as e:
        logger.error("save_strategy_report_error", error=str(e))
        return None
