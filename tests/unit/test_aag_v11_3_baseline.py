from __future__ import annotations

from pathlib import Path

import pytest

from app.services.aag_baseline import (
    evaluate_baseline,
    validate_rule_promotion,
)
from app.services.aag_governance import AAGWorkflowError
from tools.aag import scan_aads
from tools.aag.v2_contract import (
    STABLE_KEY_VERSION,
    content_fingerprint,
    finding_key_set_digest,
    normalize_findings,
    stable_finding_key,
)

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "migrations/20260919_aag_v1_1_stable_baseline_rule_lifecycle.sql"


def _finding(**overrides):
    finding = {
        "rule": "ROUTE_MISSING",
        "severity": "P0",
        "file": "web/src/api.ts",
        "lineno": 12,
        "method": "GET",
        "path": "/api/v1/items/{id}",
        "key": "web/src/api.ts:12",
        "detail": "human-readable evidence",
    }
    finding.update(overrides)
    return finding


def test_stable_finding_key_ignores_line_and_prose_but_not_semantic_target():
    original = _finding()
    moved = _finding(lineno=999, key="web/src/api.ts:999", detail="different prose")
    changed = _finding(path="/api/v1/orders/{id}")

    assert stable_finding_key("aads", original) == stable_finding_key("AADS", moved)
    assert stable_finding_key("AADS", original) != stable_finding_key("AADS", changed)


def test_normalized_finding_set_and_content_are_deterministic_twice():
    first = [_finding(), _finding(rule="TABLE_NO_MODEL", table="jobs", file="app/x.py")]
    second = list(reversed(first))

    normalized_first = normalize_findings("AADS", first)
    normalized_second = normalize_findings("AADS", second)
    assert normalized_first == normalized_second
    assert finding_key_set_digest(normalized_first) == finding_key_set_digest(normalized_second)
    graph_first = {"findings": first, "nodes": [], "edges": [], "stats": {}, "unresolved": []}
    graph_second = {"findings": second, "nodes": [], "edges": [], "stats": {}, "unresolved": []}
    assert content_fingerprint(graph_first, project="AADS") == content_fingerprint(
        graph_second, project="AADS"
    )


def test_supplied_wrong_stable_key_is_rejected():
    with pytest.raises(ValueError, match="canonical identity"):
        normalize_findings("AADS", [_finding(stable_finding_key="spoofed")])


def test_key_set_gate_blocks_only_enforced_unexcepted_additions():
    findings = normalize_findings("AADS", [
        _finding(rule="ROUTE_MISSING"),
        _finding(rule="NEW_RULE", file="app/new.py", key="app/new.py"),
        _finding(rule="TABLE_NO_MODEL", table="jobs", file="app/x.py"),
    ])
    by_rule = {item["rule"]: item["stable_finding_key"] for item in findings}
    result = evaluate_baseline(
        current_findings=findings,
        baseline_keys=[],
        rule_modes={"ROUTE_MISSING": "enforced", "TABLE_NO_MODEL": "enforced"},
        active_exception_keys=[by_rule["TABLE_NO_MODEL"]],
    )

    assert result.passed is False
    assert result.blocking_keys == (by_rule["ROUTE_MISSING"],)
    assert result.warning_keys == (by_rule["NEW_RULE"],)
    assert result.excepted_keys == (by_rule["TABLE_NO_MODEL"],)


def test_rule_promotion_requires_independent_approval_observation_and_fixture():
    digest = "a" * 64
    validate_rule_promotion(
        proposer_id="author", approver_id="reviewer", observation_count=2,
        fixture_digest=digest, fixture_passed=True,
    )
    with pytest.raises(AAGWorkflowError, match="own promotion"):
        validate_rule_promotion(
            proposer_id="author", approver_id="author", observation_count=2,
            fixture_digest=digest, fixture_passed=True,
        )
    with pytest.raises(AAGWorkflowError, match="two warn-only"):
        validate_rule_promotion(
            proposer_id="author", approver_id="reviewer", observation_count=1,
            fixture_digest=digest, fixture_passed=True,
        )
    with pytest.raises(AAGWorkflowError, match="golden fixture"):
        validate_rule_promotion(
            proposer_id="author", approver_id="reviewer", observation_count=2,
            fixture_digest=digest, fixture_passed=False,
        )


def test_migration_is_additive_repeatable_and_protects_approved_state():
    sql = MIGRATION.read_text(encoding="utf-8")
    for required in (
        "CREATE TABLE IF NOT EXISTS aag_baselines",
        "CREATE TABLE IF NOT EXISTS aag_baseline_findings",
        "CREATE TABLE IF NOT EXISTS aag_rule_lifecycle",
        "approved AAG baseline is immutable",
        "approved AAG baseline findings are immutable",
        "mode TEXT NOT NULL DEFAULT 'warn_only'",
        "observation_count >= 2",
        "fixture_digest IS NOT NULL",
    ):
        assert required in sql
    assert "DROP TABLE" not in sql
    assert "TRUNCATE" not in sql


def test_scanner_baseline_uses_key_set_and_keeps_legacy_count_fallback():
    current = {
        "stable_key_version": STABLE_KEY_VERSION,
        "stable_finding_keys": ["key-a", "key-b"],
        "findings_by_rule": {"ROUTE_MISSING": 2},
    }
    saved = {
        "stable_key_version": STABLE_KEY_VERSION,
        "stable_finding_keys": ["key-a"],
        "findings_by_rule": {"ROUTE_MISSING": 99},
    }
    assert scan_aads.compare_baseline(current, saved) == ["NEW_STABLE_FINDING: key-b"]
    assert scan_aads.compare_baseline(
        {"findings_by_rule": {"ROUTE_MISSING": 2}},
        {"findings_by_rule": {"ROUTE_MISSING": 1}},
    ) == ["ROUTE_MISSING: 1 → 2 (+1)"]
