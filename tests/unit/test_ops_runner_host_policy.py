"""GET/PUT /ops/runner-host-policy (AADS-DASH-RUNNER-HOST-POLICY-UI-20261010)."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.api.ops import RunnerHostPolicyUpdate, get_runner_host_policy, put_runner_host_policy

NOW = datetime(2026, 10, 10, 1, 0, tzinfo=timezone.utc)


def _pool(conn):
    pool = MagicMock()

    @asynccontextmanager
    async def acquire():
        yield conn

    @asynccontextmanager
    async def transaction():
        yield

    pool.acquire = acquire
    conn.transaction = transaction
    return pool


def _put(conn, **kw):
    body = RunnerHostPolicyUpdate(**{"host": "contabo14", **kw})
    with patch("app.core.db_pool.get_pool", return_value=_pool(conn)):
        return asyncio.run(put_runner_host_policy(body, {"email": "ceo@example.com"}))


def _policy_row(rev=2):
    return {"max_concurrent": 6, "heavy_slots": 2, "urgent_reserved_slots": 1,
            "low_priority_nice": None, "revision": rev, "updated_by": "ceo@example.com",
            "updated_at": NOW}


def test_get_joins_hosts_counts_and_policy():
    conn = MagicMock()
    host_row = {"host": "contabo14", "projects": "GO100,KIS", "engine_mode": "claude",
                "env_max_concurrent": 4, "seen_ago": 30, **_policy_row()}
    no_policy = {"host": "cafe24_114", "projects": "SF", "engine_mode": "", "env_max_concurrent": None,
                 "seen_ago": 9999, "max_concurrent": None, "heavy_slots": None,
                 "urgent_reserved_slots": None, "low_priority_nice": None, "revision": None,
                 "updated_by": None, "updated_at": None}
    conn.fetch = AsyncMock(side_effect=[
        [host_row, no_policy],
        [{"runner_host": "contabo14", "cnt": 3}],
        [{"project": "GO100", "cnt": 2}, {"project": "KIS", "cnt": 1}, {"project": "SF", "cnt": 5}],
    ])
    with patch("app.core.db_pool.get_pool", return_value=_pool(conn)):
        out = asyncio.run(get_runner_host_policy())
    a, b = out["hosts"]
    assert (a["host"], a["running"], a["queued"], a["alive"]) == ("contabo14", 3, 3, True)
    assert a["policy"]["revision"] == 2 and a["policy"]["updated_by"] == "ceo@example.com"
    assert b["policy"] is None and b["alive"] is False and b["queued"] == 5
    assert out["limits"]["heavy_slots"] == {"min": 0, "max": 64}


@pytest.mark.parametrize("kw", [
    {"max_concurrent": 0}, {"max_concurrent": 201}, {"heavy_slots": 65},
    {"urgent_reserved_slots": -1}, {"expected_revision": 0},
])
def test_put_rejects_out_of_range(kw):
    with pytest.raises(ValidationError):
        RunnerHostPolicyUpdate(host="contabo14", **kw)


def test_put_rejects_urgent_above_heavy():
    conn = MagicMock()
    with pytest.raises(HTTPException) as e:
        _put(conn, heavy_slots=1, urgent_reserved_slots=2, expected_revision=2)
    assert e.value.status_code == 422


def test_put_update_with_matching_revision():
    conn = MagicMock()
    conn.fetchval = AsyncMock(return_value=True)
    conn.fetchrow = AsyncMock(return_value=_policy_row(rev=3))
    out = _put(conn, max_concurrent=6, heavy_slots=2, urgent_reserved_slots=1, expected_revision=2)
    assert out["ok"] and out["policy"]["revision"] == 3
    sql, *args = conn.fetchrow.call_args.args
    assert "UPDATE runner_host_policy" in sql and "revision = $6" in sql
    assert args[-1] == 2 and args[-2] == "ceo@example.com"


def test_put_stale_revision_is_409():
    conn = MagicMock()
    conn.fetchval = AsyncMock(side_effect=[True, 5])
    conn.fetchrow = AsyncMock(return_value=None)
    with pytest.raises(HTTPException) as e:
        _put(conn, heavy_slots=2, urgent_reserved_slots=1, expected_revision=2)
    assert e.value.status_code == 409 and e.value.detail["current_revision"] == 5


def test_put_first_insert_conflicts_when_row_appeared():
    conn = MagicMock()
    conn.fetchval = AsyncMock(side_effect=[True, 1])
    conn.fetchrow = AsyncMock(return_value=None)
    with pytest.raises(HTTPException) as e:
        _put(conn, heavy_slots=2, urgent_reserved_slots=1, expected_revision=None)
    assert e.value.status_code == 409
    assert "INSERT INTO runner_host_policy" in conn.fetchrow.call_args.args[0]


def test_put_unknown_host_is_404():
    conn = MagicMock()
    conn.fetchval = AsyncMock(return_value=False)
    with pytest.raises(HTTPException) as e:
        _put(conn, host="typo-host", heavy_slots=0, urgent_reserved_slots=0)
    assert e.value.status_code == 404
