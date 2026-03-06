"""
AADS-130: Execution Chain 모듈 — 서브그래프 B.
기존 8-Agent graph (builder.py) 래핑하여 모듈화된 경로 제공.

8-Agent 실행 체인:
  PM → Architect → Developer → QA → Judge → DevOps
  (Supervisor, Researcher 지원)
"""
from __future__ import annotations

import structlog

logger = structlog.get_logger()


def build_execution_chain(checkpointer=None):
    """8-Agent 실행 체인 그래프 빌드.

    기존 app.graph.builder.build_aads_graph 를 래핑.
    checkpointer: LangGraph 체크포인터 (PostgresSaver 등)
    """
    from app.graph.builder import compile_graph
    return compile_graph(checkpointer=checkpointer)


async def get_compiled_execution_chain(checkpointer=None):
    """비동기 컴파일 래퍼."""
    from app.graph.builder import compile_graph
    return await compile_graph(checkpointer=checkpointer)


__all__ = ["build_execution_chain", "get_compiled_execution_chain"]
