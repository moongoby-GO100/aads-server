from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.api import ohvis_llmops
from app.services import llmops_store

ROOT = Path(__file__).resolve().parents[2]


class AsyncContext:
    def __init__(self, value=None):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *_args):
        return False


class FakeConn:
    def __init__(self):
        self.trace = None
        self.executed = []

    def transaction(self):
        return AsyncContext()

    async def execute(self, query, *args):
        self.executed.append((query, args))
        return "SELECT 1"

    async def fetchrow(self, query, *args):
        if "FROM llmops_traces" in query:
            return self.trace
        return None


class FakePool:
    def __init__(self, conn):
        self.conn = conn
        self.rows = []
        self.updates = []

    def acquire(self):
        return AsyncContext(self.conn)

    async def fetch(self, _query, *_args):
        return self.rows

    async def execute(self, query, *args):
        self.updates.append((query, args))
        return "UPDATE 1"


def payload(**overrides):
    base = {
        "schema_version": "1.0",
        "project": "GO100",
        "external_trace_id": "go100-trace-123",
        "input_summary": "authorization: Bearer abcdefghijklmnopqrstuvwxyz",
        "metadata": {"api_key": "secret-value", "safe": "ok"},
        "tool_calls": [{"tool_name": "market_lookup", "metadata": {"password": "bad"}}],
    }
    base.update(overrides)
    return ohvis_llmops.TraceIngestRequest(**base)


def test_contract_rejects_unknown_version_and_limits():
    with pytest.raises(ValidationError):
        payload(schema_version="2.0")
    with pytest.raises(ValidationError):
        payload(external_trace_id="x" * 201)
    with pytest.raises(ValidationError):
        payload(tool_calls=[{"tool_name": "x"}] * 51)
    with pytest.raises(ValidationError):
        payload(metadata={"blob": "x" * 70_000})


def test_redaction_is_recursive():
    cleaned = llmops_store.redact_ingest_value(
        {"api_key": "top-secret", "nested": {"authorization": "Bearer secret", "safe": "value"}}
    )
    assert cleaned["api_key"] == "[redacted]"
    assert cleaned["nested"]["authorization"] == "[redacted]"
    assert cleaned["nested"]["safe"] == "value"


def test_authentication_is_hashed_and_scoped(monkeypatch):
    conn = FakeConn()
    pool = FakePool(conn)
    token = "ohvis_ingest_test-token"
    pool.rows = [
        {"client_id": "go100-primary", "project": "GO100", "token_hash": llmops_store.ingest_token_digest(token)}
    ]
    import app.core.db_pool as db_pool
    monkeypatch.setattr(db_pool, "get_pool", lambda: pool)
    client = asyncio.run(llmops_store.authenticate_ingest_client(token))
    assert client == {"client_id": "go100-primary", "project": "GO100"}
    assert asyncio.run(llmops_store.authenticate_ingest_client("wrong")) is None


def test_missing_and_invalid_auth_are_401(monkeypatch):
    with pytest.raises(HTTPException) as missing:
        asyncio.run(ohvis_llmops.llmops_trace_ingest(payload(), authorization=None))
    assert missing.value.status_code == 401

    async def no_client(_token):
        return None

    monkeypatch.setattr(llmops_store, "authenticate_ingest_client", no_client)
    with pytest.raises(HTTPException) as invalid:
        asyncio.run(ohvis_llmops.llmops_trace_ingest(payload(), authorization="Bearer wrong"))
    assert invalid.value.status_code == 401


def test_scope_mismatch_is_403(monkeypatch):
    async def wrong_scope(_token):
        return {"client_id": "sf-client", "project": "SF"}

    monkeypatch.setattr(llmops_store, "authenticate_ingest_client", wrong_scope)
    with pytest.raises(HTTPException) as mismatch:
        asyncio.run(ohvis_llmops.llmops_trace_ingest(payload(), authorization="Bearer token"))
    assert mismatch.value.status_code == 403


def test_ingest_replay_returns_same_id_without_second_write(monkeypatch):
    conn = FakeConn()
    pool = FakePool(conn)
    import app.core.db_pool as db_pool
    monkeypatch.setattr(db_pool, "get_pool", lambda: pool)
    calls = []

    async def record_trace(**kwargs):
        calls.append(kwargs)
        conn.trace = {"id": "central-1", "created_at": "2026-09-09T00:00:00Z"}
        return "central-1"

    monkeypatch.setattr(llmops_store, "record_trace", record_trace)
    body = payload().model_dump(mode="json")
    first = asyncio.run(llmops_store.ingest_external_trace(body, client_id="go100-primary"))
    replay = asyncio.run(llmops_store.ingest_external_trace(body, client_id="go100-primary"))
    assert first["trace_id"] == replay["trace_id"] == "central-1"
    assert first["deduplicated"] is False
    assert replay["deduplicated"] is True
    assert len(calls) == 1
    assert calls[0]["external_trace_id"] == "go100-trace-123"
    assert calls[0]["trace_key"] == "external:GO100:go100-trace-123"
    assert calls[0]["metadata"]["api_key"] == "[redacted]"
    assert calls[0]["tool_calls"][0]["metadata"]["password"] == "[redacted]"


def test_migration_is_additive_and_hash_only():
    sql = (ROOT / "migrations" / "163_ohvis_internal_llmops_foundation.sql").read_text()
    assert "CREATE TABLE IF NOT EXISTS llmops_ingest_clients" in sql
    assert "token_hash TEXT NOT NULL" in sql
    executable = "\n".join(line.split("--", 1)[0] for line in sql.splitlines()).upper()
    for forbidden in ("DROP TABLE", "TRUNCATE", "DELETE FROM"):
        assert forbidden not in executable


def test_service_auth_exception_is_exact_path_only():
    source = (ROOT / "app" / "main.py").read_text()
    assert '"/api/v1/ohvis/llmops/trace-ingest"' in source
    assert "path in _SERVICE_AUTH_EXACT_PATHS" in source
