from pathlib import Path


API_FILE = Path("app/api/llm_models.py")
DASHBOARD_PAGE = Path("aads-dashboard/src/app/admin/model-routing/page.tsx")
DASHBOARD_API = Path("aads-dashboard/src/lib/api.ts")
SIDEBAR = Path("aads-dashboard/src/components/Sidebar.tsx")
SETTINGS_PAGE = Path("aads-dashboard/src/app/settings/page.tsx")
PRESENTATION_UTIL = Path("aads-dashboard/src/lib/modelRegistryPresentation.ts")


def test_model_routing_api_schema_and_write_endpoint_are_registered():
    source = API_FILE.read_text()

    assert "class ModelRoutingPreferenceInput" in source
    assert '@router.get("/routing-preferences")' in source
    assert '@router.put("/routing-preferences")' in source
    assert "model_routing_preferences" in source
    assert "chat_model_preferences" in source
    assert 'item.route_key == "llm"' in source


def test_model_routing_admin_page_exposes_required_model_fields():
    source = DASHBOARD_PAGE.read_text()
    api_source = DASHBOARD_API.read_text()
    sidebar_source = SIDEBAR.read_text()

    for text in (
        "이미지",
        "동영상",
        "검색",
        "딥리서치",
        "임베딩",
        "배경 LLM",
        "route_keys",
        "LLM",
        "provider",
        "model_id",
        "availability",
        "is_enabled",
        "is_default",
        "routeStats",
        "Registry",
        "getProviderDisplayLabel",
        "getModelFamilyLabel",
        "getModelCategoryLabel",
    ):
        assert text in source
    assert "getModelRoutingPreferences" in api_source
    assert "updateModelRoutingPreferences" in api_source
    assert "/admin/model-routing" in sidebar_source


def test_runner_settings_page_uses_registry_classification_with_legacy_value_fallback():
    source = SETTINGS_PAGE.read_text()
    util_source = PRESENTATION_UTIL.read_text()

    for text in (
        "LEGACY_RUNNER_MODEL_VALUES",
        "getLlmModels",
        "findRegistryModelByStoredValue",
        "runnerModelCatalogGroups",
        "splitStoredModelValue",
        "Legacy / Unclassified",
    ):
        assert text in source or text in util_source
