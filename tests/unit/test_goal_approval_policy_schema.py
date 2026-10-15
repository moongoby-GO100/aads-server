from __future__ import annotations

import asyncio
import json

import pytest
from fastapi import HTTPException

from app.routers import goals
from app.services import goal_approval_policy_schema as schema

TENANT = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
GOAL_ID = "11111111-1111-4111-8111-111111111111"

ACCT = {"db_ddl": "lead", "deploy": "lead", "db_destructive": "ceo", "runner_budget_usd": 5}
MIXED = {
    "auto_approve_high": True, "auto_approve_critical": False,
    "max_executions": 200, "used": 12, "set_by": "CEO", "set_at": "2026-10-01T00:00:00+00:00",
    "ceo_reset_key": "r2", "approval_scope": "goal", "before_ceo_autoapproval_r2": {"used": 3},
}
STANDARD = {
    "auto_approve_high": True, "auto_approve_critical": False,
    "max_executions": 200, "used": 10, "set_by": "CEO", "set_at": "2026-10-01T00:00:00+00:00",
}
EXHAUSTED = {**STANDARD, "used": 200}


def test_acct_legacy_role_keys_have_no_effect():
    assert schema.classify_policy(ACCT) == "legacy_role_keys"
    assert schema.validate_policy(ACCT) == (True, [])
    summary = schema.effective_summary(ACCT)
    assert summary["high_effective"] is False
    assert summary["critical_effective"] is False
    assert summary["remaining"] == 0
    assert summary["ignored_keys"] == ["db_ddl", "db_destructive", "deploy", "runner_budget_usd"]
    assert schema.needs_attention(ACCT)


def test_df479771_mixed_policy_reports_unknown_keys():
    assert schema.classify_policy(MIXED) == "mixed"
    assert schema.validate_policy(MIXED)[0] is True
    summary = schema.effective_summary(MIXED)
    assert summary["high_effective"] is True
    assert summary["remaining"] == 188
    assert summary["ignored_keys"] == [
        "approval_scope", "before_ceo_autoapproval_r2", "ceo_reset_key",
    ]
    assert schema.needs_attention(MIXED)


def test_standard_policy_is_clean():
    assert schema.classify_policy(STANDARD) == "standard"
    assert schema.validate_policy(STANDARD) == (True, [])
    assert schema.effective_summary(STANDARD)["ignored_keys"] == []
    assert not schema.needs_attention(STANDARD)


def test_exhausted_policy_200_of_200():
    summary = schema.effective_summary(EXHAUSTED)
    assert summary["exhausted"] is True
    assert summary["remaining"] == 0
    assert summary["high_effective"] is False
    assert schema.classify_policy(EXHAUSTED) == "standard"
    assert schema.needs_attention(EXHAUSTED)


def test_empty_policy_is_not_flagged_by_default():
    assert schema.classify_policy({}) == "empty"
    assert schema.classify_policy(None) == "empty"
    assert not schema.needs_attention({})
    assert schema.needs_attention({}, include_empty=True)


@pytest.mark.parametrize("bad,code", [
    ({"used": 5, "max_executions": 3}, "used_exceeds_max_executions"),
    ({"used": -1}, "used_negative"),
    ({"max_executions": -2}, "max_executions_negative"),
    ({"max_executions": "10"}, "max_executions_type"),
    ({"auto_approve_high": "yes"}, "auto_approve_high_type"),
    ({"used": True}, "used_type"),
    ({"set_by": 1}, "set_by_type"),
])
def test_validate_policy_rejects_bad_values(bad, code):
    ok, errors = schema.validate_policy(bad)
    assert ok is False
    assert any(e.startswith(code + ":") for e in errors), errors


def test_effective_summary_mirrors_postgres_text_casts():
    summary = schema.effective_summary({"auto_approve_high": "true", "max_executions": "5", "used": "x"})
    assert summary["high_effective"] is True
    assert summary["max_executions"] == 5
    assert summary["used"] == 0


class _DB:
    def __init__(self, stored):
        self.stored = stored
        self.writes = []

    async def fetchrow(self, query, *args):
        if query.lstrip().upper().startswith("UPDATE"):
            self.writes.append(args)
            return {"id": GOAL_ID, "title": "goal"}
        return {"p": self.stored, "title": "goal", "status": "active"}

    async def fetch(self, query, *args):
        return [
            {"id": "g-acct", "title": "acct", "project": "ACCT", "status": "active", "p": ACCT},
            {"id": "g-mixed", "title": "mixed", "project": "AADS", "status": "active", "p": json.dumps(MIXED)},
            {"id": "g-std", "title": "std", "project": "AADS", "status": "active", "p": STANDARD},
            {"id": "g-done", "title": "done", "project": "AADS", "status": "active", "p": EXHAUSTED},
            {"id": "g-empty", "title": "empty", "project": "AADS", "status": "draft", "p": {}},
        ]


def _ctx():
    return {"tenant": {"id": TENANT}, "user": {"user_id": "ceo", "is_internal_admin": False}}


def _install(monkeypatch, stored):
    from app.core import db_pool
    from app.services import ohvis_alert

    db = _DB(stored)
    monkeypatch.setattr(db_pool, "get_pool", lambda: db)

    async def notify(*_a, **_k):
        return None

    monkeypatch.setattr(ohvis_alert, "notify", notify)
    return db


def test_set_policy_rejects_used_over_max_with_400(monkeypatch):
    db = _install(monkeypatch, {**STANDARD, "used": 150, "auto_approve_high": False})
    req = goals.GoalApprovalPolicyRequest(auto_approve_high=False, max_executions=100)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(goals.set_goal_approval_policy(GOAL_ID, req, _ctx()))
    assert exc.value.status_code == 400
    assert exc.value.detail["code"] == "invalid_approval_policy"
    assert exc.value.detail["errors"][0].startswith("used_exceeds_max_executions:")
    assert db.writes == []


def test_set_policy_enabling_resets_used_so_it_is_not_rejected(monkeypatch):
    db = _install(monkeypatch, {**STANDARD, "used": 150})
    req = goals.GoalApprovalPolicyRequest(auto_approve_high=True, max_executions=100)
    assert asyncio.run(goals.set_goal_approval_policy(GOAL_ID, req, _ctx()))["ok"] is True
    assert len(db.writes) == 1


def test_set_policy_rejects_corrupt_stored_used_with_400(monkeypatch):
    db = _install(monkeypatch, {**STANDARD, "used": -5})
    req = goals.GoalApprovalPolicyRequest(auto_approve_high=False, max_executions=100)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(goals.set_goal_approval_policy(GOAL_ID, req, _ctx()))
    assert exc.value.status_code == 400
    assert exc.value.detail["code"] == "invalid_approval_policy"
    assert db.writes == []


def test_set_policy_rejects_stored_used_type_error_when_not_enabling(monkeypatch):
    db = _install(monkeypatch, {"used": "abc"})
    req = goals.GoalApprovalPolicyRequest(auto_approve_high=False, max_executions=10)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(goals.set_goal_approval_policy(GOAL_ID, req, _ctx()))
    assert exc.value.status_code == 400
    assert db.writes == []


def test_set_policy_warns_but_does_not_block_legacy_keys(monkeypatch):
    db = _install(monkeypatch, dict(ACCT))
    req = goals.GoalApprovalPolicyRequest(auto_approve_high=True, max_executions=50)
    result = asyncio.run(goals.set_goal_approval_policy(GOAL_ID, req, _ctx()))
    assert result["ok"] is True
    assert result["warnings"]["ignored_keys"] == [
        "db_ddl", "db_destructive", "deploy", "runner_budget_usd",
    ]
    assert len(db.writes) == 1


def test_set_policy_clean_policy_has_no_warnings(monkeypatch):
    _install(monkeypatch, dict(STANDARD))
    req = goals.GoalApprovalPolicyRequest(auto_approve_high=True, max_executions=50)
    assert "warnings" not in asyncio.run(goals.set_goal_approval_policy(GOAL_ID, req, _ctx()))


def test_get_policy_keeps_old_fields_and_adds_new(monkeypatch):
    _install(monkeypatch, dict(MIXED))
    result = asyncio.run(goals.get_goal_approval_policy(GOAL_ID, _ctx()))
    assert result["auto_approve_high"] is True
    assert result["max_executions"] == 200
    assert result["used"] == 12
    assert result["classification"] == "mixed"
    assert result["effective_summary"]["remaining"] == 188
    assert result["ignored_keys"] == [
        "approval_scope", "before_ceo_autoapproval_r2", "ceo_reset_key",
    ]


def test_lint_lists_only_nonstandard_or_exhausted(monkeypatch):
    _install(monkeypatch, {})
    result = asyncio.run(goals.lint_goal_approval_policies(False, False, _ctx()))
    by_id = {item["goal_id"]: item for item in result["items"]}
    assert set(by_id) == {"g-acct", "g-mixed", "g-done"}
    assert by_id["g-acct"]["classification"] == "legacy_role_keys"
    assert by_id["g-done"]["exhausted"] is True
    assert result["count"] == 3

    with_empty = asyncio.run(goals.lint_goal_approval_policies(True, False, _ctx()))
    assert "g-empty" in {item["goal_id"] for item in with_empty["items"]}


def test_lint_route_is_not_shadowed_by_goal_id_routes():
    paths = [r.path for r in goals.router.routes]
    assert "/goals/approval-policy/lint" in paths
