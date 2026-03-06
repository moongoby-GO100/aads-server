"""
AADS-130: 기획서(PRD, 아키텍처, Phase) Pydantic 모델 정의.
Planner 에이전트 산출물 스키마.
"""
from __future__ import annotations
from typing import Optional
from pydantic import BaseModel, Field


class PRDSection(BaseModel):
    title: str = ""
    content: str = ""


class PRDModel(BaseModel):
    project_name: str = ""
    overview: str = ""
    target_users: list[str] = Field(default_factory=list)
    feature_list: list[str] = Field(default_factory=list)
    non_functional: list[str] = Field(default_factory=list)
    success_metrics: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)


class ArchitectureModel(BaseModel):
    style: str = ""
    components: list[str] = Field(default_factory=list)
    tech_stack: list[str] = Field(default_factory=list)
    db_schema: list[str] = Field(default_factory=list)
    api_endpoints: list[str] = Field(default_factory=list)
    deployment: str = ""


class PhaseModel(BaseModel):
    phase_number: int = 1
    name: str = ""
    key_features: list[str] = Field(default_factory=list)
    deliverables: list[str] = Field(default_factory=list)
    estimated_duration: str = ""
    estimated_cost: str = ""


class ProjectPlan(BaseModel):
    prd: Optional[PRDModel] = None
    architecture: Optional[ArchitectureModel] = None
    phase_plan: list[PhaseModel] = Field(default_factory=list)
    candidate_id: str = ""
    candidate_title: str = ""
    debate_rounds: int = 0
    consensus_reached: bool = False
