"""
중앙 Anthropic 클라이언트 팩토리.

모든 Claude 호출을 LiteLLM 프록시 경유로 처리합니다.
LiteLLM은 .env.litellm의 ANTHROPIC_API_KEY(OAuth 토큰)로 Anthropic API에 접근합니다.
"""
import os
import logging
import httpx
from anthropic import AsyncAnthropic

logger = logging.getLogger(__name__)

_LITELLM_BASE_URL = os.getenv("LITELLM_BASE_URL", "http://litellm:4000")
_LITELLM_API_KEY = os.getenv("LITELLM_MASTER_KEY", "sk-litellm")


class _StripAuthTransport(httpx.AsyncBaseTransport):
    """SDK 자동 Authorization 헤더 제거 — LiteLLM x-api-key만 사용."""
    def __init__(self):
        self._inner = httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request):
        raw = [(k, v) for k, v in request.headers.raw if k.lower() != b"authorization"]
        request.headers = httpx.Headers(raw)
        return await self._inner.handle_async_request(request)


def get_client(model_hint: str = "claude-haiku") -> AsyncAnthropic:
    """LiteLLM 경유 Anthropic 클라이언트 반환."""
    return AsyncAnthropic(
        api_key=_LITELLM_API_KEY,
        base_url=_LITELLM_BASE_URL,
        http_client=httpx.AsyncClient(transport=_StripAuthTransport()),
    )
