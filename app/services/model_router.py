"""
에이전트별 모델 라우팅 + 비용 추적 + 폴백.
CEO-DIRECTIVES T-002 가격표 기준.
"""
from dataclasses import dataclass
from typing import Optional
import structlog

logger = structlog.get_logger()


@dataclass
class ModelConfig:
    provider: str          # "anthropic" | "openai" | "google"
    model_id: str
    input_cost_per_m: float   # $/1M input tokens
    output_cost_per_m: float  # $/1M output tokens


# T-002 에이전트별 모델 매트릭스 (CEO-DIRECTIVES v2.1 가격표 기준)
AGENT_MODELS: dict[str, dict[str, ModelConfig]] = {
    "pm": {
        "primary": ModelConfig("anthropic", "claude-sonnet-4-6", 3.0, 15.0),
        "fallback": ModelConfig("openai", "gpt-5.2-chat-latest", 1.75, 14.0),
    },
    "supervisor": {
        "primary": ModelConfig("anthropic", "claude-opus-4-6", 5.0, 25.0),
        "fallback": ModelConfig("google", "gemini-3.1-pro-preview", 2.0, 12.0),
    },
    "developer": {
        "primary": ModelConfig("anthropic", "claude-sonnet-4-6", 3.0, 15.0),
        "fallback": ModelConfig("openai", "gpt-5.3-codex", 1.75, 14.0),
    },
    "architect": {
        "primary": ModelConfig("anthropic", "claude-opus-4-6", 5.0, 25.0),
        "fallback": ModelConfig("google", "gemini-3.1-pro-preview", 2.0, 12.0),
    },
    "qa": {
        "primary": ModelConfig("anthropic", "claude-sonnet-4-6", 3.0, 15.0),
        "fallback": ModelConfig("openai", "gpt-5-mini", 0.25, 2.0),
    },
    "judge": {
        "primary": ModelConfig("anthropic", "claude-sonnet-4-6", 3.0, 15.0),
        "fallback": ModelConfig("google", "gemini-3.1-pro-preview", 2.0, 12.0),
    },
    "devops": {
        "primary": ModelConfig("openai", "gpt-5-mini", 0.25, 2.0),
        "fallback": ModelConfig("anthropic", "claude-haiku-4-5", 1.0, 5.0),
    },
    "researcher": {
        "primary": ModelConfig("google", "gemini-2.5-flash", 0.30, 2.50),
        "fallback": ModelConfig("openai", "gpt-5-nano", 0.05, 0.40),
    },
}


def _create_llm(config: ModelConfig):
    """프로바이더별 LLM 인스턴스 생성."""
    from app.config import settings

    if config.provider == "anthropic":
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(
            model=config.model_id,
            api_key=settings.ANTHROPIC_API_KEY.get_secret_value(),
            max_tokens=8192,
            temperature=0.1,
        )
    elif config.provider == "openai":
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(
            model=config.model_id,
            api_key=settings.OPENAI_API_KEY.get_secret_value(),
            max_tokens=8192,
            temperature=0.1,
        )
    elif config.provider == "google":
        from langchain_google_genai import ChatGoogleGenerativeAI
        return ChatGoogleGenerativeAI(
            model=config.model_id,
            google_api_key=settings.GOOGLE_API_KEY.get_secret_value(),
            max_output_tokens=8192,
            temperature=0.1,
        )
    else:
        raise ValueError(f"Unknown provider: {config.provider}")


def get_llm_for_agent(
    agent_role: str,
    use_fallback: bool = False
) -> tuple:
    """
    Returns (llm_instance, model_config).
    비용 계산을 위해 config도 함께 반환.
    """
    if agent_role not in AGENT_MODELS:
        raise ValueError(f"Unknown agent role: {agent_role}")

    models = AGENT_MODELS[agent_role]
    key = "fallback" if use_fallback else "primary"
    config = models[key]
    try:
        llm = _create_llm(config)
        return llm, config
    except Exception as e:
        if not use_fallback:
            logger.warning(
                "primary_model_failed",
                agent=agent_role,
                error=str(e),
                action="trying_fallback",
            )
            return get_llm_for_agent(agent_role, use_fallback=True)
        raise


def estimate_cost(
    config: ModelConfig,
    input_tokens: int,
    output_tokens: int,
) -> float:
    """토큰 수 기반 비용 추정."""
    return (
        (input_tokens / 1_000_000) * config.input_cost_per_m
        + (output_tokens / 1_000_000) * config.output_cost_per_m
    )
