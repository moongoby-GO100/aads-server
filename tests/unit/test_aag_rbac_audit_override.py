from __future__ import annotations

import hashlib
from pathlib import Path

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
    assert "o.project=$1 AND o.repository_id=$2 AND o.target_ref=$3" in source
    assert "o.authoritative=TRUE" in source
    assert "hmac.compare_digest" in governance
    assert "WHERE id=$1 AND project=$2 FOR UPDATE" in governance
    assert "fallback snapshot is outside project scope" in governance
    assert "requester, approver and verifier must be distinct" in governance
    assert "authoritative revalidation queued" in governance
