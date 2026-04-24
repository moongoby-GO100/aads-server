"""
에이전트별 모델 라우팅 + 비용 추적 + 폴백.
CEO-DIRECTIVES T-002 가격표 기준 프로덕션 매핑 (T-031 업데이트).
AADS-171: LiteLLM Proxy 경유 인텐트→모델 매핑 + 일 $5 비용상한 추가.

T-002 프로덕션 매핑:
  Supervisor/Architect : claude-opus-4-6     ($5/$25)
  PM/Developer/QA      : claude-sonnet-4-6   ($3/$15)
  Judge                : gemini-3.1-pro-preview ($2/$12)  ← Developer와 다른 모델
  DevOps               : gpt-5-mini          ($0.25/$2)
  Researcher           : gemini-2.5-flash    ($0.30/$2.50)
  fallback 체인: primary → alternative → error

LiteLLM Proxy 라우팅 (인텐트 기반):
  LITELLM_BASE_URL: http://aads-litellm:4000/v1  (Docker 내부 네트워크)
  일 $5 초과 시 Opus 비용 이상치 경고 로그만 기록 (v2.1 Q-COST)
  월 $150 초과 시 비용 경고 로그
"""
from dataclasses import dataclass
import os

import structlog

logger = structlog.get_logger(__name__)
_budget_logger = structlog.get_logger("aads.budget")

# LiteLLM Proxy 기본 URL (Docker 내부 네트워크)
LITELLM_BASE_URL = os.environ.get("LITELLM_BASE_URL", "http://aads-litellm:4000/v1")

# 인텐트 → LiteLLM 모델명 매핑 (AADS-171)
INTENT_MODEL_MAP: dict[str, str | None] = {
    "casual":            "gemini-flash-lite",
    "search":            "gemini-flash",
    "deep_research":     "gemini-pro",
    "url_analyze":       "gemini-flash",
    "video_analyze":     "gemini-flash",
    "image_analyze":     "gemini-flash",
    "planning":          "claude-sonnet",
    "decision":          "claude-opus",
    "code_exec":         "gemini-flash",
    "directive_gen":     "claude-sonnet",
    "memory_recall":     "gemini-flash-lite",
    "workspace_switch":  None,  # 모델 불필요
    # 기존 인텐트
    "dashboard":         "gemini-flash-lite",
    "diagnosis":         "claude-sonnet",
    "research":          "gemini-flash",
    "execute":           "claude-sonnet",
    "browser":           "claude-sonnet",
    "strategy":          "claude-opus",
    "qa":                "claude-sonnet",
    "design":            "claude-sonnet",
    "design_fix":        "claude-sonnet",
    "architect":         "claude-opus",
    "execution_verify":  "claude-sonnet",
    "health_check":      "gemini-flash-lite",
}

# 일 $5 초과 시 Opus 비용 이상치만 감지 (v2.1 Q-COST: 강제 차단 없음)
_DAILY_BUDGET_USD = float(os.environ.get("LITELLM_DAILY_BUDGET_USD", "5.0"))
_MONTHLY_BUDGET_WARN_USD = float(os.environ.get("LITELLM_MONTHLY_BUDGET_WARN_USD", "150.0"))


async def get_litellm_daily_spend() -> float:
    """LiteLLM /spend/logs API에서 오늘 누적 비용 조회. 실패 시 0.0 반환."""
    import httpx
    master_key = os.environ.get("LITELLM_MASTER_KEY", "")
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                f"{LITELLM_BASE_URL.replace('/v1', '')}/spend/logs",
                headers={"Authorization": f"Bearer {master_key}"},
                params={"start_date": "today"},
            )
            if resp.status_code == 200:
                data = resp.json()
                return float(data.get("total_cost", 0.0))
    except Exception as e:
        logger.warning("litellm_spend_fetch_failed", error=str(e))
    return 0.0


async def resolve_intent_model(intent: str) -> str | None:
    """
    인텐트를 LiteLLM 모델명으로 변환.
    일 $5 초과 시 경고 로그만 기록 (v2.1 Q-COST: 강제 차단 폐지).
    월 $150 초과 시 경고 로그.
    workspace_switch 등 모델 불필요 인텐트는 None 반환.
    """
    model = INTENT_MODEL_MAP.get(intent, "gemini-flash")

    if model is None:
        return None

    # Opus 사용 시 일 예산 초과 여부 확인
    if model == "claude-opus":
        daily_spend = await get_litellm_daily_spend()
        if daily_spend >= _DAILY_BUDGET_USD:
            _budget_logger.info(
                "daily_budget_exceeded_opus_warning",
                daily_spend=daily_spend,
                limit=_DAILY_BUDGET_USD,
                intent=intent,
                model=model,
            )
            logger.warning(
                "opus_budget_warning",
                intent=intent,
                spend=daily_spend,
                limit=_DAILY_BUDGET_USD,
                model=model,
            )

    return model


async def check_monthly_budget_warning() -> bool:
    """월 $150 초과 여부 확인. 초과 시 경고 로그 + True 반환."""
    import httpx
    master_key = os.environ.get("LITELLM_MASTER_KEY", "")
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                f"{LITELLM_BASE_URL.replace('/v1', '')}/spend/logs",
                headers={"Authorization": f"Bearer {master_key}"},
                params={"start_date": "this_month"},
            )
            if resp.status_code == 200:
                data = resp.json()
                monthly = float(data.get("total_cost", 0.0))
                if monthly >= _MONTHLY_BUDGET_WARN_USD:
                    _budget_logger.warning(
                        "monthly_budget_warning",
                        monthly_spend=monthly,
                        limit=_MONTHLY_BUDGET_WARN_USD,
                    )
                    logger.warning("monthly_budget_warning", spend=monthly, limit=_MONTHLY_BUDGET_WARN_USD)
                    return True
    except Exception as e:
        logger.warning("litellm_monthly_spend_fetch_failed", error=str(e))
    return False


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
        "error":    ModelConfig("anthropic", "claude-haiku-4-5",  1.0,   5.0),
    },
    # Architect: claude-opus-4-6 ($5/$25)
    "architect": {
        "primary":  ModelConfig("anthropic", "claude-opus-4-6",   5.0,  25.0),
        "fallback": ModelConfig("anthropic", "claude-sonnet-4-6", 3.0,  15.0),
        "error":    ModelConfig("anthropic", "claude-haiku-4-5",  1.0,   5.0),
    },
    # PM: claude-sonnet-4-6 ($3/$15)
    "pm": {
        "primary":  ModelConfig("anthropic", "claude-sonnet-4-6", 3.0,  15.0),
        "fallback": ModelConfig("openai",    "gpt-5.2-chat-latest", 5.0, 15.0),
        "error":    ModelConfig("anthropic", "claude-haiku-4-5",  1.0,   5.0),
    },
    # Developer: claude-sonnet-4-6 ($3/$15)
    "developer": {
        "primary":  ModelConfig("anthropic", "claude-sonnet-4-6", 3.0,  15.0),
        "fallback": ModelConfig("openai",    "gpt-5.2-chat-latest", 5.0, 15.0),
        "error":    ModelConfig("anthropic", "claude-haiku-4-5",  1.0,   5.0),
    },
    # QA: claude-sonnet-4-6 ($3/$15)
    "qa": {
        "primary":  ModelConfig("anthropic", "claude-sonnet-4-6", 3.0,  15.0),
        "fallback": ModelConfig("anthropic", "claude-haiku-4-5",  1.0,   5.0),
        "error":    ModelConfig("anthropic", "claude-haiku-4-5",  1.0,   5.0),
    },
    # Judge: gemini-3.1-pro-preview ($2/$12) — Developer/QA와 다른 모델 (T-002)
    "judge": {
        "primary":  ModelConfig("google",    "gemini-3.1-pro-preview", 2.0, 12.0),
        "fallback": ModelConfig("anthropic", "claude-sonnet-4-6",      3.0, 15.0),
        "error":    ModelConfig("anthropic", "claude-haiku-4-5",       1.0,  5.0),
    },
    # DevOps: gpt-5-mini ($0.25/$2)
    "devops": {
        "primary":  ModelConfig("openai",    "gpt-5-mini",         0.25,  2.0),
        "fallback": ModelConfig("anthropic", "claude-haiku-4-5",   1.0,   5.0),
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
