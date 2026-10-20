from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.api import aag as aag_api
from app.services import aag_governance, aag_operations
from app.services.aag_operations import (
    SCHEMA_VERSION,
    AAGOperationsError,
    build_coverage,
    build_debt,
    build_freshness,
    build_last_run,
    build_operations_view,
)

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
REF = {"repository_id": "kis-autotrade-v4", "target_ref": "refs/heads/main"}


def _pointer(*, verified_min=5, generated_min=5, stats=None, **extra):
    return {
        "snapshot_id": uuid4(), "resolved_commit_sha": "a" * 40,
        "verified_at": NOW - timedelta(minutes=verified_min),
        "generated_at": NOW - timedelta(minutes=generated_min),
        "stats": stats if stats is not None else {"coverage": {
            "inventory_scanned": 10, "inventory_total": 10,
            "risk_scanned": 4, "risk_total": 4}},
        **extra,
    }


def _run(result="succeeded", **extra):
    return {"result": result, "started_at": NOW - timedelta(minutes=6),
            "finished_at": NOW - timedelta(minutes=5), **extra}


def _view(**kw):
    kw.setdefault("pointer", _pointer())
    kw.setdefault("severity_counts", {"P2": 2})
    kw.setdefault("last_run", _run())
    return build_operations_view(project="GO100", now=NOW, **REF, **kw)


# ---- contract shape -------------------------------------------------------
def test_contract_shape_matches_shared_schema():
    view = _view()
    assert view["schema_version"] == SCHEMA_VERSION == "aag-operations-v1"
    for key in ("project", "repository_id", "target_ref", "snapshot_id", "commit_sha",
                "axes", "recovery_actions", "limitations"):
        assert key in view
    axes = view["axes"]
    assert set(axes) == {"freshness", "last_run", "debt", "coverage"}
    assert {"status", "verified_at", "age_minutes"} <= set(axes["freshness"])
    assert {"status", "finished_at", "error", "reason"} <= set(axes["last_run"])
    assert {"status", "p0", "p1", "p2", "p3"} <= set(axes["debt"])
    assert {"status", "inventory_scanned", "inventory_total",
            "risk_scanned", "risk_total"} <= set(axes["coverage"])
    for action in view["recovery_actions"]:
        assert set(action) == {"kind", "label", "enabled", "reason"}
    assert view["commit_sha"] == "a" * 40


# ---- freshness: stale / republished -----------------------------------------
def test_fresh_snapshot():
    freshness, notes = build_freshness(_pointer(), NOW)
    assert freshness["status"] == "fresh" and freshness["age_minutes"] == 5
    assert notes == []


def test_stale_by_verified_at():
    assert build_freshness(_pointer(verified_min=400, generated_min=400), NOW)[0]["status"] == "stale"


def test_republished_stale_artifact_is_not_fresh():
    """verified_at is NOW() on ingest even for an old artifact; generated_at exposes it."""
    freshness, notes = build_freshness(_pointer(verified_min=2, generated_min=900), NOW)
    assert freshness["status"] == "stale"
    assert freshness["age_minutes"] == 2 and freshness["scan_age_minutes"] == 900
    assert any("재적재" in note for note in notes)


def test_missing_pointer_or_timestamp_is_unknown_not_fresh():
    assert build_freshness(None, NOW)[0]["status"] == "unknown"
    pointer = _pointer()
    pointer["verified_at"] = None
    assert build_freshness(pointer, NOW)[0]["status"] == "unknown"


# ---- last_run: failure vs skip vs unknown --------------------------------
@pytest.mark.parametrize("result,status", [
    ("succeeded", "success"), ("no_change_success", "success"),
    ("scan_failed", "failed"), ("ingest_failed", "failed"),
    ("skipped_locked", "skipped"), ("cancelled", "cancelled"),
    ("out_of_order", "rejected"), ("source_behind", "rejected"),
])
def test_last_run_status_mapping(result, status):
    assert build_last_run(_run(result), NOW)["status"] == status


def test_skip_is_never_success_and_carries_reason():
    run = build_last_run(_run("skipped_locked"), NOW)
    assert run["status"] == "skipped" and run["status"] != "success"
    assert "성공도 신선도 증거도 아니다" in run["reason"]


def test_failed_run_exposes_error_text_bounded():
    run = build_last_run(_run("ingest_failed", error_code="ingest_failed",
                              error_detail="x" * 5000), NOW)
    assert run["error"] == "ingest_failed" and len(run["reason"]) == 300


def test_missing_run_is_unknown():
    run = build_last_run(None, NOW)
    assert run["status"] == "unknown" and run["finished_at"] is None


def test_stuck_running_run_is_flagged():
    row = {"result": "running", "started_at": NOW - timedelta(minutes=120)}
    assert build_last_run(row, NOW)["status"] == "running_stale"
    row["started_at"] = NOW - timedelta(minutes=3)
    assert build_last_run(row, NOW)["status"] == "running"


def test_unrecognised_result_is_unknown():
    assert build_last_run(_run("weird"), NOW)["status"] == "unknown"


# ---- debt ---------------------------------------------------------------------
def test_debt_levels():
    assert build_debt({"P0": 1})["status"] == "blocked"
    assert build_debt({"P1": 2})["status"] == "attention"
    assert build_debt({"P3": 1})["status"] == "watch"
    assert build_debt({})["status"] == "none_detected"
    assert build_debt({"p0": 1})["p0"] == 1


def test_debt_unknown_when_not_measured_and_unclassified_counted():
    debt = build_debt(None)
    assert debt["status"] == "unknown" and debt["p0"] is None
    odd = build_debt({"P2": 1, "UNCLASSIFIED": 3})
    assert odd["unclassified"] == 3 and odd["status"] == "watch"


# ---- coverage: 0/0 is not 100% ---------------------------------------------
def test_zero_over_zero_is_unknown_not_complete():
    coverage, notes = build_coverage({"coverage": {
        "inventory_scanned": 0, "inventory_total": 0, "risk_scanned": 0, "risk_total": 0}})
    assert coverage["status"] == "unknown"
    assert coverage["inventory_total"] is None and coverage["risk_total"] is None
    assert notes


def test_coverage_absent_is_unknown_and_partial_is_partial():
    assert build_coverage({})[0]["status"] == "unknown"
    assert build_coverage(None)[0]["status"] == "unknown"
    partial = build_coverage({"coverage": {
        "inventory_scanned": 5, "inventory_total": 10, "risk_scanned": 4, "risk_total": 4}})[0]
    assert partial["status"] == "partial"
    assert build_coverage({"coverage": {
        "inventory_scanned": 10, "inventory_total": 10,
        "risk_scanned": 4, "risk_total": 4}})[0]["status"] == "complete"


def test_coverage_inconsistent_counts_and_unobserved_scopes():
    bad = build_coverage({"coverage": {"inventory_scanned": 11, "inventory_total": 10,
                                       "risk_scanned": 1, "risk_total": 1}})[0]
    assert bad["inventory_scanned"] is None and bad["status"] == "partial"
    scoped, notes = build_coverage({"coverage": {
        "inventory_scanned": 2, "inventory_total": 2, "risk_scanned": 1, "risk_total": 1,
        "unobserved_scopes": ["scripts/"]}})
    assert scoped["status"] == "partial" and any("scripts/" in n for n in notes)


# ---- recovery actions: approval marker only --------------------------------
def test_rescan_is_never_enabled_and_cites_evidence():
    view = _view(pointer=_pointer(verified_min=500, generated_min=500))
    rescan = next(a for a in view["recovery_actions"] if a["kind"] == "request_rescan")
    assert rescan["enabled"] is False
    assert "승인 필요" in rescan["reason"] and "stale" in rescan["reason"]
    assert "08:00~15:40" in rescan["reason"]


def test_rescan_without_evidence_says_so_and_other_project_has_no_market_note():
    view = build_operations_view(project="AADS", now=NOW, pointer=_pointer(),
                                 severity_counts={}, last_run=_run(), **REF)
    rescan = view["recovery_actions"][0]
    assert rescan["enabled"] is False and "근거 없음" in rescan["reason"]
    assert "08:00" not in rescan["reason"]


def test_actions_follow_axes():
    view = _view(severity_counts={"P0": 1, "P1": 2}, last_run=_run("ingest_failed"))
    actions = {a["kind"]: a for a in view["recovery_actions"]}
    assert actions["inspect_last_failure"]["enabled"] is True
    assert actions["review_debt"]["enabled"] is True and "P0 1건" in actions["review_debt"]["reason"]
    assert actions["expand_coverage"]["enabled"] is False
    assert not any("execute" in a["kind"] for a in view["recovery_actions"])


def test_no_pointer_everything_unknown_with_limitation():
    view = _view(pointer=None, severity_counts=None, last_run=None)
    axes = view["axes"]
    assert view["snapshot_id"] is None and view["commit_sha"] is None
    assert axes["freshness"]["status"] == "unknown"
    assert axes["debt"]["status"] == "unknown"
    assert axes["coverage"]["status"] == "unknown"
    assert axes["last_run"]["status"] == "unknown"
    assert any("스냅샷이 없어" in item for item in view["limitations"])


def test_limitations_always_disclose_host_state_and_exceptions():
    text = " ".join(_view()["limitations"])
    assert "호스트 상태 파일" in text and "aag_finding_exceptions" in text


def test_missing_run_columns_are_reported():
    view = _view(run_columns_missing=("error_detail",))
    assert any("error_detail" in item for item in view["limitations"])


# ---- async loader against a fake pool -----------------------------------------
ALL_COLUMNS = {
    "aag_latest_pointers": aag_operations._POINTER_COLUMNS["aag_latest_pointers"],
    "aag_graph_snapshots_v2": aag_operations._POINTER_COLUMNS["aag_graph_snapshots_v2"],
    "aag_snapshot_observations": aag_operations._POINTER_COLUMNS["aag_snapshot_observations"],
    "aag_scan_runs": set(aag_operations._RUN_REQUIRED) | set(aag_operations._RUN_OPTIONAL),
}


class FakePool:
    def __init__(self, *, pointer=None, run=None, columns=None, fail=False):
        self.pointer, self.run, self.fail = pointer, run, fail
        self.columns = ALL_COLUMNS if columns is None else columns
        self.statements: list[str] = []

    def acquire(self):
        pool = self

        class Conn:
            async def fetch(self, sql, *args):
                if pool.fail:
                    raise RuntimeError("db down")
                return [{"table_name": t, "column_name": c}
                        for t, cols in pool.columns.items() for c in cols]

            async def fetchrow(self, sql, *args):
                pool.statements.append(sql)
                return pool.pointer if "aag_latest_pointers" in sql else pool.run

        class Ctx:
            async def __aenter__(self):
                return Conn()

            async def __aexit__(self, *a):
                return False

        return Ctx()


@pytest.fixture(autouse=True)
def _reset_cache():
    aag_operations.reset_schema_cache()
    yield
    aag_operations.reset_schema_cache()


def _load(pool, monkeypatch, **kw):
    monkeypatch.setattr(aag_operations, "get_pool", lambda: pool)
    return aag_operations.load_operations_view(
        project="go100", now=NOW, **{**REF, **kw})


@pytest.mark.asyncio
async def test_loader_builds_view_from_rows(monkeypatch):
    pool = FakePool(pointer={**_pointer(), "severity_counts": {"P1": 1}}, run=_run())
    view = await _load(pool, monkeypatch)
    assert view["project"] == "GO100"
    assert view["axes"]["debt"]["p1"] == 1
    pointer_sql = next(s for s in pool.statements if "aag_latest_pointers" in s)
    for guard in ("publish_status='ready'", "o.authoritative=TRUE",
                  "o.verification_status='verified'", "o.resolved_commit_sha=p.resolved_commit_sha"):
        assert guard in pointer_sql


@pytest.mark.asyncio
async def test_loader_decodes_asyncpg_jsonb_counts_and_coverage(monkeypatch):
    pointer = _pointer()
    pool = FakePool(pointer={
        **pointer,
        "stats": json.dumps(pointer["stats"]),
        "severity_counts": json.dumps({"P1": 5, "P2": 71}),
    }, run=_run())
    view = await _load(pool, monkeypatch)
    assert view["axes"]["debt"]["p1"] == 5
    assert view["axes"]["debt"]["p2"] == 71
    assert view["axes"]["coverage"]["inventory_scanned"] == 10
    assert view["axes"]["coverage"]["risk_scanned"] == 4


@pytest.mark.asyncio
async def test_loader_not_found_is_typed_404(monkeypatch):
    with pytest.raises(AAGOperationsError) as err:
        await _load(FakePool(), monkeypatch)
    assert err.value.status_code == 404 and err.value.code == "snapshot_not_found"


@pytest.mark.asyncio
async def test_loader_run_only_reports_unknown_axes(monkeypatch):
    view = await _load(FakePool(run=_run("ingest_failed", error_detail="boom")), monkeypatch)
    assert view["axes"]["last_run"]["status"] == "failed"
    assert view["axes"]["freshness"]["status"] == "unknown"
    assert view["snapshot_id"] is None


@pytest.mark.asyncio
async def test_loader_db_error_is_typed_503_without_leaking(monkeypatch):
    with pytest.raises(AAGOperationsError) as err:
        await _load(FakePool(fail=True), monkeypatch)
    assert err.value.status_code == 503 and "db down" not in err.value.message


@pytest.mark.asyncio
async def test_loader_schema_gap_is_typed_503(monkeypatch):
    cols = {**ALL_COLUMNS, "aag_latest_pointers": {"snapshot_id"}}
    with pytest.raises(AAGOperationsError) as err:
        await _load(FakePool(columns=cols), monkeypatch)
    assert err.value.code == "schema_unavailable" and err.value.status_code == 503


@pytest.mark.asyncio
async def test_loader_tolerates_missing_optional_run_columns(monkeypatch):
    cols = {**ALL_COLUMNS, "aag_scan_runs": set(aag_operations._RUN_REQUIRED)}
    pool = FakePool(pointer={**_pointer(), "severity_counts": {}}, run=_run(), columns=cols)
    view = await _load(pool, monkeypatch)
    run_sql = next(s for s in pool.statements if "FROM aag_scan_runs" in s)
    assert "error_detail" not in run_sql
    assert any("error_detail" in item for item in view["limitations"])


@pytest.mark.asyncio
async def test_loader_rejects_blank_ref(monkeypatch):
    with pytest.raises(AAGOperationsError) as err:
        await _load(FakePool(), monkeypatch, target_ref=" ")
    assert err.value.status_code == 400


# ---- route: flag, RBAC, two-project isolation -------------------------------
class GrantPool:
    """aag_project_grants stand-in: {(project, principal)} -> allowed."""

    def __init__(self, grants):
        self.grants = grants

    def acquire(self):
        grants = self.grants

        class Conn:
            async def fetchval(self, sql, project, principal, roles):
                return (project, principal) in grants

        class Ctx:
            async def __aenter__(self):
                return Conn()

            async def __aexit__(self, *a):
                return False

        return Ctx()


async def _call(project, user, monkeypatch, *, loader=None):
    async def fake_loader(**kwargs):
        if loader:
            return await loader(**kwargs)
        return {"schema_version": SCHEMA_VERSION, "project": kwargs["project"]}

    monkeypatch.setattr(aag_api, "load_operations_view", fake_loader)
    return await aag_api.get_operations_v2(
        project=project, user=user, **REF)


@pytest.fixture
def v2_on(monkeypatch):
    monkeypatch.setenv("AAG_V2_ENABLED", "1")
    monkeypatch.setattr(aag_governance, "get_pool",
                        lambda: GrantPool({("GO100", "u-go"), ("KIS", "u-kis")}))


@pytest.mark.asyncio
async def test_route_disabled_flag_is_404(monkeypatch):
    monkeypatch.setenv("AAG_V2_ENABLED", "0")
    with pytest.raises(HTTPException) as err:
        await _call("GO100", {"user_id": "u-go"}, monkeypatch)
    assert err.value.status_code == 404


@pytest.mark.asyncio
async def test_route_authorised_project_succeeds(v2_on, monkeypatch):
    out = await _call("go100", {"user_id": "u-go"}, monkeypatch)
    assert out["project"] == "GO100"


@pytest.mark.asyncio
@pytest.mark.parametrize("project,user", [
    ("KIS", "u-go"), ("GO100", "u-kis"), ("GO100", "stranger"), ("GO100", ""),
])
async def test_route_cross_project_denied_with_typed_403(v2_on, monkeypatch, project, user):
    with pytest.raises(HTTPException) as err:
        await _call(project, {"user_id": user}, monkeypatch)
    assert err.value.status_code == 403
    assert err.value.detail["code"] == "insufficient_permission"


@pytest.mark.asyncio
async def test_route_denied_before_any_snapshot_read(v2_on, monkeypatch):
    called = []

    async def loader(**kwargs):
        called.append(kwargs)
        return {}

    with pytest.raises(HTTPException):
        await _call("KIS", {"user_id": "u-go"}, monkeypatch, loader=loader)
    assert called == []


@pytest.mark.asyncio
async def test_route_maps_typed_errors(v2_on, monkeypatch):
    async def loader(**kwargs):
        raise AAGOperationsError("snapshot_not_found", "none", 404)

    with pytest.raises(HTTPException) as err:
        await _call("GO100", {"user_id": "u-go"}, monkeypatch, loader=loader)
    assert err.value.status_code == 404
    assert err.value.detail == {"code": "snapshot_not_found", "message": "none"}


def test_no_execution_endpoint_added():
    paths = {(route.path, tuple(sorted(route.methods))) for route in aag_api.router.routes}
    assert ("/aag/v2/operations", ("GET",)) in paths
    assert not [p for p, _ in paths if "rescan" in p or "operations" in p and p != "/aag/v2/operations"]
