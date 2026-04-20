"""
AADS LLM 클라이언트 — CEO-DIRECTIVES T-002 모델 매트릭스 기반.
Graceful degradation: primary 실패 시 fallback 자동 전환.
"""
import os
import structlog
from typing import Optional, Any

log = structlog.get_logger()

# CEO-DIRECTIVES T-002 모델 ID 매핑 (AADS-156 업데이트)
# 내부 ID → 실제 API 모델 ID 변환 (다른 경우만 매핑, 동일하면 생략)
MODEL_ALIASES = {
    # Anthropic — 버전 접미사 없는 short ID → 실제 API ID
    "claude-haiku-4-5": "claude-haiku-4-5-20251001",   # Haiku 4.5 날짜 버전
    # OpenAI — 내부 별칭 → 실제 API ID (미지원 시 최신 동급 모델로 매핑)
    "gpt-5.2-chat-latest": "gpt-4o",
    "gpt-5.3-codex": "gpt-4o",
    "gpt-5-nano": "gpt-4o-mini",
    # Google — 내부 프리뷰명 → 실제 API ID
    "gemini-3.1-pro-preview": "gemini-1.5-pro",
}


def resolve_model_id(model_id: str) -> str:
    """CEO-DIRECTIVES 모델 ID → 실제 API 모델 ID 변환."""
    return MODEL_ALIASES.get(model_id, model_id)


def create_anthropic_llm(model_id: str, max_tokens: int = 4096, temperature: float = 0.1) -> Any:
    """Anthropic ChatAnthropic LLM 인스턴스 생성 (auth_provider 경유)."""
    from langchain_anthropic import ChatAnthropic
    from app.core.auth_provider import get_primary_token, get_base_url
    api_key = get_primary_token()
    if not api_key:
        raise ValueError("No valid auth token (R-AUTH)")
    real_model = resolve_model_id(model_id)
    kwargs: dict[str, Any] = dict(
        model=real_model,
        api_key=api_key,
        anthropic_api_url=get_base_url(),
        max_tokens=max_tokens,
    )
    # Opus 4.7: temperature/top_p/top_k 미지원 (400 에러) — 제거
    if "opus-4-7" not in real_model:
        kwargs["temperature"] = temperature
    return ChatAnthropic(**kwargs)


def create_openai_llm(model_id: str, max_tokens: int = 4096, temperature: float = 0.1) -> Any:
    """OpenAI ChatOpenAI LLM 인스턴스 생성."""
    from langchain_openai import ChatOpenAI
    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        raise ValueError("OPENAI_API_KEY not set")
    real_model = resolve_model_id(model_id)
    return ChatOpenAI(model=real_model, api_key=api_key, max_tokens=max_tokens, temperature=temperature)


def create_google_llm(model_id: str, max_tokens: int = 4096, temperature: float = 0.1) -> Any:
    """Google ChatGoogleGenerativeAI LLM 인스턴스 생성."""
    from langchain_google_genai import ChatGoogleGenerativeAI
    api_key = os.getenv("GOOGLE_API_KEY", "")
    if not api_key:
        raise ValueError("GOOGLE_API_KEY not set")
    real_model = resolve_model_id(model_id)
    return ChatGoogleGenerativeAI(model=real_model, google_api_key=api_key, max_output_tokens=max_tokens, temperature=temperature)


def create_llm_with_fallback(
    primary_provider: str,
    primary_model: str,
    fallback_provider: Optional[str] = None,
    fallback_model: Optional[str] = None,
    max_tokens: int = 4096,
    temperature: float = 0.1,
) -> Any:
    """
    LLM 인스턴스 생성 (primary → fallback → Anthropic sonnet 최종 fallback).
    """
    def _try_create(provider: str, model: str) -> Optional[Any]:
        try:
            if provider == "anthropic":
                return create_anthropic_llm(model, max_tokens, temperature)
            elif provider == "openai":
                return create_openai_llm(model, max_tokens, temperature)
            elif provider == "google":
                return create_google_llm(model, max_tokens, temperature)
        except Exception as e:
            log.warning("llm_create_failed", provider=provider, model=model, error=str(e))
            return None

    # 1. Primary 시도
    llm = _try_create(primary_provider, primary_model)
    if llm:
        return llm

    # 2. Fallback 시도
    if fallback_provider and fallback_model:
        llm = _try_create(fallback_provider, fallback_model)
        if llm:
            log.info("using_fallback_model", fallback_provider=fallback_provider, fallback_model=fallback_model)
            return llm

    # 3. 최종 Anthropic sonnet fallback
    llm = _try_create("anthropic", "claude-sonnet-4-6")
    if llm:
        log.info("using_final_fallback", model="claude-sonnet-4-6")
        return llm

    raise RuntimeError(f"모든 LLM 생성 실패: primary={primary_provider}/{primary_model}")
