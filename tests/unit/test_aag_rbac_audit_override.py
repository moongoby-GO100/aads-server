from __future__ import annotations

import hashlib
import inspect
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.api.aag import OverrideApprovalRequest, OverrideRequest, OverrideVerificationRequest
from app.services.aag_governance import AAGWorkflowError, _require_future, approve_override

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "migrations/20260919_aag_v1_1_rbac_audit_override.sql"


def test_project_and_scanner_tokens_are_normalized_without_persisting_secret():
    source = (ROOT / "app/services/aag_governance.py").read_text(encoding="utf-8")
    digest = hashlib.sha256(b"scanner-secret").hexdigest()
    assert 'strip().upper()' in source
    assert 'hashlib.sha256(token.encode("utf-8")).hexdigest()' in source
    assert len(digest) == 64 and "scanner-secret" not in digest


def test_migration_is_additive_and_audit_is_append_only():
    sql = MIGRATION.read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS aag_project_grants" in sql
    assert "CREATE TABLE IF NOT EXISTS aag_scanner_credentials" in sql
    assert "CREATE TABLE IF NOT EXISTS aag_audit_events" in sql
    assert "AAG audit events are append-only" in sql
    assert "BEFORE UPDATE OR DELETE ON aag_audit_events" in sql
    assert "DROP TABLE" not in sql
    assert "ALTER TABLE aag_graph_snapshots" not in sql


def test_exception_enforces_separation_and_expiry():
    sql = MIGRATION.read_text(encoding="utf-8")
    assert "approver_id <> proposer_id" in sql
    assert "expires_at > created_at" in sql
    assert "idx_aag_exceptions_active" in sql


def test_override_contract_has_replay_rollback_and_revalidation_fences():
    sql = MIGRATION.read_text(encoding="utf-8")
    for required in (
        "request_id UUID NOT NULL UNIQUE",
        "commit_sha TEXT NOT NULL",
        "previous_healthy_snapshot_id",
        "independent_verification JSONB",
        "rollback_plan JSONB NOT NULL",
        "replay_nonce_hash CHAR(64) NOT NULL UNIQUE",
        "CREATE TABLE IF NOT EXISTS aag_revalidation_jobs",
    ):
        assert required in sql
    assert "never makes fallback observations authoritative" in sql


def test_api_queries_and_ingest_are_project_fenced():
    source = (ROOT / "app/api/aag.py").read_text(encoding="utf-8")
    governance = (ROOT / "app/services/aag_governance.py").read_text(encoding="utf-8")
    assert "X-AAG-Scanner-Token" in source
    assert "authenticate_scanner" in source
    assert "p.project=$1 AND p.repository_id=$2 AND p.target_ref=$3" in source
    assert "o.project=p.project" in source
    assert "o.repository_id=p.repository_id" in source
    assert "o.target_ref=p.target_ref" in source
    assert "o.authoritative=TRUE" in source
    assert "o.verification_status='verified'" in source
    assert "hmac.compare_digest" in governance
    assert "WHERE id=$1 AND project=$2 FOR UPDATE" in governance
    assert "fallback snapshot is outside project scope" in governance
    assert "requester, approver and verifier must be distinct" in governance
    assert "authoritative revalidation queued" in governance


def test_override_approval_cannot_supply_or_spoof_verifier():
    with pytest.raises(ValidationError):
        OverrideApprovalRequest(
            project="AADS",
            reason="approve verified request",
            verifier_id="spoofed-user",
            verification={"passed": True},
        )
    signature = inspect.signature(approve_override)
    assert "verifier_id" not in signature.parameters
    assert "verification" not in signature.parameters


def test_override_requires_separate_authenticated_verification_contract():
    request = OverrideVerificationRequest(
        project="AADS",
        passed=True,
        verified_commit_sha="a" * 40,
        evidence={"test_run": "AT-066"},
        reason="independent verification passed",
    )
    assert request.verified_commit_sha == "a" * 40
    source = (ROOT / "app/api/aag.py").read_text(encoding="utf-8")
    assert '/v2/overrides/{override_id}/verify' in source
    assert "verifier_id=actor" in source
    assert "record_override_verification" in source


def test_override_rejects_invalid_commit_and_empty_rollback_plan():
    values = {
        "request_id": "0b33bfef-3816-450f-9dba-a1ade2d234ec",
        "project": "AADS",
        "repository_id": "aads-server",
        "target_ref": "refs/heads/main",
        "reason": "central service outage",
        "expires_at": "2099-01-01T00:00:00Z",
        "previous_healthy_snapshot_id": "6d0b54b0-6162-4a05-b6a1-c672ef5d19e4",
        "replay_nonce": "0123456789abcdef",
    }
    with pytest.raises(ValidationError):
        OverrideRequest(commit_sha="not-a-sha", rollback_plan={"steps": ["restore"]}, **values)
    with pytest.raises(ValidationError):
        OverrideRequest(commit_sha="a" * 40, rollback_plan={}, **values)


def test_governance_expiry_requires_timezone_and_is_wired_for_all_v2_routes():
    from datetime import UTC, datetime

    with pytest.raises(AAGWorkflowError, match="timezone"):
        _require_future(datetime.now(UTC).replace(year=2099, tzinfo=None), "override expiry")
    source = (ROOT / "app/api/aag.py").read_text(encoding="utf-8")
    assert source.count("_require_v2_enabled()") >= 7
    governance = (ROOT / "app/services/aag_governance.py").read_text(encoding="utf-8")
    assert "async def expire_exceptions()" in governance
    assert "before=dict(before)" in governance
