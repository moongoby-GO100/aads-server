from app.services.golden_promotion_gate import MANDATORY_GOLDEN_CASES, MANDATORY_SUITES, evaluate_promotion_gate


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
