"""
services/model_router.py 단위 테스트 — 커버리지 확대.
ModelConfig, AGENT_MODELS, estimate_cost, get_llm_for_agent 검증.
"""
import pytest
import sys
import os
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))


# ── ModelConfig ───────────────────────────────────────────────────
class TestModelConfig:
    def test_model_config_fields(self):
        from app.services.model_router import ModelConfig
        cfg = ModelConfig(
            provider="anthropic",
            model_id="claude-sonnet-4-5",
            input_cost_per_m=3.0,
            output_cost_per_m=15.0,
        )
        assert cfg.provider == "anthropic"
        assert cfg.model_id == "claude-sonnet-4-5"
        assert cfg.input_cost_per_m == 3.0
        assert cfg.output_cost_per_m == 15.0


# ── AGENT_MODELS 구조 검증 ──────────────────────────────────────
class TestAgentModels:
    def test_all_8_agents_defined(self):
        from app.services.model_router import AGENT_MODELS
        # AADS-125/126 추가: strategist_collect, strategist_analyze, planner
        base_agents = {"pm", "supervisor", "developer", "architect", "qa", "judge", "devops", "researcher"}
        assert base_agents.issubset(set(AGENT_MODELS.keys()))

    def test_each_agent_has_primary_fallback(self):
        from app.services.model_router import AGENT_MODELS
        for agent, models in AGENT_MODELS.items():
            assert "primary" in models, f"{agent} missing primary"
            assert "fallback" in models, f"{agent} missing fallback"

    def test_primary_configs_valid(self):
        from app.services.model_router import AGENT_MODELS, ModelConfig
        for agent, models in AGENT_MODELS.items():
            primary = models["primary"]
            assert isinstance(primary, ModelConfig)
            assert primary.input_cost_per_m > 0
            assert primary.output_cost_per_m > 0


# ── estimate_cost ─────────────────────────────────────────────────
class TestEstimateCost:
    def test_zero_tokens(self):
        from app.services.model_router import estimate_cost, ModelConfig
        cfg = ModelConfig("anthropic", "claude-sonnet-4-5", 3.0, 15.0)
        assert estimate_cost(cfg, 0, 0) == 0.0

    def test_cost_calculation_correct(self):
        from app.services.model_router import estimate_cost, ModelConfig
        cfg = ModelConfig("anthropic", "claude-sonnet-4-5", 3.0, 15.0)
        # 1M input tokens = $3, 1M output = $15
        cost = estimate_cost(cfg, 1_000_000, 1_000_000)
        assert abs(cost - 18.0) < 0.001

    def test_small_token_cost(self):
        from app.services.model_router import estimate_cost, ModelConfig
        cfg = ModelConfig("anthropic", "claude-haiku-4-5", 0.80, 4.0)
        cost = estimate_cost(cfg, 3000, 2000)
        expected = (3000 / 1_000_000) * 0.80 + (2000 / 1_000_000) * 4.0
        assert abs(cost - expected) < 1e-10

    def test_haiku_cheaper_than_sonnet(self):
        from app.services.model_router import estimate_cost, ModelConfig
        haiku = ModelConfig("anthropic", "claude-haiku-4-5", 0.80, 4.0)
        sonnet = ModelConfig("anthropic", "claude-sonnet-4-5", 3.0, 15.0)
        assert estimate_cost(haiku, 5000, 5000) < estimate_cost(sonnet, 5000, 5000)


# ── get_llm_for_agent ─────────────────────────────────────────────
class TestGetLlmForAgent:
    def test_known_agent_returns_llm_and_config(self):
        from app.services.model_router import get_llm_for_agent, ModelConfig
        mock_llm = MagicMock()
        with patch("app.services.model_router._create_llm", return_value=mock_llm):
            llm, config = get_llm_for_agent("developer")
        assert llm is mock_llm
        assert isinstance(config, ModelConfig)

    def test_unknown_agent_uses_developer(self):
        from app.services.model_router import get_llm_for_agent, AGENT_MODELS
        mock_llm = MagicMock()
        with patch("app.services.model_router._create_llm", return_value=mock_llm):
            llm, config = get_llm_for_agent("unknown_xyz")
        # fallback to developer primary
        assert config == AGENT_MODELS["developer"]["primary"]

    def test_fallback_flag_uses_fallback_config(self):
        from app.services.model_router import get_llm_for_agent, AGENT_MODELS
        mock_llm = MagicMock()
        with patch("app.services.model_router._create_llm", return_value=mock_llm):
            _, config = get_llm_for_agent("developer", use_fallback=True)
        assert config == AGENT_MODELS["developer"]["fallback"]

    def test_primary_failure_tries_fallback(self):
        from app.services.model_router import get_llm_for_agent, AGENT_MODELS
        mock_llm = MagicMock()
        call_count = [0]

        def _create_side_effect(cfg):
            call_count[0] += 1
            if call_count[0] == 1:
                raise ConnectionError("Primary unavailable")
            return mock_llm

        with patch("app.services.model_router._create_llm", side_effect=_create_side_effect):
            llm, config = get_llm_for_agent("pm")

        assert llm is mock_llm
        assert config == AGENT_MODELS["pm"]["fallback"]
        assert call_count[0] == 2  # primary 1회 + fallback 1회
