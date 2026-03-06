"""
AADS-130: 프로젝트 산출물 Pydantic 모델.
project_artifacts 테이블 스키마.
"""
from __future__ import annotations
from typing import Optional
from pydantic import BaseModel, Field

ARTIFACT_TYPES = frozenset({
    "strategy_report",
    "prd",
    "architecture",
    "phase_plan",
    "taskspec",
    "code",
    "test_result",
    "deployment",
})


class ProjectArtifact(BaseModel):
    id: Optional[int] = None
    project_id: str = ""
    artifact_type: str = Field(..., description=f"유형: {', '.join(sorted(ARTIFACT_TYPES))}")
    artifact_name: str = ""
    content: dict = Field(default_factory=dict)
    source_agent: Optional[str] = None
    source_task: Optional[str] = None
    version: int = 1
    created_at: Optional[str] = None
