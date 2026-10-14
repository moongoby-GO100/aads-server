"""배달앱 4사 조각 레시피 초안 카탈로그 계약."""
from __future__ import annotations

import json

import pytest

from app.services import delivery_recipe_fragments as frag

REQUIRED = {"name", "version", "lane", "work_key", "credential_scope", "starts_when", "succeeds_when",
            "verify_requirements", "on_failure", "registration_state", "auto_approve"}


def test_four_apps_five_fragments_each_and_no_raileon():
    catalog = frag.build_catalog()
    assert set(catalog) == {"baemin", "coupangeats", "yogiyo", "ddangyo"}
    for service, pieces in catalog.items():
        assert [p["fragment_no"] for p in pieces] == ["01", "02", "03", "04", "05"]
        assert all(p["service"] == service for p in pieces)
    assert "라일론" not in json.dumps(catalog, ensure_ascii=False)


@pytest.mark.parametrize("service", frag.DELIVERY_RECIPE_SERVICES)
def test_every_fragment_is_pending_draft_with_flow_metadata(service):
    for piece in frag.build_catalog()[service]:
        assert REQUIRED <= set(piece), piece["name"]
        assert piece["auto_approve"] is False
        assert piece["registration_state"] == "draft_pending_approval"
        assert piece["lane"] == "pc_agent"
        assert piece["starts_when"] and piece["succeeds_when"]
        assert piece["on_failure"]["return_to"]


def test_fragment_kinds_and_risk_levels():
    pieces = frag.build_catalog()["baemin"]
    assert [p["max_risk"] for p in pieces] == ["READ", "WRITE_EXTERNAL", "WRITE_EXTERNAL", "READ", "READ"]
    assert pieces[0]["name"] == "baemin_01_open_login"
    assert pieces[3]["name"] == "baemin_04_sales_settlement_query"
    assert pieces[4]["name"] == "baemin_05_review_ads_query"


def test_login_fragments_stop_on_user_action_codes_and_never_auto_retry():
    for pieces in frag.build_catalog().values():
        for piece in pieces[:3]:
            assert piece["on_failure"]["scheduler_retry"] is False
            assert "PC_AGENT_LOGIN_REQUIRED" in piece["on_failure"]["user_action_codes"]
            assert "PORTAL_BLOCKED" in piece["on_failure"]["user_action_codes"]


def test_credentials_are_referenced_by_scope_name_only():
    text = json.dumps(frag.build_catalog(), ensure_ascii=False).lower()
    for forbidden in ("password\": \"", "cookie\": \"", "token\": \"", "sk-ant", "api_key"):
        assert forbidden not in text
    for service, pieces in frag.build_catalog().items():
        assert {p["credential_scope"] for p in pieces} == {f"yeoljeong-{service}"}


def test_coupangeats_reuses_existing_login_fragments_and_keeps_csv_fallback():
    from app.services.browser_recipe_registry import COUPANGEATS_LOGIN_FRAGMENTS

    pieces = frag.build_catalog()["coupangeats"]
    existing = {item["name"]: item["version"] for item in COUPANGEATS_LOGIN_FRAGMENTS}
    for piece in pieces[:3]:
        assert existing[piece["name"]] == piece["version"]
    assert all(p["csv_fallback"]["kind"] == "settlement_csv_upload" for p in pieces)
    assert "csv_fallback" not in frag.build_catalog()["baemin"][0]


def test_lane_work_key_is_per_app_so_apps_run_sequentially_per_lane():
    keys = {s: {p["work_key"] for p in pieces} for s, pieces in frag.build_catalog().items()}
    assert all(len(v) == 1 for v in keys.values())
    assert len({next(iter(v)) for v in keys.values()}) == 4


def test_register_e2e_inputs_and_unknown_name():
    data = frag.register_e2e_inputs("yogiyo_04_sales_settlement_query")
    assert data["lane"] == "pc_agent" and data["browser_work_key"].startswith("yogiyo:")
    with pytest.raises(KeyError):
        frag.register_e2e_inputs("nope")
