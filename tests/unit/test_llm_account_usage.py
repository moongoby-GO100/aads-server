from app.services import llm_account_usage


def test_classify_provider_from_model_handles_prefixed_runtime_models():
    cases = {
        "codex:gpt-5.4": "codex",
        "litellm:gemini-2.5-flash": "gemini",
        "litellm:openrouter-grok-4-fast": "openrouter",
        "litellm:kimi-k2": "kimi",
        "litellm:minimax-m2.7": "minimax",
        "litellm:groq-qwen3-32b": "groq",
    }

    for model_name, expected in cases.items():
        assert llm_account_usage._classify_provider_from_model(model_name) == expected


def test_classify_provider_from_model_handles_unprefixed_litellm_runtime_names():
    cases = {
        "gemini-2.5-flash": "gemini",
        "openrouter-grok-4-fast": "openrouter",
        "kimi-k2": "kimi",
        "minimax-m2.7": "minimax",
        "groq-qwen3-32b": "groq",
        "gpt-5.4": "codex",
    }

    for model_name, expected in cases.items():
        assert llm_account_usage._classify_provider_from_model(model_name) == expected


def test_provider_display_names_cover_new_providers():
    assert llm_account_usage._display_name_for_provider("kimi") == "Kimi"
    assert llm_account_usage._display_name_for_provider("minimax") == "MiniMax"
    assert llm_account_usage._display_name_for_provider("codex") == "Codex CLI"
