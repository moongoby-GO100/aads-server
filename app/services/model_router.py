"""
에이전트별 모델 라우팅 + 비용 추적 + 폴백.
CEO-DIRECTIVES T-002 가격표 기준 프로덕션 매핑 (T-031 업데이트).

T-002 프로덕션 매핑:
  Supervisor/Architect : claude-opus-4-6     ($5/$25)
  PM/Developer/QA      : claude-sonnet-4-6   ($3/$15)
  Judge                : gemini-3.1-pro-preview ($2/$12)  ← Developer와 다른 모델
  DevOps               : gpt-5-mini          ($0.25/$2)
  Researcher           : gemini-2.5-flash    ($0.30/$2.50)
  fallback 체인: primary → alternative → error
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


# T-002 에이전트별 모델 매트릭스 — CEO-DIRECTIVES v2.1 프로덕션 기준 (T-031)
AGENT_MODELS: dict[str, dict[str, ModelConfig]] = {
    # Supervisor: claude-opus-4-6 ($5/$25)
    "supervisor": {
        "primary":  ModelConfig("anthropic", "claude-opus-4-6",   5.0,  25.0),
        "fallback": ModelConfig("anthropic", "claude-sonnet-4-6", 3.0,  15.0),
        "error":    ModelConfig("anthropic", "claude-haiku-4-5",  0.80,  4.0),
    },
    # Architect: claude-opus-4-6 ($5/$25)
    "architect": {
        "primary":  ModelConfig("anthropic", "claude-opus-4-6",   5.0,  25.0),
        "fallback": ModelConfig("anthropic", "claude-sonnet-4-6", 3.0,  15.0),
        "error":    ModelConfig("anthropic", "claude-haiku-4-5",  0.80,  4.0),
    },
    # PM: claude-sonnet-4-6 ($3/$15)
    "pm": {
        "primary":  ModelConfig("anthropic", "claude-sonnet-4-6", 3.0,  15.0),
        "fallback": ModelConfig("openai",    "gpt-5.2-chat-latest", 5.0, 15.0),
        "error":    ModelConfig("anthropic", "claude-haiku-4-5",  0.80,  4.0),
    },
    # Developer: claude-sonnet-4-6 ($3/$15)
    "developer": {
        "primary":  ModelConfig("anthropic", "claude-sonnet-4-6", 3.0,  15.0),
        "fallback": ModelConfig("openai",    "gpt-5.2-chat-latest", 5.0, 15.0),
        "error":    ModelConfig("anthropic", "claude-haiku-4-5",  0.80,  4.0),
    },
    # QA: claude-sonnet-4-6 ($3/$15)
    "qa": {
        "primary":  ModelConfig("anthropic", "claude-sonnet-4-6", 3.0,  15.0),
        "fallback": ModelConfig("anthropic", "claude-haiku-4-5",  0.80,  4.0),
        "error":    ModelConfig("anthropic", "claude-haiku-4-5",  0.80,  4.0),
    },
    # Judge: gemini-3.1-pro-preview ($2/$12) — Developer/QA와 다른 모델 (T-002)
    "judge": {
        "primary":  ModelConfig("google",    "gemini-3.1-pro-preview", 2.0, 12.0),
        "fallback": ModelConfig("anthropic", "claude-sonnet-4-6",      3.0, 15.0),
        "error":    ModelConfig("anthropic", "claude-haiku-4-5",       0.80, 4.0),
    },
    # DevOps: gpt-5-mini ($0.25/$2)
    "devops": {
        "primary":  ModelConfig("openai",    "gpt-5-mini",         0.25,  2.0),
        "fallback": ModelConfig("anthropic", "claude-haiku-4-5",   0.80,  4.0),
        "error":    ModelConfig("anthropic", "claude-sonnet-4-6",  3.0,  15.0),
    },
    # Researcher: gemini-2.5-flash ($0.30/$2.50)
    "researcher": {
        "primary":  ModelConfig("google",    "gemini-2.5-flash",   0.30,  2.50),
        "fallback": ModelConfig("anthropic", "claude-haiku-4-5",   0.80,  4.0),
        "error":    ModelConfig("anthropic", "claude-sonnet-4-6",  3.0,  15.0),
    },
    # Strategist 수집: gemini-2.5-flash ($0.30/$2.50) — 비용 효율 (AADS-125)
    "strategist_collect": {
        "primary":  ModelConfig("google",    "gemini-2.5-flash",   0.30,  2.50),
        "fallback": ModelConfig("anthropic", "claude-haiku-4-5",   0.80,  4.0),
        "error":    ModelConfig("anthropic", "claude-sonnet-4-6",  3.0,  15.0),
    },
    # Strategist 분석: claude-opus-4.6 ($5/$25) — 고품질 전략 분석 (AADS-125)
    "strategist_analyze": {
        "primary":  ModelConfig("anthropic", "claude-opus-4-6",    5.0,  25.0),
        "fallback": ModelConfig("anthropic", "claude-sonnet-4-6",  3.0,  15.0),
        "error":    ModelConfig("anthropic", "claude-haiku-4-5",   0.80,  4.0),
    },
    # Planner: claude-sonnet-4-6 ($3/$15) — PRD/아키텍처/Phase 생성 (AADS-126)
    "planner": {
        "primary":  ModelConfig("anthropic", "claude-sonnet-4-6",  3.0,  15.0),
        "fallback": ModelConfig("anthropic", "claude-haiku-4-5",   0.80,  4.0),
        "error":    ModelConfig("anthropic", "claude-haiku-4-5",   0.80,  4.0),
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
    use_fallback: bool = False,
    use_error_fallback: bool = False,
) -> tuple:
    """
    Returns (llm_instance, model_config).
    fallback 체인: primary → fallback → error
    비용 계산을 위해 config도 함께 반환.
    """
    if agent_role not in AGENT_MODELS:
        # 알 수 없는 role은 developer로 fallback
        logger.warning("unknown_agent_role", role=agent_role, fallback="developer")
        agent_role = "developer"

    models = AGENT_MODELS[agent_role]

    if use_error_fallback:
        key = "error"
    elif use_fallback:
        key = "fallback"
    else:
        key = "primary"

    config = models[key]
    try:
        llm = _create_llm(config)
        logger.debug(
            "llm_selected",
            agent=agent_role,
            tier=key,
            model=config.model_id,
            provider=config.provider,
        )
        return llm, config
    except Exception as e:
        if key == "primary":
            logger.warning(
                "primary_model_failed",
                agent=agent_role,
                model=config.model_id,
                error=str(e),
                action="trying_fallback",
            )
            return get_llm_for_agent(agent_role, use_fallback=True)
        elif key == "fallback":
            logger.warning(
                "fallback_model_failed",
                agent=agent_role,
                model=config.model_id,
                error=str(e),
                action="trying_error_fallback",
            )
            return get_llm_for_agent(agent_role, use_error_fallback=True)
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


def get_model_matrix_summary() -> dict:
    """T-002 모델 매트릭스 요약 반환 (검증용)."""
    summary = {}
    for agent, tiers in AGENT_MODELS.items():
        summary[agent] = {
            tier: {
                "provider": cfg.provider,
                "model_id": cfg.model_id,
                "input_$/M": cfg.input_cost_per_m,
                "output_$/M": cfg.output_cost_per_m,
            }
            for tier, cfg in tiers.items()
        }
    return summary
