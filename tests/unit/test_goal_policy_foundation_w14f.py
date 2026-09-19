from __future__ import annotations

import asyncio
from copy import deepcopy
from decimal import Decimal
from uuid import uuid4

import pytest

from app.services.goal_policy_foundation import (
    EvaluationRequest,
    PolicyContractError,
    SigningKey,
    canonical_hash,
    canonicalize,
    enforce_budget_overrun,
    evaluate_and_persist,
    parse_patch_json,
    patch_hash,
    reserve_single_grant,
    verify_immediately_before_execution,
)

KEY = SigningKey("decision-test", 1, b"w14f-test-key-material-must-be-32-bytes")
ZERO_HASH = "sha256:" + "0" * 64


class FakeConnection:
    def __init__(self, *, state=None, fail_write=False, rows=None):
        self.state = state
        self.fail_write = fail_write
        self.rows = list(rows or [])
        self.statements = []

    async def execute(self, sql, *args):
        self.statements.append((sql, args))
        if self.fail_write:
            raise RuntimeError("ledger unavailable")
        return "INSERT 0 1"

    async def fetchrow(self, sql, *args):
        self.statements.append((sql, args))
        if "goal_policy_execution_fences" in sql:
            return self.state
        return self.rows.pop(0) if self.rows else None


def request(**changes):
    values = {
        "tenant_id": str(uuid4()), "project": "AADS", "workspace_kind": "project",
        "principal_session_id": str(uuid4()), "assignment_id": str(uuid4()),
        "target_type": "task", "target_id": str(uuid4()), "action": "update",
        "base_version": 1, "patch": [{"op": "add", "path": "/title", "value": "safe"}],
        "environment": "dev", "risk_factors": [], "policy_version": str(uuid4()),
        "precondition_snapshot_hash": ZERO_HASH, "target_version": 1,
        "risk_tier": "A0", "approval_route": "NONE",
        "automation_eligibility": "BASELINE_AUTO",
    }
    values.update(changes)
    return EvaluationRequest(**values)


def evaluate(conn, value, **kwargs):
    return asyncio.run(evaluate_and_persist(
        conn, value, signing_key=KEY, original_engine_result="Allow", **kwargs
    ))


def expected_input(value):
    return {
        "tenant_id": value.tenant_id, "project": value.project,
        "workspace_kind": value.workspace_kind,
        "principal_session_id": value.principal_session_id,
        "assignment_id": value.assignment_id, "target_type": value.target_type,
        "target_id": value.target_id, "action": value.action,
        "base_version": value.base_version, "patch_hash": patch_hash(value.patch),
        "environment": value.environment, "risk_factors": list(value.risk_factors),
        "precondition_snapshot_hash": value.precondition_snapshot_hash,
        "policy_version": value.policy_version, "grant_id": value.grant_id,
        "grant_version": value.grant_version,
    }


def execution_state(envelope):
    return {
        "target_version": 1, "policy_version": envelope["policy_version"],
        "precondition_snapshot_hash": envelope["precondition_snapshot_hash"],
        "kill_switch_epoch": envelope["kill_switch_epoch"],
        "deny_policy_epoch": envelope["deny_policy_epoch"],
        "assignment_epoch": envelope["assignment_epoch"],
        "grant_revocation_epoch": envelope["grant_revocation_epoch"],
        "ancestor_revocation_epoch": envelope["ancestor_revocation_epoch"],
        "kill_switch_active": False, "policy_active": True, "assignment_active": True,
        "grant_chain_active": True,
    }


def test_t36_cedar_diagnostic_discards_allow_auto():
    envelope = evaluate(FakeConnection(), request(), error_policy_ids=["forbid-1"], error_kinds=["evaluation"])
    assert envelope["result"] == "APPROVAL_REQUIRED"
    assert envelope["automation_eligibility"] == "MANDATORY_HUMAN"
    assert "policy_evaluation_error" in envelope["reason_codes"]


def test_t38_ledger_failure_never_returns_auto():
    with pytest.raises(RuntimeError, match="ledger unavailable"):
        evaluate(FakeConnection(fail_write=True), request())


@pytest.mark.parametrize(
    ("case", "field", "reason"),
    [
        ("T45", "ancestor_revocation_epoch", "stale_ancestor_revocation_epoch"),
        ("T46", "kill_switch_epoch", "stale_kill_switch_epoch"),
        ("T54", "ancestor_revocation_epoch", "stale_ancestor_revocation_epoch"),
        ("T55", "deny_policy_epoch", "stale_deny_policy_epoch"),
    ],
)
def test_executor_rejects_changed_epochs(case, field, reason):
    value = request()
    envelope = evaluate(FakeConnection(), value)
    state = execution_state(envelope)
    state[field] += 1
    assert asyncio.run(verify_immediately_before_execution(
        FakeConnection(state=state), envelope, signing_key=KEY,
        expected_input=expected_input(value),
    )) == (False, reason)


def test_t48_single_grant_reservation_uses_blocking_conditional_update():
    grant = {"id": str(uuid4()), "grant_version": 1, "remaining_uses": 0}
    reservation = {"id": str(uuid4()), "grant_id": grant["id"], "grant_version": 1,
                   "reservation_state": "reserved"}
    conn = FakeConnection(rows=[None, grant, reservation])
    result = asyncio.run(reserve_single_grant(
        conn, tenant_id=str(uuid4()), project="AADS", execution_key="exec-1",
        decision_id=str(uuid4()), grant_id=grant["id"], grant_version=1,
    ))
    sql = " ".join(statement for statement, _ in conn.statements)
    assert result == reservation
    assert "SKIP LOCKED" not in sql.upper()
    assert "used_executions=used_executions+1" in sql


def test_t49_object_key_order_has_same_patch_hash():
    first = parse_patch_json('[{"op":"add","path":"/x","value":{"a":1,"b":2}}]')
    second = parse_patch_json('[{"value":{"b":2,"a":1},"path":"/x","op":"add"}]')
    assert patch_hash(first) == patch_hash(second)


def test_t50_patch_operation_order_changes_hash():
    first = [{"op": "add", "path": "/a", "value": 1}, {"op": "remove", "path": "/b"}]
    assert patch_hash(first) != patch_hash(list(reversed(first)))


@pytest.mark.parametrize("raw", [
    '[{"op":"add","op":"remove","path":"/x","value":1}]',
    '[{"op":"add","path":"/x","value":NaN}]',
    '[{"op":"add","path":"/x","value":Infinity}]',
    '[{"op":"add","path":"/x","value":01}]',
])
def test_t51_rejects_duplicate_and_malformed_json_numbers(raw):
    with pytest.raises(PolicyContractError) as exc:
        parse_patch_json(raw)
    assert (exc.value.status_code, exc.value.code) == (422, "invalid_patch")


def test_unsupported_canonicalization_version_is_rejected():
    with pytest.raises(PolicyContractError) as exc:
        canonicalize({"safe": True}, version="RFC8785-draft")
    assert exc.value.code == "unsupported_canonicalization_version"


def test_rfc8785_binary64_rounding_and_exponent_thresholds():
    assert canonicalize(Decimal("333333333.33333329")) == b"333333333.3333333"
    assert canonicalize(Decimal("0.000001")) == b"0.000001"
    assert canonicalize(Decimal("0.0000001")) == b"1e-7"
    assert canonicalize(Decimal(100000000000000000000)) == b"100000000000000000000"


def test_rfc8785_rejects_lone_unicode_surrogates():
    with pytest.raises(PolicyContractError) as exc:
        canonicalize("\ud800")
    assert exc.value.code == "invalid_patch"


def test_t52_environment_change_invalidates_input_hash():
    value = request()
    envelope = evaluate(FakeConnection(), value)
    changed = expected_input(value)
    changed["environment"] = "staging"
    assert asyncio.run(verify_immediately_before_execution(
        FakeConnection(state=execution_state(envelope)), envelope, signing_key=KEY,
        expected_input=changed,
    )) == (False, "decision_input_mismatch")


def test_executor_rejects_current_target_version_and_live_safety_state():
    value = request()
    envelope = evaluate(FakeConnection(), value)
    expected = expected_input(value)

    changed_version = execution_state(envelope)
    changed_version["target_version"] += 1
    assert asyncio.run(verify_immediately_before_execution(
        FakeConnection(state=changed_version), envelope, signing_key=KEY,
        expected_input=expected,
    )) == (False, "stale_target_version")

    kill_switch = execution_state(envelope)
    kill_switch["kill_switch_active"] = True
    assert asyncio.run(verify_immediately_before_execution(
        FakeConnection(state=kill_switch), envelope, signing_key=KEY,
        expected_input=expected,
    )) == (False, "kill_switch_active")

    inactive_policy = execution_state(envelope)
    inactive_policy["policy_active"] = False
    assert asyncio.run(verify_immediately_before_execution(
        FakeConnection(state=inactive_policy), envelope, signing_key=KEY,
        expected_input=expected,
    )) == (False, "stale_policy")

    inactive_assignment = execution_state(envelope)
    inactive_assignment["assignment_active"] = False
    assert asyncio.run(verify_immediately_before_execution(
        FakeConnection(state=inactive_assignment), envelope, signing_key=KEY,
        expected_input=expected,
    )) == (False, "stale_assignment")


def test_mutable_epoch_tampering_breaks_the_envelope_signature():
    value = request()
    envelope = evaluate(FakeConnection(), value)
    tampered = deepcopy(envelope)
    tampered["kill_switch_epoch"] += 1
    assert asyncio.run(verify_immediately_before_execution(
        FakeConnection(state=execution_state(tampered)), tampered,
        signing_key=KEY, expected_input=expected_input(value),
    )) == (False, "invalid_decision_signature")


def test_t53_same_idempotency_key_with_different_grant_is_conflict():
    existing = {"id": str(uuid4()), "grant_id": str(uuid4()), "grant_version": 1,
                "reservation_state": "reserved"}
    with pytest.raises(PolicyContractError) as exc:
        asyncio.run(reserve_single_grant(
            FakeConnection(rows=[existing]), tenant_id=str(uuid4()), project="AADS",
            execution_key="same", decision_id=str(uuid4()), grant_id=str(uuid4()), grant_version=1,
        ))
    assert (exc.value.status_code, exc.value.code) == (409, "idempotency_key_reused")


def test_t56_decision_hash_uses_server_precondition_hash():
    value = request(precondition_snapshot_hash="sha256:" + "6" * 64)
    envelope = evaluate(FakeConnection(), value)
    assert envelope["decision_input_hash"] == canonical_hash(expected_input(value))


def test_t57_cross_project_boundary_is_deny_not_approval():
    envelope = evaluate(FakeConnection(), request(
        boundary_decision="DENY", automation_eligibility="NOT_EXECUTABLE",
        approval_route="NONE", risk_factors=["cross_project"],
    ), reason_codes=["project_scope_denied"])
    assert envelope["result"] == "DENY"
    assert envelope["approval_route"] == "NONE"


def test_t58_measured_budget_overrun_stales_grant():
    result = enforce_budget_overrun(
        {"status": "active", "max_files": 2, "max_rows": 10, "max_cost_usd": "1.25"},
        {"files": 2, "rows": 11, "cost_usd": "1.25"},
    )
    assert result == {"overrun": True, "overrun_dimensions": ["rows"],
                      "grant_status": "stale", "allow_new_execution": False}


def test_a3_is_always_mandatory_human_and_signed_envelope_is_tamper_evident():
    value = request(risk_tier="A3", grant_id=str(uuid4()), grant_version=1)
    envelope = evaluate(FakeConnection(), value)
    assert envelope["result"] == "APPROVAL_REQUIRED"
    assert envelope["automation_eligibility"] == "MANDATORY_HUMAN"
    tampered = deepcopy(envelope)
    tampered["effective_application_result"] = "AUTO"
    assert asyncio.run(verify_immediately_before_execution(
        FakeConnection(state=execution_state(envelope)), tampered, signing_key=KEY,
        expected_input=expected_input(value),
    )) == (False, "invalid_decision_signature")


def test_secret_patch_value_is_not_persisted_and_only_pointer_is_recorded():
    conn = FakeConnection()
    envelope = evaluate(conn, request(
        patch=[{"op": "replace", "path": "/credentials/access_token", "value": "do-not-store"}],
    ))
    assert envelope["erased"] == ["/credentials/access_token"]
    assert "do-not-store" not in str(conn.statements)
