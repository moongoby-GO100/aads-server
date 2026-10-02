from __future__ import annotations

import pytest

from app.services import tool_executor as tool_executor_module
from app.services.tool_executor import ToolExecutor
from app.services.work_recipe import recorder as recorder_module
from app.services.work_recipe import registration as registration_module
from app.services.work_recipe.schema import parse_recipe

TENANT_ID = "2d701a8c-9596-4757-8588-faa4f7837112"
SESSION_ID = "11111111-1111-1111-1111-111111111111"

VERIFY = [
    {"assertion": "url_equals", "expected": "https://example.com/login",
     "description": "login url"},
    {"assertion": "text_contains", "expected": "Sign in"},
    {"assertion": "element_visible", "selector": "form#login"},
]


@pytest.fixture
def captured(monkeypatch):
    saved = {}

    async def tenant(**kwargs):
        return TENANT_ID

    async def request(recipe, *, tenant_id, requested_by=""):
        saved["spec"] = recipe.to_dict()
        return {"id": "registration-1", "status": "pending", "spec": saved["spec"]}

    monkeypatch.setattr(tool_executor_module, "resolve_bound_tenant_id", tenant)
    monkeypatch.setattr(registration_module, "request_registration", request)
    return saved


async def _register(**extra):
    token = tool_executor_module.current_chat_session_id.set(SESSION_ID)
    try:
        return await ToolExecutor()._smart_browser(
            {
                "action": "register_e2e",
                "recipe_name": "login_screen_check",
                "domain": "example.com",
                "steps": [{"action": "navigate", "url": "https://example.com/login", "risk": "READ"}],
                "e2e_evidence": {
                    "screen_verified": True,
                    "snapshot_ref": "snap-1",
                    **extra.pop("evidence", {}),
                },
                **extra,
            }
        )
    finally:
        tool_executor_module.current_chat_session_id.reset(token)


async def test_top_level_verify_and_flow_metadata_are_persisted(captured):
    result = await _register(
        verify=VERIFY, starts_when="login page open", succeeds_when="form visible", lane="pc_agent"
    )
    assert result["status"] == "approval_required"
    spec = captured["spec"]
    assert [v["assertion"] for v in spec["verify"]] == [
        "url_equals", "text_contains", "element_visible"
    ]
    assert all(v["action"] == "snapshot" and v["risk"] == "READ" for v in spec["verify"])
    assert spec["verify"][2]["selector"] == "form#login"
    meta = spec["metadata"]
    assert meta["starts_when"] == "login page open"
    assert meta["succeeds_when"] == "form visible"
    assert meta["lane"] == "pc_agent"
    assert meta["screen_e2e"]["screen_verified"] is True
    assert parse_recipe(spec).verify  # approval path re-parses the spec


async def test_evidence_verify_is_accepted(captured):
    result = await _register(
        evidence={"verify": VERIFY, "starts_when": "s", "succeeds_when": "t", "lane": "pc_agent"}
    )
    assert result["status"] == "approval_required"
    assert len(captured["spec"]["verify"]) == 3
    assert captured["spec"]["metadata"]["lane"] == "pc_agent"
    assert "verify" not in captured["spec"]["metadata"]["screen_e2e"]


async def test_unknown_assertion_is_rejected_not_dropped(captured):
    result = await _register(verify=[{"assertion": "title_matches", "expected": "x"}])
    assert result["error"].startswith("unsupported_verify_assertion:1:title_matches")
    assert "spec" not in captured


@pytest.mark.parametrize(
    "bad,error",
    [
        ([{"assertion": "url_equals"}], "verify_expected_required:1"),
        ([{"assertion": "element_visible"}], "verify_selector_required:1"),
        ([{"assertion": "text_contains", "expected": "x", "risk": "WRITE_EXTERNAL"}],
         "verify_risk_must_be_read:1"),
        ([{"assertion": "text_contains", "expected": "x", "action": "click"}],
         "unsupported_verify_action:1:click"),
        ([{"assertion": "text_contains", "expected": "x", "evil": 1}],
         "unsupported_verify_field:1:evil"),
        (["url_equals"], "invalid_verify_step:1"),
        ("url_equals", "invalid_verify_spec"),
    ],
)
async def test_malformed_verify_is_rejected(captured, bad, error):
    result = await _register(verify=bad)
    assert result["error"] == error
    assert "spec" not in captured


async def test_conflicting_verify_sources_are_rejected(captured):
    result = await _register(
        verify=VERIFY, evidence={"verify": [{"assertion": "text_contains", "expected": "x"}]}
    )
    assert result["error"] == "verify_conflict"


async def test_non_string_flow_metadata_is_rejected(captured):
    result = await _register(verify=VERIFY, lane=["pc_agent"])
    assert result["error"] == "invalid_lane"


async def test_without_verify_behaviour_is_unchanged(captured):
    result = await _register()
    assert result["status"] == "approval_required"
    spec = captured["spec"]
    assert "verify" not in spec
    assert not {"starts_when", "succeeds_when", "lane"} & set(spec["metadata"])
    assert set(spec["metadata"]) == {
        "recorded", "credentials", "source_chat_session_id", "screen_e2e"
    }


def test_recorder_default_has_no_verify():
    recording = recorder_module.start_recording("n", "example.com", TENANT_ID)
    assert recording.verify == [] and recording.flow_metadata == {}
