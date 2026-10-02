from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path

import pytest

from app.services.work_recipe import auto_approve, auto_record, registration
from app.services.work_recipe import recorder as recorder_module
from tests.unit.test_smart_browser_auto_record import (
    NAV_OK,
    SHOT_OK,
    SNAP_OK,
    TENANT,
    FakePool,
    run_flow,
)

DOMAIN = "shop.example.com"
READ_STEPS = [
    {"action": "navigate", "url": f"https://{DOMAIN}/orders"},
    {"action": "navigate", "url": f"https://{DOMAIN}/orders/history"},
]
REG_ID = str(uuid.uuid4())


async def build_row(monkeypatch, steps=READ_STEPS, domain=DOMAIN, sessions=("s-a", "s-b")):
    """auto_record 가 실제로 만드는 초안(spec/dry_run)과 그에 대응하는 장부 행."""
    captured = {}

    async def capture(recipe, **kwargs):
        captured["recipe"] = recipe
        return {}

    monkeypatch.setattr(recorder_module.registration, "request_registration", capture)
    seq = auto_record.CompletedSequence(
        domain=domain, steps=[dict(s) for s in steps], signature=auto_record.signature_of(domain, steps)
    )
    await auto_record._build_recording(seq, TENANT, sessions[0], len(sessions)).finish_recording()
    recipe = captured["recipe"]
    row = {
        "id": REG_ID, "status": "pending", "name": recipe.name, "domain": domain,
        "spec": recipe.to_dict(),
        "dry_run": registration.build_dry_run(recipe, proposed_version=1),
    }
    traces = [
        {"id": i + 1, "chat_session_id": s, "steps": json.dumps(seq.steps)}
        for i, s in enumerate(sessions)
    ]
    return row, traces


@pytest.fixture
def flag_on(monkeypatch):
    monkeypatch.setenv(auto_approve.FLAG_ENV, "1")
    monkeypatch.delenv(auto_approve.MIN_SESSIONS_ENV, raising=False)


async def test_read_only_two_sessions_is_eligible_with_evidence(monkeypatch, flag_on):
    row, traces = await build_row(monkeypatch)
    verdict = auto_approve.evaluate(row, traces)
    assert verdict.eligible, verdict.reasons
    assert verdict.evidence["session_ids"] == ["s-a", "s-b"]
    assert verdict.evidence["evidence_refs"] == [
        "smart_browser_auto_traces:1", "smart_browser_auto_traces:2",
    ]
    assert verdict.evidence["max_risk"] == "READ"


async def test_same_session_twice_stays_pending(monkeypatch, flag_on):
    row, traces = await build_row(monkeypatch, sessions=("s-a", "s-a"))
    verdict = auto_approve.evaluate(row, traces)
    assert not verdict.eligible and "sessions_below_minimum" in verdict.reasons


async def test_min_sessions_env_cannot_go_below_two(monkeypatch, flag_on):
    monkeypatch.setenv(auto_approve.MIN_SESSIONS_ENV, "1")
    row, traces = await build_row(monkeypatch)
    assert auto_approve.evaluate(row, traces[:1]).reasons == ["sessions_below_minimum"]


@pytest.mark.parametrize(
    ("steps", "expected"),
    [
        (
            [*READ_STEPS, {"action": "click", "selector": "button.go"}],
            "risk_not_read",
        ),
        (
            [READ_STEPS[0], {"action": "fill", "selector": "#q", "variable": "fill_1"}],
            "risk_not_read",
        ),
        (
            [READ_STEPS[0], {"action": "fill", "selector": "input[type=password]", "variable": "f", "credential": True}],
            "secret_input",
        ),
    ],
)
async def test_write_and_secret_recipes_stay_pending(monkeypatch, flag_on, steps, expected):
    row, traces = await build_row(monkeypatch, steps=steps)
    verdict = auto_approve.evaluate(row, traces)
    assert not verdict.eligible
    assert expected in verdict.reasons
    assert "trace_has_interaction" in verdict.reasons  # 장부에서도 조회 전용이 아니다


async def test_login_path_stays_pending(monkeypatch, flag_on):
    steps = [READ_STEPS[0], {"action": "navigate", "url": f"https://{DOMAIN}/login"}]
    row, traces = await build_row(monkeypatch, steps=steps)
    verdict = auto_approve.evaluate(row, traces)
    assert not verdict.eligible and "login_step" in verdict.reasons


@pytest.mark.parametrize(
    "domain",
    ["pay.example.com", "www.kakaopay.com", "admin.example.com", "console.example.com",
     "bank.example.com", "localhost", "10.0.0.5"],
)
async def test_blocked_domains_stay_pending(monkeypatch, flag_on, domain):
    steps = [{"action": "navigate", "url": f"https://{domain}/a"},
             {"action": "navigate", "url": f"https://{domain}/b"}]
    row, traces = await build_row(monkeypatch, steps=steps, domain=domain)
    verdict = auto_approve.evaluate(row, traces)
    assert not verdict.eligible
    assert any(r.startswith("domain_") for r in verdict.reasons)


@pytest.mark.parametrize("path", ["/checkout/start", "/ko/결제/내역", "/admin", "/my/payment"])
async def test_blocked_paths_stay_pending(monkeypatch, flag_on, path):
    steps = [READ_STEPS[0], {"action": "navigate", "url": f"https://{DOMAIN}{path}"}]
    row, traces = await build_row(monkeypatch, steps=steps)
    verdict = auto_approve.evaluate(row, traces)
    assert not verdict.eligible and "path_blocked" in verdict.reasons


async def test_path_token_match_is_exact_not_substring(monkeypatch, flag_on):
    steps = [READ_STEPS[0], {"action": "navigate", "url": f"https://{DOMAIN}/payload/cardinal"}]
    row, traces = await build_row(monkeypatch, steps=steps)
    assert auto_approve.evaluate(row, traces).eligible


async def test_tampered_spec_with_declared_read_but_click_is_rejected(monkeypatch, flag_on):
    row, traces = await build_row(monkeypatch)
    row["spec"]["steps"].append({"seq": 3, "action": "click", "selector": "#ok", "risk": "READ"})
    row["dry_run"]["max_risk"] = "READ"
    verdict = auto_approve.evaluate(row, traces)
    assert not verdict.eligible and "action_not_read_only:click" in verdict.reasons


async def test_non_pending_or_missing_signature_is_rejected(monkeypatch, flag_on):
    row, traces = await build_row(monkeypatch)
    assert "not_pending" in auto_approve.evaluate({**row, "status": "approved"}, traces).reasons
    row["spec"]["metadata"]["screen_e2e"].pop("signature")
    assert "signature_missing" in auto_approve.evaluate(row, traces).reasons


# ------------------------------------------------------------------ maybe_auto_approve


@pytest.fixture
def approval_env(monkeypatch):
    calls = {"decide": [], "notify": []}

    async def decide(registration_id, **kwargs):
        calls["decide"].append((registration_id, kwargs))
        return {"id": registration_id, "status": "approved", "recipe": {"id": "recipe-1"}}

    async def notify(name, domain):
        calls["notify"].append((name, domain))

    monkeypatch.setattr(auto_approve.registration, "decide_registration", decide)
    monkeypatch.setattr(auto_approve, "_notify", notify)
    return calls


async def wire(monkeypatch, row, traces):
    async def get_registration(registration_id, *, tenant_id):
        return row

    async def load_traces(tenant, domain, signature):
        return traces

    monkeypatch.setattr(auto_approve.registration, "get_registration", get_registration)
    monkeypatch.setattr(auto_approve, "_load_traces", load_traces)


async def test_eligible_draft_is_approved_by_system_with_audit_and_alert(monkeypatch, flag_on, approval_env):
    row, traces = await build_row(monkeypatch)
    await wire(monkeypatch, row, traces)
    result = await auto_approve.maybe_auto_approve(REG_ID, tenant_id=TENANT)
    assert result == {"status": "auto_approved", "recipe_id": "recipe-1"}
    assert len(approval_env["decide"]) == 1
    _, kwargs = approval_env["decide"][0]
    assert kwargs["decision"] == "approve" and kwargs["decided_by"] == "system:auto_read_policy"
    audit = json.loads(kwargs["reason"])
    assert audit["session_ids"] == ["s-a", "s-b"] and len(audit["evidence_refs"]) == 2
    assert approval_env["notify"] == [(row["name"], DOMAIN)]


async def test_flag_off_never_approves(monkeypatch, approval_env):
    monkeypatch.delenv(auto_approve.FLAG_ENV, raising=False)
    row, traces = await build_row(monkeypatch)
    await wire(monkeypatch, row, traces)
    assert await auto_approve.maybe_auto_approve(REG_ID, tenant_id=TENANT) == {"status": "disabled"}
    assert approval_env["decide"] == [] and approval_env["notify"] == []
    monkeypatch.setenv(auto_approve.FLAG_ENV, "0")
    assert not auto_approve.is_enabled()


async def test_ineligible_draft_stays_pending_without_alert(monkeypatch, flag_on, approval_env):
    row, traces = await build_row(monkeypatch, sessions=("s-a", "s-a"))
    await wire(monkeypatch, row, traces)
    result = await auto_approve.maybe_auto_approve(REG_ID, tenant_id=TENANT)
    assert result["status"] == "pending" and "sessions_below_minimum" in result["reasons"]
    assert approval_env["decide"] == [] and approval_env["notify"] == []


async def test_human_decided_first_or_db_error_keeps_pending(monkeypatch, flag_on, approval_env, caplog):
    row, traces = await build_row(monkeypatch)
    await wire(monkeypatch, row, traces)

    async def already(*args, **kwargs):
        raise registration.RegistrationError("registration_already_decided:approved")

    monkeypatch.setattr(auto_approve.registration, "decide_registration", already)
    assert (await auto_approve.maybe_auto_approve(REG_ID, tenant_id=TENANT))["status"] == "pending"

    async def boom(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(auto_approve.registration, "decide_registration", boom)
    caplog.set_level(logging.WARNING)
    assert (await auto_approve.maybe_auto_approve(REG_ID, tenant_id=TENANT)) == {
        "status": "pending", "reasons": ["policy_error"],
    }
    assert approval_env["notify"] == []


# ------------------------------------------------------------------ record_success 연결


@pytest.fixture
def record_env(monkeypatch):
    monkeypatch.delenv(auto_record.FLAG_ENV, raising=False)
    monkeypatch.delenv(auto_record.MIN_STEPS_ENV, raising=False)
    monkeypatch.delenv(auto_record.MIN_SESSIONS_ENV, raising=False)
    auto_record.get_book().clear()
    db = {"traces": {}, "drafts": {}, "sql_args": []}
    monkeypatch.setattr(auto_record, "get_pool", lambda: FakePool(db))
    seen = []

    async def list_recipes(**kwargs):
        return []

    async def request_registration(recipe, **kwargs):
        return {"id": REG_ID, "status": "pending"}

    async def maybe(registration_id, *, tenant_id):
        seen.append(registration_id)
        return {"status": "auto_approved"}

    monkeypatch.setattr(auto_record.store, "list_recipes", list_recipes)
    monkeypatch.setattr(recorder_module.registration, "request_registration", request_registration)
    monkeypatch.setattr(auto_approve, "maybe_auto_approve", maybe)
    yield seen
    auto_record.get_book().clear()


async def test_record_success_calls_policy_only_for_new_draft_when_flag_on(monkeypatch, record_env):
    monkeypatch.setenv(auto_approve.FLAG_ENV, "1")
    assert (await run_flow("session-a", evidence=SHOT_OK))["status"] == "recorded"
    assert record_env == []
    second = await run_flow("session-b", evidence=SHOT_OK)
    assert second["status"] == "drafted" and second["auto_approve"] == {"status": "auto_approved"}
    assert record_env == [REG_ID]
    third = await run_flow("session-c", evidence=SHOT_OK)
    assert third["status"] == "already_drafted" and record_env == [REG_ID]


async def test_record_success_flag_off_leaves_draft_pending(monkeypatch, record_env):
    monkeypatch.delenv(auto_approve.FLAG_ENV, raising=False)
    await run_flow("session-a")
    result = await run_flow("session-b")
    assert result["status"] == "drafted" and "auto_approve" not in result
    assert record_env == []


async def test_navigate_only_flow_from_observe_is_eligible_end_to_end(monkeypatch, flag_on, approval_env):
    """실제 observe 경로(navigate 2회 + 스냅샷)로 만든 초안이 정책을 통과한다."""
    db = {"traces": {}, "drafts": {}, "sql_args": []}
    registered = {}

    async def request_registration(recipe, **kwargs):
        registered["recipe"] = recipe
        return {"id": REG_ID, "status": "pending"}

    async def list_recipes(**kwargs):
        return []

    auto_record.get_book().clear()
    monkeypatch.setattr(auto_record, "get_pool", lambda: FakePool(db))
    monkeypatch.setattr(auto_record.store, "list_recipes", list_recipes)
    monkeypatch.setattr(recorder_module.registration, "request_registration", request_registration)
    key = {"browser_work_key": "wk"}
    plan = [
        ("browser_navigate", {"url": f"https://{DOMAIN}/orders", **key}, NAV_OK),
        ("browser_navigate", {"url": f"https://{DOMAIN}/orders/history", **key}, NAV_OK),
        ("browser_snapshot", dict(key), SNAP_OK),
    ]
    for session in ("session-a", "session-b"):
        await run_flow(session, steps=plan)
    recipe = registered["recipe"]
    row = {
        "id": REG_ID, "status": "pending", "name": recipe.name, "domain": DOMAIN,
        "spec": recipe.to_dict(),
        "dry_run": registration.build_dry_run(recipe, proposed_version=1),
    }
    traces = [
        {"id": i, "chat_session_id": s, "steps": json.dumps(READ_STEPS)}
        for i, s in enumerate(("session-a", "session-b"), start=1)
    ]
    verdict = auto_approve.evaluate(row, traces)
    assert row["dry_run"]["max_risk"] == "READ"
    assert verdict.eligible, verdict.reasons
    auto_record.get_book().clear()


# ------------------------------------------------------------------ revoke 스크립트


class RevokeConn:
    def __init__(self, rows):
        self.rows = rows
        self.updated = []

    def transaction(self):
        conn = self

        class _Tx:
            async def __aenter__(self_inner):
                return conn

            async def __aexit__(self_inner, *exc):
                return False

        return _Tx()

    async def fetch(self, sql, decider, tenant):
        assert decider == "system:auto_read_policy" and "FOR UPDATE" in sql
        return [r for r in self.rows if r["created_by"] == decider and r["enabled"]]

    async def execute(self, sql, ids):
        assert "enabled = FALSE" in sql and "DELETE" not in sql.upper()
        self.updated = ids


def _load_revoke():
    import importlib.util

    path = Path(__file__).resolve().parents[2] / "scripts" / "smart_browser_revoke_auto_read.py"
    spec = importlib.util.spec_from_file_location("smart_browser_revoke_auto_read", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _recipes():
    mk = lambda n, by, on: {  # noqa: E731
        "id": uuid.uuid4(), "tenant_id": TENANT, "name": n, "domain": DOMAIN, "version": 1,
        "created_at": None, "created_by": by, "enabled": on,
    }
    return [mk("auto_a", "system:auto_read_policy", True), mk("auto_b", "system:auto_read_policy", False),
            mk("human_c", "ceo", True)]


async def test_revoke_script_dry_run_changes_nothing():
    module = _load_revoke()
    conn = RevokeConn(_recipes())
    rows = await module.revoke(conn, apply=False)
    assert [r["name"] for r in rows] == ["auto_a"]
    assert conn.updated == []
    assert "dry-run" in module.render(rows, apply=False)


async def test_revoke_script_apply_targets_only_auto_approved_enabled_rows():
    module = _load_revoke()
    recipes = _recipes()
    conn = RevokeConn(recipes)
    rows = await module.revoke(conn, apply=True)
    assert conn.updated == [str(recipes[0]["id"])]
    assert "비활성화함" in module.render(rows, apply=True)
    assert module.AUTO_DECIDER == auto_approve.AUTO_DECIDER
