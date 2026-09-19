from pathlib import Path

from app.services.golden_promotion_gate import (
    MANDATORY_GOLDEN_CASES,
    MANDATORY_SUITES,
    evaluate_promotion_gate,
)


def _passing():
    focused = {suite: {"status": "passed", "critical_complete": True} for suite in MANDATORY_SUITES}
    focused["security"]["authorization_passed"] = True
    focused["golden_cases"] = {
        case: {"status": "passed", "fixture_sha256": f"sha256:{case}", "evidence": [case]}
        for case in MANDATORY_GOLDEN_CASES
    }
    return focused


def test_all_suites_golden_cases_and_measured_baseline_pass():
    decision = evaluate_promotion_gate(
        focused_results=_passing(),
        affected_regressions={"selected": ["g1", "g2", "g3", "g4", "g5", "g6"], "status": "passed"},
        candidate_metrics={"success_rate": 1.0, "cost_usd": 0.1, "latency_ms": 90},
        active_metrics={"success_rate": 0.99, "cost_usd": 0.1, "latency_ms": 100},
    )
    assert decision.passed


def test_missing_infrastructure_and_unmeasured_baseline_fail_closed():
    focused = _passing()
    focused["security"] = {"status": "passed", "critical_complete": True, "missing_dependency": True}
    decision = evaluate_promotion_gate(
        focused_results=focused,
        affected_regressions={"selected": ["g1"], "status": "passed"},
        candidate_metrics={}, active_metrics={},
    )
    assert not decision.passed
    assert "suite_infrastructure_error:security" in decision.reasons
    assert "unmeasured_metric:success_rate" in decision.reasons


def test_authorization_and_all_g1_g6_regressions_are_mandatory():
    focused = _passing()
    focused["security"].pop("authorization_passed")
    decision = evaluate_promotion_gate(
        focused_results=focused,
        affected_regressions={"selected": ["g1", "g2"], "status": "passed"},
        candidate_metrics={"success_rate": 1, "cost_usd": 1, "latency_ms": 1},
        active_metrics={"success_rate": 1, "cost_usd": 1, "latency_ms": 1},
    )
    assert "authorization_critical_failed_or_missing" in decision.reasons
    assert "affected_regressions_missing:g3,g4,g5,g6" in decision.reasons


def test_each_measured_metric_must_not_regress():
    cases = (
        ({"success_rate": 0.98, "cost_usd": 1, "latency_ms": 10}, "success_rate_regression"),
        ({"success_rate": 1, "cost_usd": 2, "latency_ms": 10}, "cost_regression"),
        ({"success_rate": 1, "cost_usd": 1, "latency_ms": 11}, "latency_regression"),
    )
    for candidate, reason in cases:
        decision = evaluate_promotion_gate(
            focused_results=_passing(),
            affected_regressions={"selected": ["g1", "g2", "g3", "g4", "g5", "g6"], "status": "passed"},
            candidate_metrics=candidate,
            active_metrics={"success_rate": 1, "cost_usd": 1, "latency_ms": 10},
        )
        assert not decision.passed and reason in decision.reasons


def test_database_trigger_blocks_direct_active_insert_and_payload_mutation():
    root = Path(__file__).resolve().parents[2]
    sql = (root / "migrations/20260919_g6_golden_promotion_gate.sql").read_text()
    assert "BEFORE INSERT OR UPDATE ON browser_learned_artifact_versions" in sql
    assert "learned artifact versions must enter as candidate" in sql
    assert "learned artifact version payload is immutable" in sql
    assert "learned artifact lifecycle bypass" in sql
    assert "BEFORE INSERT OR UPDATE ON ops_skill_versions" in sql
    assert "skill versions must enter as candidate" in sql


def test_runtime_promotion_paths_are_tenant_scoped_idempotent_and_transactional():
    root = Path(__file__).resolve().parents[2]
    learned_api = (root / "app/api/learned_artifacts.py").read_text()
    skill_service = (root / "app/services/ohvis_harness.py").read_text()
    for source in (learned_api, skill_service):
        assert "async with get_pool().acquire() as conn, conn.transaction()" in source
        assert "tenant_id=$1::uuid AND idempotency_key=$2 FOR UPDATE" in source
    assert "a.tenant_id=$1::uuid" in learned_api
    assert "l.tenant_id=$1::uuid" in skill_service
