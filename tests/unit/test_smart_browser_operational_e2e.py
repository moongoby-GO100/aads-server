"""The operational M11 E2E must fail closed on anything it did not observe."""
from __future__ import annotations

import json
from pathlib import Path

from app.services.golden_promotion_gate import evaluate_promotion_gate
from scripts import smart_browser_operational_e2e as e2e

PASSING_REGRESSION = {"status": "passed", "selected": list(e2e.REQUIRED_REGRESSIONS)}


def _focused(observed: dict[str, bool | None]) -> dict:
    return e2e.build_focused_results(
        observed=observed, evidence="/tmp/aria-snapshots.json",
        fixture_sha256="a" * 64, regression=PASSING_REGRESSION,
    )


def _decision(observed: dict[str, bool | None]):
    focused = _focused(observed)
    candidate, active = e2e.build_metrics(
        observed=observed, latency_ms=10, llm_calls=0, llm_cost_usd=0.0,
    )
    return focused, evaluate_promotion_gate(
        focused_results=focused, affected_regressions=PASSING_REGRESSION,
        candidate_metrics=candidate, active_metrics=active,
    )


def test_missing_regression_file_fails_closed(tmp_path: Path) -> None:
    result = e2e.load_regression(tmp_path / "absent.json")

    assert result["status"] == "failed"
    assert result["infrastructure_error"] is True


def test_regression_requires_full_g1_to_g6_coverage(tmp_path: Path) -> None:
    path = tmp_path / "regression.json"
    path.write_text(json.dumps({"status": "passed", "exit_code": 0, "selected": ["g1", "g2"]}),
                    encoding="utf-8")

    assert e2e.load_regression(path)["status"] == "failed"


def test_regression_passes_only_when_the_run_exited_zero(tmp_path: Path) -> None:
    path = tmp_path / "regression.json"
    payload = {"status": "passed", "exit_code": 0, "selected": list(e2e.REQUIRED_REGRESSIONS),
               "log_sha256": "b" * 64}
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert e2e.load_regression(path)["status"] == "passed"

    payload["exit_code"] = 1
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert e2e.load_regression(path)["status"] == "failed"


def test_unobserved_check_fails_its_suite_and_the_gate() -> None:
    observed = dict.fromkeys(e2e.OBSERVATIONS, True)
    observed["stored_payload_clean"] = None

    focused, decision = _decision(observed)

    assert focused["security"]["status"] == "failed"
    assert focused["security"]["critical_complete"] is False
    assert decision.passed is False


def test_failed_observation_fails_its_golden_case() -> None:
    observed = dict.fromkeys(e2e.OBSERVATIONS, True)
    observed["structural_reuse"] = False

    focused, decision = _decision(observed)

    assert focused["golden_cases"]["success"]["status"] == "failed"
    assert decision.passed is False


def test_missing_tenant_isolation_blocks_authorization() -> None:
    observed = dict.fromkeys(e2e.OBSERVATIONS, True)
    observed["tenant_isolation_blocked"] = False

    focused, decision = _decision(observed)

    assert focused["security"]["authorization_passed"] is False
    assert decision.passed is False


def test_failed_regression_blocks_promotion() -> None:
    observed = dict.fromkeys(e2e.OBSERVATIONS, True)
    focused = e2e.build_focused_results(
        observed=observed, evidence="/tmp/aria-snapshots.json", fixture_sha256="a" * 64,
        regression={"status": "failed", "selected": list(e2e.REQUIRED_REGRESSIONS)},
    )
    candidate, active = e2e.build_metrics(
        observed=observed, latency_ms=10, llm_calls=0, llm_cost_usd=0.0,
    )

    decision = evaluate_promotion_gate(
        focused_results=focused, affected_regressions={"status": "failed", "selected": []},
        candidate_metrics=candidate, active_metrics=active,
    )

    assert focused["regression"]["status"] == "failed"
    assert decision.passed is False


def test_fully_observed_run_passes_with_measured_metrics() -> None:
    observed = dict.fromkeys(e2e.OBSERVATIONS, True)

    focused, decision = _decision(observed)
    candidate, active = e2e.build_metrics(
        observed=observed, latency_ms=42, llm_calls=0, llm_cost_usd=0.0,
    )

    assert decision.passed is True
    assert focused["golden_cases"]["page_injection"]["status"] == "passed"
    assert candidate["success_rate"] == 1.0
    assert candidate["latency_ms"] == 42
    assert candidate["cost_usd"] == 0.0
    assert active["baseline"] == "no_prior_active_version_first_activation"
