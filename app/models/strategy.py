"""
AADS-130: 전략 분석 Pydantic 모델 정의.
Strategist 에이전트 산출물 스키마.
"""
from __future__ import annotations
from typing import Optional
from pydantic import BaseModel, Field


class MarketSize(BaseModel):
    tam: float = Field(default=0.0, description="Total Addressable Market (억 달러)")
    sam: float = Field(default=0.0, description="Serviceable Addressable Market (억 달러)")
    som: float = Field(default=0.0, description="Serviceable Obtainable Market (억 달러)")
    source: str = Field(default="", description="출처")


class Competitor(BaseModel):
    name: str = ""
    description: str = ""
    strengths: list[str] = Field(default_factory=list)
    weaknesses: list[str] = Field(default_factory=list)


class Trend(BaseModel):
    title: str = ""
    description: str = ""
    impact: str = ""


class CandidateScore(BaseModel):
    market_potential: float = 0.0
    feasibility: float = 0.0
    differentiation: float = 0.0
    profitability: float = 0.0
    total: float = 0.0


class StrategyCandidate(BaseModel):
    id: str = ""
    title: str = ""
    description: str = ""
    market_size: Optional[MarketSize] = None
    mvp_cost: str = ""
    mvp_timeline: str = ""
    competitive_edge: str = ""
    risks: list[str] = Field(default_factory=list)
    score: Optional[CandidateScore] = None


class MarketResearch(BaseModel):
    market_size: Optional[MarketSize] = None
    competitors: list[Competitor] = Field(default_factory=list)
    trends: list[Trend] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)


class StrategyReport(BaseModel):
    direction: str = ""
    market_research: Optional[MarketResearch] = None
    candidates: list[StrategyCandidate] = Field(default_factory=list)
    recommendation: str = ""
    project_id: Optional[str] = None
