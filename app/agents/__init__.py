"""
AADS-130: Agents 패키지 — BaseAgent + 10개 전문 에이전트 모듈.
각 에이전트는 독립 import/테스트 가능.
"""
from __future__ import annotations

import structlog
from abc import ABC, abstractmethod
from typing import Any


class BaseAgent(ABC):
    """공통 베이스 에이전트 클래스.

    모든 AADS 에이전트가 상속하는 인터페이스.
    - 에이전트 이름/역할 정의
    - 비용 추적 인터페이스
    - 로깅 표준화
    """

    agent_name: str = "base"
    agent_role: str = "기본 에이전트"

    def __init__(self):
        self.logger = structlog.get_logger().bind(agent=self.agent_name)

    @abstractmethod
    async def run(self, state: Any) -> Any:
        """에이전트 실행 — 서브클래스에서 구현."""
        raise NotImplementedError

    def log_start(self, **kwargs):
        self.logger.info(f"{self.agent_name}_start", **kwargs)

    def log_end(self, cost_usd: float = 0.0, **kwargs):
        self.logger.info(f"{self.agent_name}_end", cost_usd=cost_usd, **kwargs)

    def log_error(self, error: Exception, **kwargs):
        self.logger.error(f"{self.agent_name}_error", error=str(error), **kwargs)


__all__ = ["BaseAgent"]
