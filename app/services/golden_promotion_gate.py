"""Fail-closed G6 candidate -> shadow -> active promotion policy."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

MANDATORY_SUITES = frozenset({"security", "functional", "aria", "freshness", "regression", "audit"})
CRITICAL_SUITES = frozenset({"security", "freshness"})
REQUIRED_AFFECTED_REGRESSIONS = frozenset({"g1", "g2", "g3", "g4", "g5", "g6"})
MANDATORY_GOLDEN_CASES = frozenset({
    "success", "empty_result", "login_expired", "selector_changed",
    "page_injection", "ttl_or_value_changed",
})


@dataclass(frozen=True)
class GateDecision:
    passed: bool
    reasons: tuple[str, ...]


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def evaluate_promotion_gate(
    *, focused_results: Mapping[str, Any], affected_regressions: Mapping[str, Any],
    candidate_metrics: Mapping[str, Any], active_metrics: Mapping[str, Any],
) -> GateDecision:
    """Evaluate fixed fixtures and affected regressions; infrastructure errors fail closed."""
    reasons: list[str] = []
    missing_suites = sorted(MANDATORY_SUITES - set(focused_results))
    if missing_suites:
        reasons.append("missing_suites:" + ",".join(missing_suites))
    for suite in sorted(MANDATORY_SUITES & set(focused_results)):
        result = focused_results[suite]
        if not isinstance(result, Mapping):
            reasons.append(f"invalid_suite_result:{suite}")
            continue
        if result.get("infrastructure_error") or result.get("missing_dependency"):
            reasons.append(f"suite_infrastructure_error:{suite}")
        if result.get("status") != "passed":
            reasons.append(f"suite_failed:{suite}")
        if suite in CRITICAL_SUITES and not result.get("critical_complete", False):
            reasons.append(f"critical_incomplete:{suite}")
        if suite == "security" and not result.get("authorization_passed", False):
            reasons.append("authorization_critical_failed_or_missing")
    cases = focused_results.get("golden_cases", {})
    if not isinstance(cases, Mapping):
        reasons.append("invalid_golden_cases")
    else:
        for case in sorted(MANDATORY_GOLDEN_CASES):
            result = cases.get(case)
            if not isinstance(result, Mapping) or result.get("status") != "passed":
                reasons.append(f"golden_case_failed_or_missing:{case}")
            elif not result.get("fixture_sha256") or not result.get("evidence"):
                reasons.append(f"golden_case_not_reproducible:{case}")
    selected = {str(value).lower() for value in affected_regressions.get("selected", [])}
    missing_regressions = sorted(REQUIRED_AFFECTED_REGRESSIONS - selected)
    if missing_regressions:
        reasons.append("affected_regressions_missing:" + ",".join(missing_regressions))
    if affected_regressions.get("status") != "passed":
        reasons.append("affected_regressions_failed")
    if affected_regressions.get("infrastructure_error"):
        reasons.append("affected_regressions_infrastructure_error")

    required_metrics = ("success_rate", "cost_usd", "latency_ms")
    for name in required_metrics:
        if not _number(candidate_metrics.get(name)) or not _number(active_metrics.get(name)):
            reasons.append(f"unmeasured_metric:{name}")
    if not reasons:
        if candidate_metrics["success_rate"] < active_metrics["success_rate"]:
            reasons.append("success_rate_regression")
        if candidate_metrics["cost_usd"] > active_metrics["cost_usd"]:
            reasons.append("cost_regression")
        if candidate_metrics["latency_ms"] > active_metrics["latency_ms"]:
            reasons.append("latency_regression")
    return GateDecision(not reasons, tuple(reasons))
