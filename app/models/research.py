"""
AADS-186E2: Deep Research Pydantic 모델
ResearchEvent — 스트리밍 이벤트, ResearchResult — 최종 결과.
"""
from __future__ import annotations

from typing import Literal, Optional
from pydantic import BaseModel


class ResearchEvent(BaseModel):
    """딥리서치 스트리밍 이벤트."""
    type: Literal["start", "thinking", "content", "complete", "error"]
    text: Optional[str] = None
    interaction_id: Optional[str] = None


class ResearchResult(BaseModel):
    """딥리서치 최종 결과 (API 응답용)."""
    content: str
    interaction_id: str = ""
    status: Literal["completed", "failed", "timeout", "daily_limit"]
    error: Optional[str] = None
    cost_usd: float = 3.0
    elapsed_sec: float = 0.0
