"""Scope 4 — second-project onboarding through the real route + real loader.

Only the DB pool is faked (keyed by the bound parameters, so a query that forgot the
project filter would return the wrong project's rows and fail here). No remote scan,
no credential, no service change is involved.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.api import aag as aag_api
from app.services import aag_governance, aag_operations

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
GO = ("GO100", "kis-autotrade-v4", "refs/heads/main", "default")
AADS = ("AADS", "aads-server", "refs/heads/main", "default")
GO_SNAP, AADS_SNAP = uuid4(), uuid4()

ALL_COLUMNS = {
    "aag_latest_pointers": aag_operations._POINTER_COLUMNS["aag_latest_pointers"],
    "aag_graph_snapshots_v2": aag_operations._POINTER_COLUMNS["aag_graph_snapshots_v2"],
    "aag_snapshot_observations": aag_operations._POINTER_COLUMNS["aag_snapshot_observations"],
    "aag_scan_runs": set(aag_operations._RUN_REQUIRED) | set(aag_operations._RUN_OPTIONAL),
}


def _pointer(snap, sha, severity, minutes):
    return {
        "snapshot_id": snap, "resolved_commit_sha": sha,
        "verified_at": NOW - timedelta(minutes=minutes), "generated_at": NOW - timedelta(minutes=minutes),
        "stats": {"coverage": {"inventory_scanned": 8, "inventory_total": 8,
                               "risk_scanned": 2, "risk_total": 2}},
        "severity_counts": severity,
    }


POINTERS = {GO: _pointer(GO_SNAP, "a" * 40, {"P1": 3}, 10), AADS: _pointer(AADS_SNAP, "b" * 40, {"P2": 1}, 20)}
RUNS = {
    GO: {"id": "r-go", "result": "succeeded", "started_at": NOW - timedelta(minutes=11),
         "finished_at": NOW - timedelta(minutes=10)},
    AADS: {"id": "r-aads", "result": "skipped_locked", "started_at": NOW - timedelta(minutes=21),
           "finished_at": NOW - timedelta(minutes=21)},
}


class ScopedPool:
    def __init__(self, *, grants):
        self.grants = grants
        self.bound: list[tuple] = []

    def acquire(self):
        pool = self

        class Conn:
            async def fetch(self, sql, *args):
                return [{"table_name": t, "column_name": c} for t, cols in ALL_COLUMNS.items() for c in cols]

            async def fetchrow(self, sql, *key):
                pool.bound.append(tuple(key))
                table = POINTERS if "aag_latest_pointers" in sql else RUNS
                return table.get(tuple(key))

            async def fetchval(self, sql, project, principal, roles):
                return (project, principal) in pool.grants

        class Ctx:
            async def __aenter__(self):
                return Conn()

            async def __aexit__(self, *a):
                return False

        return Ctx()


@pytest.fixture
def pool(monkeypatch):
    monkeypatch.setenv("AAG_V2_ENABLED", "1")
    aag_operations.reset_schema_cache()
    scoped = ScopedPool(grants={("GO100", "u-go"), ("AADS", "u-aads")})
    monkeypatch.setattr(aag_governance, "get_pool", lambda: scoped)
    monkeypatch.setattr(aag_operations, "get_pool", lambda: scoped)
    yield scoped
    aag_operations.reset_schema_cache()


async def _get(project, repo, user, ref="refs/heads/main"):
    return await aag_api.get_operations_v2(
        project=project, repository_id=repo, target_ref=ref, user={"user_id": user})


@pytest.mark.asyncio
async def test_each_project_sees_only_its_own_snapshot(pool):
    go = await _get("GO100", GO[1], "u-go")
    aads = await _get("aads", AADS[1], "u-aads")
    assert (go["snapshot_id"], go["commit_sha"]) == (str(GO_SNAP), "a" * 40)
    assert (aads["snapshot_id"], aads["commit_sha"]) == (str(AADS_SNAP), "b" * 40)
    assert go["axes"]["debt"]["p1"] == 3 and aads["axes"]["debt"]["p1"] == 0
    assert aads["axes"]["debt"]["p2"] == 1


@pytest.mark.asyncio
async def test_second_project_skip_is_not_success_and_does_not_leak_go100(pool):
    aads = await _get("AADS", AADS[1], "u-aads")
    assert aads["axes"]["last_run"]["status"] == "skipped"
    assert aads["axes"]["last_run"]["status"] != "success"
    assert not any("08:00~15:40" in a["reason"] for a in aads["recovery_actions"])
    assert "kis-autotrade" not in repr(aads)


@pytest.mark.asyncio
async def test_operational_key_is_the_full_scope_tuple_bound_to_the_query(pool):
    await _get("AADS", AADS[1], "u-aads")
    assert pool.bound
    assert set(pool.bound) == {AADS}, "every statement must be bound to (project, repo, ref, scope)"


@pytest.mark.asyncio
@pytest.mark.parametrize(("project", "repo", "user"), [
    ("AADS", AADS[1], "u-go"),
    ("GO100", GO[1], "u-aads"),
    ("GO100", GO[1], "nobody"),
])
async def test_cross_permission_is_denied_before_any_query(pool, project, repo, user):
    with pytest.raises(HTTPException) as err:
        await _get(project, repo, user)
    assert err.value.status_code == 403 and err.value.detail["code"] == "insufficient_permission"
    assert pool.bound == []


@pytest.mark.asyncio
async def test_other_projects_repository_under_my_project_is_not_found(pool):
    """Authorized for GO100 but asking for AADS's repository must not return AADS data."""
    with pytest.raises(HTTPException) as err:
        await _get("GO100", AADS[1], "u-go")
    assert err.value.status_code == 404
    assert err.value.detail["code"] == "snapshot_not_found"
    assert str(AADS_SNAP) not in repr(err.value.detail)


@pytest.mark.asyncio
async def test_unmeasured_ref_of_second_project_is_not_found_not_clean(pool):
    with pytest.raises(HTTPException) as err:
        await _get("AADS", AADS[1], "u-aads", ref="refs/heads/release")
    assert err.value.status_code == 404
