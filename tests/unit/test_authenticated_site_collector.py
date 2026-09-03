import pytest

from app.services.authenticated_site_collector import normalize_origin, normalize_site_profile
from app.services.browser_recipe_registry import normalize_recipe_payload
from app.api.authenticated_site_collector import ResumeIn


def test_site_profile_normalizes_multisite_runtime_and_origin():
    profile = normalize_site_profile({
        "project_key": "go100", "site_key": "Research", "display_name": "리서치 포털",
        "base_origin": "https://Research.Example.com/path", "allowed_origins": ["https://research.example.com"],
        "runtime": "official_api", "data_categories": ["research"],
    })
    assert profile["project_key"] == "GO100"
    assert profile["site_key"] == "research"
    assert profile["base_origin"] == "https://research.example.com"
    assert profile["runtime"] == "official_api"


@pytest.mark.parametrize("origin", ["javascript:alert(1)", "https://user:password@example.com", "file:///tmp/a"])
def test_origin_rejects_unsafe_or_secret_bearing_values(origin):
    with pytest.raises(ValueError, match="valid_http_origin_required"):
        normalize_origin(origin)


def test_collector_recipe_preserves_saas_fields():
    recipe = normalize_recipe_payload({
        "recipe_id": "go100.research", "version": "v2", "service": "research",
        "allowed_origins": ["https://research.example.com"], "project_key": "GO100",
        "site_environment": "official_api", "record_types": ["report"],
        "normalization_schema": {"type": "object"}, "fixture_cases": [{"name": "empty"}],
        "version_status": "active",
    })
    assert recipe["project_key"] == "GO100"
    assert recipe["site_environment"] == "official_api"
    assert recipe["record_types"] == ["report"]
    assert recipe["version_status"] == "active"


def test_resume_contract_rejects_challenge_answer_fields():
    with pytest.raises(Exception):
        ResumeIn(resolution="completed", otp="123456")
