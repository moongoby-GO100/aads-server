"""
AADS-188A: Deep Research Pydantic 모델
ResearchEvent — 스트리밍 이벤트 (AADS-188A: content/sources/phase 필드 추가)
ResearchResult — 최종 결과.
"""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel


class ResearchEvent(BaseModel):
    """딥리서치 스트리밍 이벤트.

    type:
      - start: 리서치 시작
      - planning: 연구 계획 수립 중
      - searching: 소스 탐색 중
      - analyzing: 교차 분석 중
      - thinking: 내부 사고 (백그라운드)
      - content: 보고서 텍스트 청크
      - complete: 최종 완료 (report + sources)
      - error: 오류 발생
    """
    type: Literal["start", "planning", "searching", "analyzing", "thinking", "content", "complete", "error"]
    text: Optional[str] = None          # 이전 호환성 유지
    content: Optional[str] = None       # AADS-188A: 스트리밍 텍스트 / 최종 보고서
    sources: Optional[List[Dict[str, Any]]] = None  # AADS-188A: complete 이벤트 인용 목록
    interaction_id: Optional[str] = None
    phase: Optional[str] = None         # 진행 단계 설명 (예: "소스 탐색 중... (3/15)")
    progress_pct: Optional[int] = None  # 진행률 0~100


class ResearchResult(BaseModel):
    """딥리서치 최종 결과 (API 응답용)."""
    content: str
    interaction_id: str = ""
    status: Literal["completed", "failed", "timeout", "daily_limit"]
    error: Optional[str] = None
    cost_usd: float = 3.0
    elapsed_sec: float = 0.0
    sources: List[Dict[str, Any]] = []  # AADS-188A: 인용 소스 목록
