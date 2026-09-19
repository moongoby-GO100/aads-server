from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.api import aag as aag_api
from app.services.aag_brief_v2 import build_brief_v2, coverage_from_snapshot

NOW = datetime(2026, 9, 19, 12, tzinfo=UTC)


def _snapshot(**overrides):
    value = {
        "snapshot_id": "snap-1", "observation_id": "obs-1", "run_id": "run-1",
        "project": "AADS", "repository_id": "aads-server", "target_ref": "main",
        "resolved_commit_sha": "a" * 40, "expected_target_ref_head_sha": "a" * 40,
        "source": "central_db", "authoritative": True, "verified_at": NOW - timedelta(minutes=5),
        "analyzer": {"name": "scan_aads", "version": "1.1", "ruleset_digest": "b" * 64,
                     "scan_scope_digest": "c" * 64},
        "stats": {"coverage": {"inventory_scanned": 10, "inventory_total": 10,
                                "risk_scanned": 8, "risk_total": 8, "unobserved_scopes": []}},
    }
    value.update(overrides)
    return value


@pytest.mark.parametrize("state", [
    "not_detected", "outside_scope", "partial", "stale", "fallback", "mismatch", "truncated",
])
def test_unsafe_states_never_make_negative_assertion(state):
    brief = build_brief_v2(snapshot=_snapshot(), target="app/api/aag.py", findings=[], status=state, now=NOW)
    assert brief["negative_assertion_allowed"] is False
    assert brief["conclusion"] == "not_proven"
    assert brief["required_actions"]


@pytest.mark.parametrize(("field", "value", "expected"), [
    ("source", "local_fallback", "fallback"),
    ("authoritative", False, "fallback"),
    ("resolved_commit_sha", "d" * 40, "mismatch"),
    ("expected_target_ref_head_sha", "e" * 40, "mismatch"),
    ("truncated", True, "truncated"),
    ("verified_at", None, "stale"),
    ("verified_at", NOW - timedelta(minutes=181), "stale"),
])
def test_source_safety_overrides_claimed_status(field, value, expected):
    brief = build_brief_v2(snapshot=_snapshot(**{field: value}), target="x", findings=[], status="fresh", now=NOW)
    assert brief["state"] == expected


@pytest.mark.parametrize(("scanned", "total", "ratio"), [
    (0, 0, 0.0), (0, 10, 0.0), (1, 4, 0.25), (4, 4, 1.0), (8, 4, 1.0),
    ("2", "5", 0.4), (-1, 5, 0.0), (3, -1, 0.0), (None, 5, 0.0), (3, None, 0.0),
])
def test_inventory_coverage_golden_set(scanned, total, ratio):
    snapshot = _snapshot(stats={"coverage": {"inventory_scanned": scanned, "inventory_total": total}})
    assert coverage_from_snapshot(snapshot).inventory_ratio == ratio


@pytest.mark.parametrize("unobserved", [
    ["payments"], ["admin", "payments"], ["payments", "payments"], [""], [], None,
])
def test_unobserved_scope_golden_set(unobserved):
    snapshot = _snapshot(stats={"coverage": {"inventory_scanned": 1, "inventory_total": 1,
                                              "risk_scanned": 1, "risk_total": 1,
                                              "unobserved_scopes": unobserved}})
    brief = build_brief_v2(snapshot=snapshot, target="x", findings=[], status="not_detected", now=NOW)
    assert brief["miss_review_required"] is True
    assert brief["negative_assertion_allowed"] is False


def test_pins_identity_analyzer_coverage_and_findings():
    finding = {"rule": "ROUTE_MISSING", "severity": "P0"}
    brief = build_brief_v2(snapshot=_snapshot(), target="app/api/aag.py", findings=[finding], status="detected", now=NOW)
    assert brief["snapshot"]["resolved_commit_sha"] == "a" * 40
    assert brief["analyzer"]["name"] == "scan_aads"
    assert brief["coverage"]["risk_ratio"] == 1.0
    assert brief["conclusion"] == "findings_present"
    assert brief["findings"] == [finding]


@pytest.mark.asyncio
async def test_api_brief_filters_target_and_preserves_pin(monkeypatch):
    async def fake_latest(**_kwargs):
        return {
            "snapshot": {"id": "snap", "stats": _snapshot()["stats"], "unresolved": [],
                         "findings": [{"file": "app/api/aag.py", "rule": "ROUTE_MISSING"}]},
            "observation_id": "obs", "run_id": "run",
            "ref": {"project": "AADS", "repository_id": "aads-server",
                    "target_ref": "main", "governance_scope": "default"},
            "commit": {"resolved": "a" * 40, "expected_ref_head": "a" * 40},
            "source": "central_db", "authoritative": True, "verified_at": NOW,
            "analyzer": _snapshot()["analyzer"],
        }

    monkeypatch.setattr(aag_api, "get_latest_snapshot_v2", fake_latest)
    brief = await aag_api.get_brief_v2(
        project="AADS", repository_id="aads-server", target_ref="main",
        target="app/api/aag.py", user={"user_id": "viewer"},
    )
    assert brief["state"] == "detected"
    assert brief["snapshot"]["snapshot_id"] == "snap"
    assert len(brief["findings"]) == 1


@pytest.mark.asyncio
async def test_api_brief_marks_unresolved_snapshot_partial(monkeypatch):
    async def fake_latest(**_kwargs):
        return {
            "snapshot": {"id": "snap", "stats": _snapshot()["stats"],
                         "unresolved": [{"kind": "dynamic"}], "findings": []},
            "observation_id": "obs", "run_id": "run", "ref": {
                "project": "AADS", "repository_id": "aads-server",
                "target_ref": "main", "governance_scope": "default"},
            "commit": {"resolved": "a" * 40, "expected_ref_head": "a" * 40},
            "source": "central_db", "authoritative": True, "verified_at": NOW,
            "analyzer": _snapshot()["analyzer"],
        }

    monkeypatch.setattr(aag_api, "get_latest_snapshot_v2", fake_latest)
    brief = await aag_api.get_brief_v2(
        project="AADS", repository_id="aads-server", target_ref="main",
        target="unknown", user={"user_id": "viewer"},
    )
    assert brief["state"] == "partial"
    assert brief["miss_review_required"] is True
