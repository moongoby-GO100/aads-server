from pathlib import Path

from app.services.goal_binding import (
    approval_execution_key,
    remediation_purpose,
    requires_mandatory_human,
)

ROOT = Path(__file__).resolve().parents[2]
SOURCE = (ROOT / "app/services/pipeline_runner_service.py").read_text(encoding="utf-8")


def test_blocked_remediation_requires_nonempty_explicit_purpose() -> None:
    assert remediation_purpose("REMEDIATION_PURPOSE: repair-evidence") == "repair-evidence"
    assert remediation_purpose("REMEDIATION_PURPOSE:") is None
    assert remediation_purpose("please repair the goal") is None


def test_mandatory_human_risks_fail_closed() -> None:
    assert requires_mandatory_human("P0-CRITICAL", "safe change")
    for instruction in ("production deploy", "DB schema migration", "security fix", "bulk delete"):
        assert requires_mandatory_human("P2", instruction)
    assert not requires_mandatory_human("P2", "focused unit-test correction")


def test_action_required_execution_key_is_stable_and_purpose_scoped() -> None:
    first = approval_execution_key("runner-1", "approval-remediation")
    assert first == approval_execution_key("runner-1", "approval-remediation")
    assert first != approval_execution_key("runner-1", "stale-approval")


def test_scope_binding_is_atomic_and_fail_closed() -> None:
    body = SOURCE.split("async def _link_job_to_goal_explicit", 1)[1].split(
        "# 활성 작업 저장", 1
    )[0]
    assert "conn.transaction()" in body
    assert "goal_link_rejected_cross_tenant" in body
    assert "goal_link_rejected_milestone_required" in body
    assert "REMEDIATION_PURPOSE" not in body  # parsed centrally, not fuzzy searched
    assert "UPDATE pipeline_jobs SET goal_id=" in body
    assert "INSERT INTO goal_task_links" in body


def test_structured_gate_has_no_free_text_pass_path() -> None:
    body = SOURCE.split("async def _structured_approval_gate", 1)[1].split(
        "async def _update_linked_goal_state_with_phase", 1
    )[0]
    for canonical_source in (
        "code_review_requests", "work_item_evidence", "commit_hash",
        "goal_approval_policy_versions", "goal_auto_approval_grants",
    ):
        assert canonical_source in body
    assert "audit_mode_no_execution" in body
    assert "mandatory_human_risk" in body
    assert "verified_evidence_incomplete" in body


def test_unbound_reconciliation_is_diagnostic_only() -> None:
    body = SOURCE.split("async def reconcile_unbound_awaiting_approval", 1)[1].split(
        "async def _auto_link_job_to_goal", 1
    )[0]
    assert '"dry_run": True' in body
    assert '"rebound": 0' in body
    assert "UPDATE pipeline_jobs" not in body
    assert "INSERT INTO goal_task_links" not in body


def test_rejection_propagates_as_failure_not_completion() -> None:
    body = SOURCE.split("async def _reject_inner", 1)[1].split(
        "# ─── Claude Code 프로세스 종료", 1
    )[0]
    assert 'self.status = "rejected_done"' in body
    assert 'self.status = "done"' not in body
