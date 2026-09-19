from __future__ import annotations

from app.routers.work_items import PolicyInputRequest
from app.services.goal_policy_preconditions import _canonical_hash


def test_t56_client_precondition_object_is_not_a_policy_input_field():
    request = PolicyInputRequest.model_validate({
        "action": "execute",
        "base_version": 8,
        "expected_parent_version": 7,
        "preconditions": {"evidence_complete": True, "blocker_count": 0},
    })

    dumped = request.model_dump()
    assert dumped["expected_parent_version"] == 7
    assert "preconditions" not in dumped


def test_t57_policy_input_does_not_offer_cross_project_override():
    fields = PolicyInputRequest.model_fields

    assert "target_project" not in fields
    assert "allow_cross_project" not in fields
    assert "explicit_approval_id" not in fields


def test_evidence_snapshot_hash_is_deterministic_for_object_key_order():
    first = {"version": 3, "evidence": [{"state": "verified", "artifact_hash": "sha256:a"}]}
    second = {"evidence": [{"artifact_hash": "sha256:a", "state": "verified"}], "version": 3}

    assert _canonical_hash(first) == _canonical_hash(second)


def test_evidence_snapshot_hash_changes_when_version_changes():
    assert _canonical_hash({"version": 3, "evidence": []}) != _canonical_hash(
        {"version": 4, "evidence": []}
    )
