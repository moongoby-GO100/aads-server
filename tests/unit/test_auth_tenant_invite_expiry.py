"""create_tenant_invite 만료 인자 회귀 테스트 — DB 연결은 fake.

2026-10-15 운영 500: `($6::text || ' hours')::interval` 에 int 를 넘겨
asyncpg DataError(expected str, got int). 인자 타입과 SQL 형태가 맞물려야 한다.
"""
from __future__ import annotations

import asyncio

import pytest

from app import auth


class _FakeConn:
    def __init__(self):
        self.insert_sql = None
        self.insert_args = None

    async def fetchval(self, sql, *args):
        return "external"

    async def fetchrow(self, sql, *args):
        self.insert_sql = sql
        self.insert_args = args
        return {"invite_id": "i1", "tenant_id": args[0], "email": args[1], "role": args[3]}


class _Acquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class _FakePool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _Acquire(self._conn)


def _run(monkeypatch, expires_in_hours):
    conn = _FakeConn()

    async def _ready():
        return None

    async def _pool():
        return _FakePool(conn)

    monkeypatch.setattr(auth, "require_saas_schema_ready", _ready)
    monkeypatch.setattr(auth, "_ensure_pool", _pool)
    asyncio.run(
        auth.create_tenant_invite(
            tenant_id="00000000-0000-0000-0000-000000000001",
            email="User@Example.com",
            role="member",
            invited_by="u1",
            expires_in_hours=expires_in_hours,
        )
    )
    return conn


def test_expiry_param_matches_sql_cast(monkeypatch):
    conn = _run(monkeypatch, 168)
    sql = conn.insert_sql
    expires = conn.insert_args[5]
    if "$6::text" in sql:
        assert isinstance(expires, str), "$6::text 는 asyncpg 가 str 만 받는다"
    else:
        assert "make_interval(hours => $6::int)" in sql
        assert isinstance(expires, int)
    assert int(expires) == 168


@pytest.mark.parametrize("given,expected", [(0, 1), (None, 1), (-5, 1), (24, 24), (99999, 720)])
def test_expiry_range_clamped(monkeypatch, given, expected):
    conn = _run(monkeypatch, given)
    assert int(conn.insert_args[5]) == expected


def test_no_int_into_text_concat_interval_pattern():
    import inspect

    assert "::text || ' hours'" not in inspect.getsource(auth)
