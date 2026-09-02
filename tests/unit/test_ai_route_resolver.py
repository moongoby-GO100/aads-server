from app.services.ai_route_resolver import (
    AI_ROUTE_KEYS,
    GOOGLE_PROVIDERS,
    ROUTE_GROUPS,
    normalize_embedding_dimension,
)


def test_ai_route_keys_cover_ceo_requested_capabilities():
    for route_key in (
        "search",
        "deep_research",
        "image_analyze",
        "video_analyze",
        "image",
        "edit_image",
        "video",
        "embedding",
        "background_llm",
        "runner_llm",
        "fact_check",
        "code_exec",
    ):
        assert route_key in AI_ROUTE_KEYS


def test_google_providers_are_explicit_policy_group():
    assert GOOGLE_PROVIDERS == {"google", "gemini"}


def test_route_groups_cover_all_route_keys():
    missing = set(AI_ROUTE_KEYS) - set(ROUTE_GROUPS)
    assert missing == set()


def test_embedding_dimension_is_stable_at_768():
    assert len(normalize_embedding_dimension([1.0, 2.0], 768)) == 768
    assert normalize_embedding_dimension([1.0] * 800, 768) == [1.0] * 768
