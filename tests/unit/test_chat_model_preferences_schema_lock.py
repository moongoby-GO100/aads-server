"""chat_model_preferences 스키마 보정이 락을 기다리지 않는다.

2026-10-02: 10:00 KST pg_dump 가 AccessShareLock 을 쥔 동안 ALTER TABLE 이 매 조회마다
대기하다 30초 TimeoutError 로 /chat-preferences 가 500 이 됐다.
"""
from __future__ import annotations

import asyncpg
import pytest

from app.api import llm_models


class _Txn:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Conn:
    def __init__(self, fail_on: str | None = None):
        self.statements: list[str] = []
        self.fail_on = fail_on

    def transaction(self):
        return _Txn()

    async def execute(self, sql, *args):
        self.statements.append(" ".join(sql.split()))
        if self.fail_on and self.fail_on in sql:
            raise asyncpg.exceptions.LockNotAvailableError("canceling statement due to lock timeout")


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        return False


class _Pool:
    def __init__(self, conn):
        self.conn = conn
        self.acquired = 0

    def acquire(self):
        self.acquired += 1
        return _Acquire(self.conn)


@pytest.fixture(autouse=True)
def _reset_flag(monkeypatch):
    monkeypatch.setattr(llm_models, "_chat_model_preferences_schema_ready", False)


@pytest.mark.asyncio
async def test_schema_sets_lock_timeout_before_ddl_and_runs_once(monkeypatch):
    conn = _Conn()
    pool = _Pool(conn)
    monkeypatch.setattr(llm_models, "get_pool", lambda: pool)

    await llm_models._ensure_chat_model_preferences_table()
    await llm_models._ensure_chat_model_preferences_table()

    assert conn.statements[0] == "SET LOCAL lock_timeout = '2s'"
    assert any(s.startswith("ALTER TABLE chat_model_preferences") for s in conn.statements)
    assert pool.acquired == 1


@pytest.mark.asyncio
async def test_lock_busy_is_skipped_and_retried_next_call(monkeypatch):
    conn = _Conn(fail_on="ALTER TABLE")
    pool = _Pool(conn)
    monkeypatch.setattr(llm_models, "get_pool", lambda: pool)

    await llm_models._ensure_chat_model_preferences_table()  # 예외 없이 반환

    assert llm_models._chat_model_preferences_schema_ready is False
    conn.fail_on = None
    await llm_models._ensure_chat_model_preferences_table()
    assert llm_models._chat_model_preferences_schema_ready is True
