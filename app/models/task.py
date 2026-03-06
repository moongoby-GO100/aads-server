"""
AADS-130: Task 모델 모듈 — TaskSpec, JudgeVerdict.
기존 app.graph.state 에서 모델 분리하여 독립 import 가능.
"""
from __future__ import annotations

# TaskSpec, JudgeVerdict는 graph.state 에서 정의된 원본을 re-export
from app.graph.state import TaskSpec, JudgeVerdict

__all__ = ["TaskSpec", "JudgeVerdict"]
