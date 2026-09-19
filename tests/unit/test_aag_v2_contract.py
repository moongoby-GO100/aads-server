from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import pytest

from app.services import aag_ingest_v2
from scripts.aag_snapshot_push import _v2_statement
from tools.aag.v2_contract import content_fingerprint, input_fingerprint

ROOT = Path(__file__).resolve().parents[2]


def test_content_fingerprint_ignores_time_host_and_collection_order():
    left = {
        "generated_at": "2026-09-19T10:00:00+09:00",
        "host": "scanner-a",
        "nodes": [{"id": "b"}, {"id": "a"}],
        "edges": [{"to": "b", "from": "a"}],
        "findings": [{"rule": "X", "path": "app\\x.py"}],
        "stats": {"b": 2, "a": 1},
    }
    right = {
        "generated_at": "2026-09-19T11:00:00+09:00",
        "host": "scanner-b",
        "nodes": [{"id": "a"}, {"id": "b"}],
        "edges": [{"from": "a", "to": "b"}],
        "findings": [{"path": "app/x.py", "rule": "X"}],
        "stats": {"a": 1, "b": 2},
    }

    assert content_fingerprint(left) == content_fingerprint(right)


def test_input_fingerprint_changes_with_resolved_commit():
    common = {
        "project": "GO100", "repository_id": "kis-autotrade-v4",
        "target_ref": "refs/heads/main", "scanner_version": "aag-scanner-v1.1",
        "ruleset_digest": "a" * 64, "scan_scope_digest": "b" * 64,
        "parser_versions": {"python_ast": "3.12"},
        "expected_target_ref_head_sha": "1" * 40,
    }
    first = input_fingerprint(resolved_commit_sha="1" * 40, **common)
    second = input_fingerprint(resolved_commit_sha="2" * 40, **common)

    assert len(first) == 64
    assert first != second


def test_input_fingerprint_isolates_governance_scope():
    common = {
        "project": "GO100", "repository_id": "kis-autotrade-v4",
        "target_ref": "refs/heads/main", "resolved_commit_sha": "1" * 40,
        "expected_target_ref_head_sha": "1" * 40,
        "scanner_version": "aag-scanner-v1.1", "ruleset_digest": "a" * 64,
        "scan_scope_digest": "b" * 64,
    }
    assert input_fingerprint(governance_scope="default", **common) != input_fingerprint(
        governance_scope="release", **common
    )


def test_v11_foundation_migration_is_additive_and_immutable():
    sql = (ROOT / "migrations/20260919_aag_v1_1_foundation.sql").read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS aag_scan_runs" in sql
    assert "CREATE TABLE IF NOT EXISTS aag_graph_snapshots_v2" in sql
    assert "CREATE TABLE IF NOT EXISTS aag_snapshot_observations" in sql
    assert "UNIQUE(project, repository_id, content_fingerprint)" in sql
    assert "ready AAG snapshots are immutable" in sql
    assert "ALTER TABLE aag_graph_snapshots" not in sql
    assert "DROP TABLE" not in sql


def test_snapshot_pusher_records_no_change_observation_contract():
    graph = {
        "generated_at": "2026-09-19T16:00:00+09:00",
        "source_identity": {
            "repository_id": "kis-autotrade-v4",
            "target_ref": "refs/heads/main",
            "resolved_commit_sha": "1" * 40,
            "expected_target_ref_head_sha": "1" * 40,
            "scanner_version": "aag-scanner-v1.1",
            "ruleset_digest": "a" * 64,
            "scan_scope_digest": "b" * 64,
            "normalization_version": "aag-c14n-v1",
            "stable_key_version": "aag-stable-key-v1",
            "parser_versions": {"python_ast": "3.12"},
        },
        "stats": {}, "nodes": [], "edges": [], "findings": [], "unresolved": [],
    }

    sql = _v2_statement("GO100", graph)

    assert sql is not None
    assert "ON CONFLICT (project, repository_id, content_fingerprint) DO NOTHING" in sql
    assert "no_change_success" in sql
    assert "aag_snapshot_observations" in sql
    assert "verification_status" in sql


def test_snapshot_pusher_marks_old_commit_non_authoritative():
    graph = {
        "generated_at": "2026-09-19T16:00:00+09:00",
        "source_identity": {
            "repository_id": "kis-autotrade-v4",
            "target_ref": "refs/heads/main",
            "resolved_commit_sha": "1" * 40,
            "expected_target_ref_head_sha": "2" * 40,
            "scanner_version": "aag-scanner-v1.1",
            "ruleset_digest": "a" * 64,
            "scan_scope_digest": "b" * 64,
            "normalization_version": "aag-c14n-v1",
            "stable_key_version": "aag-stable-key-v1",
            "parser_versions": {"python_ast": "3.12"},
        },
        "stats": {}, "nodes": [], "edges": [], "findings": [], "unresolved": [],
    }

    sql = _v2_statement("GO100", graph)

    assert sql is not None
    assert "commit_mismatch" in sql
    assert "authoritative" in sql


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _Connection:
    def __init__(
        self, *, existing_snapshot=False, existing_run=None, previous=None,
        fail_pointer=False,
    ):
        self.snapshot_id = uuid4()
        self.observation_id = uuid4()
        self.existing_snapshot = existing_snapshot
        self.existing_run = existing_run
        self.previous = previous
        self.fail_pointer = fail_pointer
        self.executed = []

    def transaction(self):
        return _Transaction()

    async def fetchval(self, sql, *args):
        self.executed.append((" ".join(sql.split()), args))
        if "INSERT INTO aag_scan_runs" in sql:
            return None if self.existing_run else args[0]
        if "INSERT INTO aag_graph_snapshots_v2" in sql:
            return None if self.existing_snapshot else self.snapshot_id
        if "SELECT id FROM aag_graph_snapshots_v2" in sql:
            return self.snapshot_id
        if "INSERT INTO aag_snapshot_observations" in sql:
            return self.observation_id
        raise AssertionError(sql)

    async def fetchrow(self, sql, *args):
        self.executed.append((" ".join(sql.split()), args))
        if "FROM aag_scan_runs" in sql:
            return self.existing_run
        if "FROM aag_latest_pointers" in sql:
            return self.previous
        raise AssertionError(sql)

    async def execute(self, sql, *args):
        self.executed.append((" ".join(sql.split()), args))
        if self.fail_pointer and "INSERT INTO aag_latest_pointers" in sql:
            self.fail_pointer = False
            raise RuntimeError("injected pointer failure")


class _Pool:
    def __init__(self, connection):
        self.connection = connection

    @asynccontextmanager
    async def acquire(self):
        yield self.connection


def _ingest_kwargs(**overrides):
    values = {
        "project": "GO100",
        "repository_id": "kis-autotrade-v4",
        "target_ref": "refs/heads/main",
        "governance_scope": "default",
        "resolved_commit_sha": "1" * 40,
        "expected_target_ref_head_sha": "1" * 40,
        "host": "scanner-a",
        "generated_at": "2026-09-19T16:00:00+09:00",
        "scanner_version": "aag-scanner-v1.1",
        "ruleset_digest": "a" * 64,
        "scan_scope_digest": "b" * 64,
        "parser_versions": {"python_ast": "3.12"},
        "graph": {"stats": {}, "nodes": [], "edges": [], "findings": [], "unresolved": []},
    }
    values.update(overrides)
    return values


def test_ingest_reuses_content_but_records_fresh_observation(monkeypatch):
    connection = _Connection(existing_snapshot=True)
    monkeypatch.setattr(aag_ingest_v2, "get_pool", lambda: _Pool(connection))

    result = asyncio.run(aag_ingest_v2.ingest_graph(**_ingest_kwargs()))

    assert result.result == "no_change_success"
    assert result.authoritative is True
    assert any("INSERT INTO aag_snapshot_observations" in sql for sql, _ in connection.executed)
    assert any("INSERT INTO aag_latest_pointers" in sql for sql, _ in connection.executed)
    assert not any("first_published_at=NOW()" in sql for sql, _ in connection.executed)


def test_ingest_old_commit_cannot_be_authoritative(monkeypatch):
    connection = _Connection(existing_snapshot=True)
    monkeypatch.setattr(aag_ingest_v2, "get_pool", lambda: _Pool(connection))

    result = asyncio.run(aag_ingest_v2.ingest_graph(**_ingest_kwargs(
        expected_target_ref_head_sha="2" * 40,
    )))

    assert result.result == "source_behind"
    assert result.authoritative is False
    assert result.verification_status == "commit_mismatch"
    assert not any("INSERT INTO aag_latest_pointers" in sql for sql, _ in connection.executed)


def test_ingest_out_of_order_observation_preserves_pointer(monkeypatch):
    connection = _Connection(existing_snapshot=True, previous={
        "generated_at": "2026-09-19T17:00:00+09:00",
        "resolved_commit_sha": "1" * 40,
        "head_commit_sha": "1" * 40,
        "head_generated_at": "2026-09-19T17:00:00+09:00",
    })
    monkeypatch.setattr(aag_ingest_v2, "get_pool", lambda: _Pool(connection))

    result = asyncio.run(aag_ingest_v2.ingest_graph(**_ingest_kwargs()))

    assert result.result == "out_of_order"
    assert result.authoritative is False
    assert result.verification_status == "out_of_order"
    assert not any("INSERT INTO aag_latest_pointers" in sql for sql, _ in connection.executed)


def test_publish_sequence_is_fenced_and_atomic(monkeypatch):
    connection = _Connection()
    monkeypatch.setattr(aag_ingest_v2, "get_pool", lambda: _Pool(connection))

    asyncio.run(aag_ingest_v2.ingest_graph(**_ingest_kwargs()))
    statements = [sql for sql, _ in connection.executed]

    lock = next(i for i, sql in enumerate(statements) if "pg_advisory_xact_lock" in sql)
    candidate = next(i for i, sql in enumerate(statements) if "INSERT INTO aag_graph_snapshots_v2" in sql)
    ready = next(i for i, sql in enumerate(statements) if "first_published_at=NOW()" in sql)
    observation = next(i for i, sql in enumerate(statements) if "INSERT INTO aag_snapshot_observations" in sql)
    pointer = next(i for i, sql in enumerate(statements) if "INSERT INTO aag_latest_pointers" in sql)
    assert lock < candidate < ready < observation < pointer


def test_pointer_failure_records_failed_run(monkeypatch):
    connection = _Connection(fail_pointer=True)
    monkeypatch.setattr(aag_ingest_v2, "get_pool", lambda: _Pool(connection))

    with pytest.raises(RuntimeError, match="injected pointer failure"):
        asyncio.run(aag_ingest_v2.ingest_graph(**_ingest_kwargs()))

    statements = [sql for sql, _ in connection.executed]
    assert any("'ingest_failed'" in sql for sql in statements)


def test_pointer_queries_include_complete_isolation_key(monkeypatch):
    connection = _Connection(existing_snapshot=True)
    monkeypatch.setattr(aag_ingest_v2, "get_pool", lambda: _Pool(connection))

    asyncio.run(aag_ingest_v2.ingest_graph(**_ingest_kwargs(
        project="AADS", repository_id="repo-b", target_ref="refs/heads/release",
        governance_scope="regulated",
    )))

    pointer_args = next(
        args for sql, args in connection.executed if "INSERT INTO aag_latest_pointers" in sql
    )
    assert pointer_args[:4] == ("AADS", "repo-b", "refs/heads/release", "regulated")


def test_latest_pointer_migration_is_additive_and_repeatable():
    sql = (ROOT / "migrations/20260919_aag_v1_1_latest_pointers.sql").read_text(
        encoding="utf-8"
    )
    assert "CREATE TABLE IF NOT EXISTS aag_ref_heads" in sql
    assert "CREATE TABLE IF NOT EXISTS aag_latest_pointers" in sql
    assert "ADD COLUMN IF NOT EXISTS" in sql
    assert "PRIMARY KEY (project, repository_id, target_ref, governance_scope)" in sql
    assert "ADD COLUMN IF NOT EXISTS expected_target_ref_head_sha TEXT" in sql
    assert "ADD COLUMN IF NOT EXISTS verification_status TEXT" in sql
    assert sql.index("ADD COLUMN IF NOT EXISTS verification_status TEXT") < sql.index(
        "DROP CONSTRAINT IF EXISTS aag_snapshot_observations_verification_status_check"
    )
    assert "DROP TABLE" not in sql
    assert "TRUNCATE" not in sql


def test_latest_read_api_contract_is_feature_fenced():
    source = (ROOT / "app/api/aag.py").read_text(encoding="utf-8")
    latest = source[source.index('@router.get("/v2/latest")'):]
    assert "_require_v2_enabled()" in latest
    for field in (
        '"snapshot"', '"observation_id"', '"run_id"', '"ref"', '"commit"',
        '"source"', '"authoritative"', '"generated_at"', '"verified_at"',
        '"freshness"', '"limitation"',
    ):
        assert field in latest


def test_run_id_retry_is_idempotent_and_input_is_immutable(monkeypatch):
    run_id = uuid4()
    input_hash = input_fingerprint(
        project="GO100", repository_id="kis-autotrade-v4", target_ref="refs/heads/main",
        resolved_commit_sha="1" * 40, expected_target_ref_head_sha="1" * 40,
        scanner_version="aag-scanner-v1.1", ruleset_digest="a" * 64,
        scan_scope_digest="b" * 64, parser_versions={"python_ast": "3.12"},
    )
    connection = _Connection(existing_run={
        "input_fingerprint": input_hash,
        "result": "succeeded",
        "snapshot_id": uuid4(),
        "observation_id": uuid4(),
        "content_fingerprint": "c" * 64,
        "authoritative": True,
        "verification_status": "verified",
    })
    monkeypatch.setattr(aag_ingest_v2, "get_pool", lambda: _Pool(connection))

    result = asyncio.run(aag_ingest_v2.ingest_graph(**_ingest_kwargs(run_id=run_id)))

    assert result.run_id == run_id
    assert result.result == "succeeded"
    assert not any("INSERT INTO aag_snapshot_observations" in sql for sql, _ in connection.executed)

    connection.existing_run["input_fingerprint"] = "f" * 64
    with pytest.raises(aag_ingest_v2.IngestConflictError):
        asyncio.run(aag_ingest_v2.ingest_graph(**_ingest_kwargs(run_id=run_id)))
