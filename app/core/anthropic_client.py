"""
중앙 Anthropic 클라이언트 팩토리.

OAuth 토큰으로 Anthropic API 직접 호출.
백그라운드 시스템(self_evaluator, fact_extractor, compaction 등)에서 사용.
"""
import os
import logging
from anthropic import AsyncAnthropic

logger = logging.getLogger(__name__)

# OAuth 토큰 직접 사용 (Agent SDK 채팅 AI와 동일 경로)
_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
_BASE_URL = os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com")


def get_client(model_hint: str = "claude-haiku") -> AsyncAnthropic:
    """Anthropic API 직접 클라이언트 반환 (OAuth 토큰 인증)."""
    return AsyncAnthropic(
        api_key=_API_KEY,
        base_url=_BASE_URL,
    )
