"""
AADS-125: Business Strategist Agent 단위 테스트.
gate_condition: 7개 전체 통과 필수.
"""
import pytest
import json
from unittest.mock import AsyncMock, MagicMock, patch


# ─── test_strategy_state_schema ───────────────────────────────────────────────

def test_strategy_state_schema():
    """StrategyState TypedDict 필드 검증."""
    from app.agents.strategist import StrategyState

    state: StrategyState = {
        "direction": "AI 퍼포먼스 마케팅 SaaS",
        "budget": "$100K",
        "timeline": "12개월",
        "search_results": [],
        "strategy_report": None,
        "candidates": [],
        "recommendation": "",
        "sources": [],
    }

    assert state["direction"] == "AI 퍼포먼스 마케팅 SaaS"
    assert state["budget"] == "$100K"
    assert state["timeline"] == "12개월"
    assert isinstance(state["search_results"], list)
    assert state["strategy_report"] is None
    assert isinstance(state["candidates"], list)
    assert isinstance(state["sources"], list)


# ─── test_strategy_report_validation ─────────────────────────────────────────

def test_strategy_report_validation():
    """Pydantic StrategyReport 직렬화/역직렬화 검증."""
    from app.agents.strategist import (
        StrategyReport, MarketResearch, MarketSize,
        Competitor, Trend, Candidate, CandidateScore,
    )

    report = StrategyReport(
        direction="AI 퍼포먼스 마케팅 SaaS",
        market_research=MarketResearch(
            tam=MarketSize(value="$50B", sources=["src1", "src2", "src3"]),
            sam=MarketSize(value="$5B",  sources=["src1", "src2", "src3"]),
            som=MarketSize(value="$500M", sources=["src1", "src2", "src3"]),
        ),
        competitors=[
            Competitor(
                name="HubSpot",
                url="https://hubspot.com",
                pricing="$50/month",
                features=["CRM", "Email Marketing"],
                weaknesses=["비쌈", "복잡한 UI"],
            )
        ],
        trends=[
            Trend(title="AI 자동화", description="AI 광고 최적화 급성장", impact="high"),
        ],
        candidates=[
            Candidate(
                id="C001",
                title="AI 광고 최적화 플랫폼",
                tam_sam_som="TAM $50B / SAM $5B / SOM $500M",
                mvp_cost="$30K",
                mvp_timeline="4개월",
                competitive_edge="실시간 AI 최적화",
                risks=["경쟁 심화"],
                revenue_model="구독 SaaS",
                score=CandidateScore(feasibility=0.8, profitability=0.7, differentiation=0.9, total=0.795),
            )
        ],
        recommendation="C001 AI 광고 최적화 플랫폼 우선 개발",
        total_sources=10,
    )

    # 직렬화
    report_dict = report.model_dump()
    assert report_dict["direction"] == "AI 퍼포먼스 마케팅 SaaS"
    assert report_dict["market_research"]["tam"]["value"] == "$50B"
    assert len(report_dict["competitors"]) == 1
    assert len(report_dict["candidates"]) == 1
    assert report_dict["total_sources"] == 10

    # JSON 직렬화/역직렬화
    json_str = json.dumps(report_dict)
    restored = StrategyReport.model_validate(json.loads(json_str))
    assert restored.direction == report.direction
    assert restored.candidates[0].id == "C001"


# ─── test_collect_market_data_mock ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_collect_market_data_mock():
    """Brave Search 응답 모킹 — search_results 구조 검증."""
    from app.agents.strategist import collect_market_data, StrategyState

    mock_brave_result = [
        {
            "title": "AI SaaS Market Size 2026",
            "url": "https://example.com/market",
            "description": "AI SaaS market expected to reach $100B by 2026",
        }
    ]

    mock_mcp = AsyncMock()
    mock_mcp.call_tool = AsyncMock(return_value=mock_brave_result)

    state: StrategyState = {
        "direction": "AI 퍼포먼스 마케팅 SaaS",
        "search_results": [],
    }

    with patch("app.agents.strategist.get_mcp_manager", return_value=mock_mcp):
        result = await collect_market_data(state)

    assert "search_results" in result
    assert isinstance(result["search_results"], list)
    # search_results에 각 항목은 query, title, url, snippet, source 필드를 가져야 함
    for item in result["search_results"]:
        assert "query" in item
        assert "title" in item
        assert "source" in item


# ─── test_analyze_strategy_mock ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_analyze_strategy_mock():
    """LLM 응답 모킹 — StrategyReport 스키마 준수 검증."""
    from app.agents.strategist import analyze_strategy, StrategyState, StrategyReport

    mock_report = {
        "direction": "AI 퍼포먼스 마케팅 SaaS",
        "market_research": {
            "tam": {"value": "$50B", "sources": ["s1", "s2", "s3"]},
            "sam": {"value": "$5B",  "sources": ["s1", "s2", "s3"]},
            "som": {"value": "$500M", "sources": ["s1", "s2", "s3"]},
        },
        "competitors": [
            {"name": "HubSpot", "url": "", "pricing": "$50/mo", "features": [], "weaknesses": []}
        ],
        "trends": [
            {"title": "AI 자동화", "description": "급성장", "impact": "high"}
        ],
        "candidates": [
            {
                "id": "C001",
                "title": "AI 광고 최적화",
                "tam_sam_som": "TAM $50B",
                "mvp_cost": "$30K",
                "mvp_timeline": "4개월",
                "competitive_edge": "AI",
                "risks": ["경쟁"],
                "revenue_model": "SaaS",
                "score": {"feasibility": 0.8, "profitability": 0.7, "differentiation": 0.9, "total": 0.0},
            }
        ],
        "recommendation": "C001 추천",
        "generated_at": "2026-03-06T12:00:00+00:00",
        "total_sources": 5,
    }

    mock_llm = AsyncMock()
    mock_llm.ainvoke = AsyncMock(
        return_value=MagicMock(content=json.dumps(mock_report))
    )

    state: StrategyState = {
        "direction": "AI 퍼포먼스 마케팅 SaaS",
        "search_results": [
            {"query": "test", "title": "test", "url": "https://ex.com", "snippet": "test", "source": "brave_search"}
        ] * 5,
    }

    with patch("app.agents.strategist.get_llm_for_agent", return_value=(mock_llm, MagicMock())):
        result = await analyze_strategy(state)

    assert "strategy_report" in result
    assert result["strategy_report"] is not None

    # Pydantic 스키마 검증
    validated = StrategyReport.model_validate(result["strategy_report"])
    assert validated.direction == "AI 퍼포먼스 마케팅 SaaS"
    assert len(validated.candidates) >= 1
    assert validated.candidates[0].id == "C001"


# ─── test_candidate_scoring ───────────────────────────────────────────────────

def test_candidate_scoring():
    """점수 계산 로직: feasibility×0.4 + profitability×0.35 + differentiation×0.25"""
    from app.agents.strategist import calculate_candidate_score

    score = calculate_candidate_score(
        feasibility=0.8,
        profitability=0.6,
        differentiation=1.0,
    )

    expected_total = 0.8 * 0.4 + 0.6 * 0.35 + 1.0 * 0.25
    assert score.feasibility == 0.8
    assert score.profitability == 0.6
    assert score.differentiation == 1.0
    assert abs(score.total - expected_total) < 1e-6

    # 경계값 테스트
    score_zero = calculate_candidate_score(0.0, 0.0, 0.0)
    assert score_zero.total == 0.0

    score_perfect = calculate_candidate_score(1.0, 1.0, 1.0)
    assert abs(score_perfect.total - 1.0) < 1e-6


# ─── test_source_minimum ─────────────────────────────────────────────────────

def test_source_minimum():
    """TAM/SAM/SOM 각 sources 배열 len >= 3 검증."""
    from app.agents.strategist import StrategyReport, MarketResearch, MarketSize

    # 정상: sources >= 3
    report = StrategyReport(
        direction="test",
        market_research=MarketResearch(
            tam=MarketSize(value="$50B", sources=["s1", "s2", "s3"]),
            sam=MarketSize(value="$5B",  sources=["s1", "s2", "s3", "s4"]),
            som=MarketSize(value="$500M", sources=["s1", "s2", "s3"]),
        ),
    )
    assert len(report.market_research.tam.sources) >= 3
    assert len(report.market_research.sam.sources) >= 3
    assert len(report.market_research.som.sources) >= 3

    # 직렬화 후에도 유지
    d = report.model_dump()
    assert len(d["market_research"]["tam"]["sources"]) >= 3
    assert len(d["market_research"]["sam"]["sources"]) >= 3
    assert len(d["market_research"]["som"]["sources"]) >= 3


# ─── test_model_routing ───────────────────────────────────────────────────────

def test_model_routing():
    """수집=Flash, 분석=Opus 모델 선택 검증."""
    import app.agents.strategist as strategist_module
    from app.services.model_router import AGENT_MODELS

    # 환경변수 기본값 검증
    assert strategist_module.STRATEGIST_COLLECT_MODEL == "gemini-2.5-flash"
    assert strategist_module.STRATEGIST_ANALYZE_MODEL == "claude-opus-4.6"

    # model_router에 strategist 항목 존재 확인
    assert "strategist_collect" in AGENT_MODELS
    assert "strategist_analyze" in AGENT_MODELS

    # 수집 모델 = Flash
    collect_primary = AGENT_MODELS["strategist_collect"]["primary"]
    assert "flash" in collect_primary.model_id.lower() or "gemini" in collect_primary.model_id.lower()

    # 분석 모델 = Opus
    analyze_primary = AGENT_MODELS["strategist_analyze"]["primary"]
    assert "opus" in analyze_primary.model_id.lower()
