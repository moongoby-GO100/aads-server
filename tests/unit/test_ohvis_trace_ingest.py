from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
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

    def transaction(self, **_kwargs):
        return AsyncContext()

    async def execute(self, query, *args):
        self.executed.append((query, args))
        return "SELECT 1"

    async def fetchrow(self, query, *args):
        if "FROM llmops_traces" in query:
            return self.trace
        return None


class RecordingConn:
    """record_trace의 실제 INSERT 인자를 그대로 붙잡는 커넥션 대역."""

    def __init__(self, table_exists: bool = True, raise_on_write: bool = False):
        self.table_exists = table_exists
        self.raise_on_write = raise_on_write
        self.executed: list[tuple] = []
        self.fetchvals: list[tuple] = []

    async def fetchval(self, query: str, *args):
        if "information_schema.tables" in query:
            return self.table_exists
        if self.raise_on_write:
            raise RuntimeError("insert boom")
        self.fetchvals.append((query, args))
        return "11111111-2222-3333-4444-555555555555"

    async def execute(self, query: str, *args):
        if self.raise_on_write:
            raise RuntimeError("insert boom")
        self.executed.append((query, args))
        return "INSERT 0 1"


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
        asyncio.run(ohvis_llmops.require_trace_ingest_client(authorization=None))
    assert missing.value.status_code == 401

    async def no_client(_token):
        return None

    monkeypatch.setattr(llmops_store, "authenticate_ingest_client", no_client)
    with pytest.raises(HTTPException) as invalid:
        asyncio.run(ohvis_llmops.require_trace_ingest_client(authorization="Bearer wrong"))
    assert invalid.value.status_code == 401


def test_auth_store_failure_is_sanitized_503(monkeypatch):
    async def unavailable(_token):
        raise RuntimeError("database detail must not leak")

    monkeypatch.setattr(llmops_store, "authenticate_ingest_client", unavailable)
    with pytest.raises(HTTPException) as failed:
        asyncio.run(
            ohvis_llmops.require_trace_ingest_client(
                authorization="Bearer syntactically-valid-token"
            )
        )

    assert failed.value.status_code == 503
    assert "database detail" not in str(failed.value.detail)


def test_missing_auth_precedes_body_validation():
    app = FastAPI()
    app.include_router(ohvis_llmops.router, prefix="/api/v1")

    async def reject_missing_auth():
        raise HTTPException(status_code=401, detail="trace ingest credential required")

    app.dependency_overrides[ohvis_llmops.require_trace_ingest_client] = reject_missing_auth
    response = TestClient(app).post("/api/v1/ohvis/llmops/trace-ingest", json={})

    assert response.status_code == 401


def test_unauthenticated_malformed_body_is_401_not_422(monkeypatch):
    """미인증 발신자는 파싱 오류로 스키마를 떠볼 수 없어야 한다."""
    app = FastAPI()
    app.include_router(ohvis_llmops.router, prefix="/api/v1")

    async def no_client(_token):
        return None

    monkeypatch.setattr(llmops_store, "authenticate_ingest_client", no_client)
    client = TestClient(app)
    url = "/api/v1/ohvis/llmops/trace-ingest"

    for body, headers in (
        (b'{"schema_version": ', {"Content-Type": "application/json"}),
        (b"not json at all", {"Content-Type": "text/plain"}),
        (b"", {}),
    ):
        response = client.post(url, content=body, headers=headers)
        assert response.status_code == 401, body
        assert "schema_version" not in response.text

    authorized = client.post(
        url, content=b'{"broken', headers={"Content-Type": "application/json", "Authorization": "Bearer x"}
    )
    assert authorized.status_code == 401


def test_authenticated_malformed_body_is_422_with_fastapi_shape():
    app = FastAPI()
    app.include_router(ohvis_llmops.router, prefix="/api/v1")
    app.dependency_overrides[ohvis_llmops.require_trace_ingest_client] = lambda: {
        "client_id": "go100-primary",
        "project": "GO100",
    }
    response = TestClient(app).post(
        "/api/v1/ohvis/llmops/trace-ingest",
        content=b'{"broken',
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body"]


@pytest.mark.parametrize(
    "override",
    [
        {"unknown_field": "extra"},
        {"external_trace_id": "has space"},
        {"external_trace_id": "trace-​-zero-width"},
        {"input_summary": "nul\x00byte"},
        {"error": "bell\x07"},
        {"session_id": "not-a-uuid"},
        {"run_type": "DROP TABLE"},
        {"tags": ["bell\x07"]},
        {"tool_calls": [{"tool_name": "t", "risk_tier": "WRITE!"}]},
        {"tool_calls": [{"tool_name": "t", "unknown_field": 1}]},
    ],
)
def test_external_input_is_strictly_rejected(override):
    with pytest.raises(ValidationError):
        payload(**override)


def test_scope_mismatch_is_403(monkeypatch):
    async def wrong_scope(_token):
        return {"client_id": "sf-client", "project": "SF"}

    with pytest.raises(HTTPException) as mismatch:
        asyncio.run(
            ohvis_llmops.llmops_trace_ingest(
                payload(), client=asyncio.run(wrong_scope("token"))
            )
        )
    assert mismatch.value.status_code == 403


def test_long_external_ids_use_distinct_bounded_idempotency_keys(monkeypatch):
    conn = FakeConn()
    pool = FakePool(conn)
    import app.core.db_pool as db_pool
    monkeypatch.setattr(db_pool, "get_pool", lambda: pool)
    calls = []

    async def record_trace(**kwargs):
        calls.append(kwargs)
        conn.trace = {"id": kwargs["external_trace_id"], "created_at": "2026-09-09T00:00:00Z"}
        return kwargs["external_trace_id"]

    monkeypatch.setattr(llmops_store, "record_trace", record_trace)
    external_id = "x" * 200
    body = payload(external_trace_id=external_id).model_dump(mode="json")
    first = asyncio.run(llmops_store.ingest_external_trace(body, client_id="go100-primary"))
    replay = asyncio.run(llmops_store.ingest_external_trace(body, client_id="go100-primary"))

    expected_key = llmops_store.external_trace_key("GO100", external_id)
    assert calls[0]["trace_key"] == expected_key
    assert len(expected_key) <= llmops_store.TRACE_KEY_LIMIT
    assert expected_key != llmops_store.external_trace_key("GO100", "x" * 199 + "y")
    assert conn.executed[0][1][0] == expected_key
    assert first["trace_id"] == replay["trace_id"] == external_id
    assert replay["deduplicated"] is True


def test_forged_hashed_key_cannot_collide_with_a_long_id(monkeypatch):
    """해시 형태를 흉내낸 짧은 ID가 다른 트레이스의 키를 가로채지 못한다."""
    long_id = "x" * 200
    hashed_key = llmops_store.external_trace_key("GO100", long_id)
    forged_id = hashed_key.split("GO100:", 1)[1]

    forged_key = llmops_store.external_trace_key("GO100", forged_id)

    assert hashed_key.startswith("external:GO100:sha256:")
    assert len(forged_id) <= 200
    assert forged_key != hashed_key
    assert len(forged_key) <= llmops_store.TRACE_KEY_LIMIT


def test_external_ingest_keeps_trace_id_out_of_the_internal_namespace(monkeypatch):
    """외부 ID가 UNIQUE trace_id 컬럼을 점유하면 내부 trace와 충돌한다."""
    conn = FakeConn()
    pool = FakePool(conn)
    import app.core.db_pool as db_pool
    monkeypatch.setattr(db_pool, "get_pool", lambda: pool)
    observed = {}

    async def record_trace(**kwargs):
        observed.update(kwargs)
        return "central-1"

    monkeypatch.setattr(llmops_store, "record_trace", record_trace)
    asyncio.run(
        llmops_store.ingest_external_trace(payload().model_dump(mode="json"), client_id="go100-primary")
    )

    assert observed["trace_id_override"] == "external:GO100:go100-trace-123"
    assert observed["external_trace_id"] == "go100-trace-123"
    assert "NULLIF($21::text, '')" in llmops_store._INSERT_TRACE_SQL


def test_record_trace_uses_the_override_for_the_unique_trace_id_column(monkeypatch):
    conn = RecordingConn()
    import app.core.db_pool as db_pool
    monkeypatch.setattr(db_pool, "get_pool", lambda: FakePool(conn))
    llmops_store.reset_relation_cache()

    asyncio.run(
        llmops_store.record_trace(
            graph_run_id="go100-trace-123",
            external_trace_id="go100-trace-123",
            trace_key="external:GO100:go100-trace-123",
            trace_id_override="external:GO100:go100-trace-123",
        )
    )

    _query, args = conn.fetchvals[0]
    assert args[20] == "external:GO100:go100-trace-123"
    llmops_store.reset_relation_cache()


def test_record_trace_preserves_bounded_trace_key(monkeypatch):
    """이관: 긴 외부 ID의 키가 잘리지 않고 그대로 저장된다."""
    conn = RecordingConn()
    import app.core.db_pool as db_pool
    monkeypatch.setattr(db_pool, "get_pool", lambda: FakePool(conn))
    llmops_store.reset_relation_cache()
    trace_key = llmops_store.external_trace_key("GO100", "x" * 200)

    asyncio.run(llmops_store.record_trace(graph_run_id="x" * 200, trace_key=trace_key))

    _query, args = conn.fetchvals[0]
    assert args[0] == trace_key
    llmops_store.reset_relation_cache()


def test_record_trace_strict_mode_propagates_insert_failure(monkeypatch):
    """이관: strict 모드에서만 예외가 호출부로 올라간다."""
    conn = RecordingConn(raise_on_write=True)
    import app.core.db_pool as db_pool
    monkeypatch.setattr(db_pool, "get_pool", lambda: FakePool(conn))
    llmops_store.reset_relation_cache()

    with pytest.raises(RuntimeError, match="insert boom"):
        asyncio.run(llmops_store.record_trace(graph_run_id="run:x", strict=True))

    llmops_store.reset_relation_cache()
    assert asyncio.run(llmops_store.record_trace(graph_run_id="run:x")) is None
    llmops_store.reset_relation_cache()


def test_control_characters_are_stripped_before_storage(monkeypatch):
    """NUL 한 글자가 strict 트랜잭션 전체를 영구 실패시키지 않아야 한다."""
    conn = FakeConn()
    pool = FakePool(conn)
    import app.core.db_pool as db_pool
    monkeypatch.setattr(db_pool, "get_pool", lambda: pool)
    calls = []

    async def record_trace(**kwargs):
        calls.append(kwargs)
        return "central-1"

    monkeypatch.setattr(llmops_store, "record_trace", record_trace)
    body = payload().model_dump(mode="json")
    body["input_summary"] = "before\x00after"
    body["external_trace_id"] = "go100\x00trace"
    body["metadata"] = {"note\x00": "value\x00"}
    asyncio.run(llmops_store.ingest_external_trace(body, client_id="go100-primary"))

    assert calls[0]["input_summary"] == "beforeafter"
    assert calls[0]["external_trace_id"] == "go100trace"
    assert calls[0]["trace_key"] == "external:GO100:go100trace"
    assert calls[0]["metadata"]["note"] == "value"


def test_external_ingest_requests_strict_atomic_child_writes(monkeypatch):
    conn = FakeConn()
    pool = FakePool(conn)
    import app.core.db_pool as db_pool
    monkeypatch.setattr(db_pool, "get_pool", lambda: pool)
    observed = {}

    async def record_trace(**kwargs):
        observed.update(kwargs)
        return "central-1"

    monkeypatch.setattr(llmops_store, "record_trace", record_trace)
    asyncio.run(
        llmops_store.ingest_external_trace(
            payload().model_dump(mode="json"), client_id="go100-primary"
        )
    )

    assert observed["conn"] is conn
    assert observed["strict"] is True


def test_store_failure_is_sanitized_retryable_503(monkeypatch):
    async def unavailable(*_args, **_kwargs):
        raise RuntimeError("database detail must not leak")

    monkeypatch.setattr(llmops_store, "ingest_external_trace", unavailable)
    with pytest.raises(HTTPException) as failed:
        asyncio.run(
            ohvis_llmops.llmops_trace_ingest(
                payload(), client={"client_id": "go100-primary", "project": "GO100"}
            )
        )

    assert failed.value.status_code == 503
    assert failed.value.headers["Retry-After"] == "1"
    assert "database detail" not in str(failed.value.detail)


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
