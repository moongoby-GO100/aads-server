"""
에이전트별 모델 라우팅 + 비용 추적 + 폴백.
CEO-DIRECTIVES T-002 가격표 기준.
"""
from dataclasses import dataclass
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
        "primary": ModelConfig("anthropic", "claude-sonnet-4-5", 3.0, 15.0),
        "fallback": ModelConfig("openai", "gpt-4o", 5.0, 15.0),
    },
    "supervisor": {
        "primary": ModelConfig("anthropic", "claude-opus-4-5", 15.0, 75.0),
        "fallback": ModelConfig("anthropic", "claude-sonnet-4-5", 3.0, 15.0),
    },
    "developer": {
        "primary": ModelConfig("anthropic", "claude-sonnet-4-5", 3.0, 15.0),
        "fallback": ModelConfig("openai", "gpt-4o", 5.0, 15.0),
    },
    "architect": {
        "primary": ModelConfig("anthropic", "claude-sonnet-4-5", 3.0, 15.0),
        "fallback": ModelConfig("anthropic", "claude-haiku-4-5", 0.80, 4.0),
    },
    "qa": {
        "primary": ModelConfig("anthropic", "claude-sonnet-4-5", 3.0, 15.0),
        "fallback": ModelConfig("anthropic", "claude-haiku-4-5", 0.80, 4.0),
    },
    "judge": {
        "primary": ModelConfig("anthropic", "claude-sonnet-4-5", 3.0, 15.0),
        "fallback": ModelConfig("anthropic", "claude-haiku-4-5", 0.80, 4.0),
    },
    "devops": {
        "primary": ModelConfig("anthropic", "claude-haiku-4-5", 0.80, 4.0),
        "fallback": ModelConfig("anthropic", "claude-sonnet-4-5", 3.0, 15.0),
    },
    "researcher": {
        "primary": ModelConfig("anthropic", "claude-haiku-4-5", 0.80, 4.0),
        "fallback": ModelConfig("anthropic", "claude-sonnet-4-5", 3.0, 15.0),
    },
}


def _create_llm(config: ModelConfig):
    """프로바이더별 LLM 인스턴스 생성."""
    from app.llm.client import create_llm_with_fallback
    return create_llm_with_fallback(
        primary_provider=config.provider,
        primary_model=config.model_id,
        max_tokens=4096,
        temperature=0.1,
    )


def get_llm_for_agent(
    agent_role: str,
    use_fallback: bool = False
) -> tuple:
    """
    Returns (llm_instance, model_config).
    비용 계산을 위해 config도 함께 반환.
    """
    if agent_role not in AGENT_MODELS:
        # 알 수 없는 role은 developer로 fallback
        agent_role = "developer"

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
