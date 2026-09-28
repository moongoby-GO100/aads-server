import importlib.util
import sys
import types
from pathlib import Path

import pytest


SPEC = importlib.util.spec_from_file_location(
    "project_document_m3_inventory",
    Path(__file__).resolve().parents[2] / "scripts/project_document_m3_inventory.py",
)
m3 = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m3)


def test_repeatable_comparison_detects_key_hash_and_path_conflicts(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "a.md").write_text("same body", encoding="utf-8")
    (tmp_path / "docs" / "b.md").write_text("same body", encoding="utf-8")
    rows = [
        {"document_key": "a", "kind": "prd", "version": "1.0.0", "doc_path": "docs/a.md"},
        {"document_key": "a", "kind": "prd", "version": "1.0.0", "doc_path": "docs/a.md"},
        {"document_key": "b", "kind": "prd", "version": "1.0.0", "doc_path": "docs/b.md"},
    ]
    revisions = [{"document_key": "a", "kind": "prd", "version": "1.0.0",
                  "source_path": "docs/a.md", "content_hash": "0" * 64}]
    result = m3.compare(rows, [], [], revisions, {"heads": 1}, tmp_path)
    assert result == m3.compare(rows, [], [], revisions, {"heads": 1}, tmp_path)
    goal = result["goal_documents"]
    assert goal["comparison"] == {"hash_mismatch": 2, "path_mismatch": 1}
    assert goal["duplicate_identity_groups"] == 1
    assert goal["duplicate_path_groups"] == 1
    assert goal["duplicate_content_hash_groups"] == 1
    assert goal["unmatched_rows"] == 3
    assert "same body" not in str(result)
    assert "docs/a.md" not in str(result)


def test_m1_legacy_link_contract_ignores_key_but_records_difference(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "a.md").write_text("same body", encoding="utf-8")
    digest = m3.source_hash("docs/a.md", tmp_path)[1]
    rows = [{"document_key": "legacy-key", "kind": "prd", "version": "1.0.0",
             "doc_path": "docs/a.md"}]
    revisions = [{"document_key": "canonical-key", "kind": "prd", "version": "1.0.0",
                  "source_path": "docs/a.md", "content_hash": digest}]
    goal = m3.compare(rows, [], [], revisions, {}, tmp_path)["goal_documents"]
    assert goal["comparison"] == {"exact": 1}
    assert goal["unmatched_rows"] == 0
    assert goal["document_key_observation"] == {"different": 1}
    assert goal["review_rows"][0]["document_key_observation"] == "different"


def test_multiple_exact_revisions_are_ambiguous_and_counted(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "a.md").write_text("same body", encoding="utf-8")
    digest = m3.source_hash("docs/a.md", tmp_path)[1]
    rows = [{"document_key": "legacy", "kind": "prd", "version": "1", "doc_path": "docs/a.md"}]
    revisions = [{"document_key": key, "kind": "prd", "version": "1",
                  "source_path": "docs/a.md", "content_hash": digest} for key in ("a", "b")]
    goal = m3.compare(rows, [], [], revisions, {}, tmp_path)["goal_documents"]
    assert goal["comparison"] == {"ambiguous_exact": 1}
    assert goal["review_rows"][0]["exact_candidate_count"] == 2
    assert goal["review_rows"][0]["document_key_observation"] == "ambiguous"


def test_non_utf8_file_is_counted_separately(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "bad.md").write_bytes(b"\xff\xfe")
    assert m3.source_hash("docs/bad.md", tmp_path) == ("non_utf8", None)
    rows = [{"document_key": "bad", "kind": "prd", "version": "1", "doc_path": "docs/bad.md"}]
    goal = m3.compare(rows, [], [], [], {}, tmp_path)["goal_documents"]
    assert goal["file_status"] == {"non_utf8": 1}


def test_symlink_resolution_runtime_error_does_not_abort_inventory(monkeypatch, tmp_path):
    (tmp_path / "docs").mkdir()
    original_resolve = Path.resolve

    def resolve(path, *args, **kwargs):
        if path.name == "loop.md":
            raise RuntimeError("Symlink loop")
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    rows = [{"document_key": "loop", "kind": "prd", "version": "1", "doc_path": "docs/loop.md"},
            {"document_key": "null", "kind": "prd", "version": "1", "doc_path": None}]
    goal = m3.compare(rows, [], [], [], {}, tmp_path)["goal_documents"]
    assert goal["rows"] == 2
    assert goal["file_status"] == {"invalid_path": 2}


def test_empty_and_bad_paths_never_read_outside_root(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "reports").mkdir()
    (tmp_path / "private").mkdir()
    (tmp_path / "private" / "inside.md").write_text("private", encoding="utf-8")
    (tmp_path / "reports" / "public.md").write_text("public", encoding="utf-8")
    (tmp_path / "docs" / "link.md").symlink_to("/etc/passwd")
    (tmp_path / "docs" / "private.md").symlink_to(tmp_path / "private" / "inside.md")
    (tmp_path / "docs" / "public.md").symlink_to(tmp_path / "reports" / "public.md")
    (tmp_path / "docs" / "loop.md").symlink_to("loop.md")
    for path in ("../outside.md", "docs/../reports/public.md", "/etc/passwd", "docs/link.md", "https://host/doc", "docs/.hidden"):
        assert m3.source_hash(path, tmp_path)[0] == "invalid_path"
    assert m3.source_hash("docs/private.md", tmp_path)[0] == "invalid_path"
    assert m3.source_hash("docs/public.md", tmp_path)[0] == "ok"
    assert m3.source_hash("docs/missing.md", tmp_path)[0] == "missing_or_unreadable"
    assert m3.source_hash("docs/loop.md", tmp_path)[0] in {"invalid_path", "missing_or_unreadable"}
    empty = m3.compare([], [], [], [], {"heads": 0}, tmp_path)
    assert empty["goal_documents"]["rows"] == 0
    assert empty["chat_artifacts"]["rows"] == 0
    assert empty["canonical"]["heads"] == 0


def test_parent_component_is_invalid_before_path_resolution(tmp_path):
    (tmp_path / "reports").mkdir()
    (tmp_path / "reports" / "public.md").write_text("public", encoding="utf-8")
    assert m3.source_hash("docs/../reports/public.md", tmp_path) == ("invalid_path", None)


def test_artifact_and_chat_duplicates_remain_unmatched(tmp_path):
    artifacts = [
        {"artifact_type": "prd", "artifact_name": "x", "version": 1, "json_hash": "a" * 64},
        {"artifact_type": "prd", "artifact_name": "x", "version": 1, "json_hash": "a" * 64},
    ]
    chats = [{"type": "report", "rows": 3, "distinct_hashes": 1}]
    result = m3.compare([], artifacts, chats, [], {}, tmp_path)
    assert result["project_artifacts"]["duplicate_identity_groups"] == 1
    assert result["project_artifacts"]["duplicate_json_hash_groups"] == 1
    assert result["project_artifacts"]["unmatched_rows"] == 2
    assert result["chat_artifacts"]["duplicate_content_hash_rows"] == 2
    assert result["chat_artifacts"]["unmatched_rows"] == 3


def test_all_queries_enforce_tenant_and_project_before_comparison():
    for sql in (m3.GOALS_SQL, m3.ARTIFACTS_SQL, m3.CHAT_SQL, m3.CANONICAL_SQL, *m3.COUNT_SQL.values()):
        assert "tenant_id=$1::uuid" in sql
        assert "$2" in sql
    assert m3.CHAT_SQL.count("tenant_id=$1::uuid") == 3
    assert m3.CANONICAL_SQL.count("tenant_id=$1::uuid") == 2
    assert not hasattr(m3, "GLOBAL_COUNT_SQL")


def test_database_url_precedes_pg_fallback_and_errors_are_safe(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://name:secret%20word@postgres:5432/aads")
    monkeypatch.setenv("PGHOST", "127.0.0.1")
    params = m3.connection_params()
    assert params == {"host": "postgres", "port": 5432, "database": "aads",
                      "user": "name", "password": "secret word"}
    monkeypatch.delenv("DATABASE_URL")
    monkeypatch.setenv("PGPORT", "5433")
    assert m3.connection_params()["host"] == "127.0.0.1"
    assert m3.connection_params()["port"] == 5433
    class SchemaError(Exception):
        sqlstate = "42883"
    assert "schema: SchemaError: digest unavailable" == m3.classified_error(SchemaError("digest unavailable"))
    error = m3.classified_error(OSError("postgresql://name:secret@host/db password=secret"))
    assert error.startswith("connection:")
    assert "secret" not in error
    class PermissionErrorFromDb(Exception):
        sqlstate = "42501"
    assert m3.classified_error(PermissionErrorFromDb("denied")).startswith("permission:")


def test_password_values_are_fully_redacted():
    for raw in (
        "password=simple",
        "password='two words'",
        'password="a b c"',
    ):
        masked = m3.classified_error(OSError(raw))
        assert "[redacted]" in masked
        assert raw.split("=", 1)[1] not in masked


@pytest.mark.asyncio
async def test_invalid_scope_is_rejected_before_database(monkeypatch):
    with pytest.raises(ValueError):
        await m3.inventory("00000000-0000-0000-0000-000000000001", "OTHER/PROJECT")
    with pytest.raises(ValueError):
        await m3.inventory("wrong-tenant", "AADS")


@pytest.mark.asyncio
async def test_read_only_transaction_passes_exact_scope_to_every_row_query(monkeypatch, tmp_path):
    tenant = "00000000-0000-0000-0000-000000000001"
    calls = []

    class Transaction:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    class Connection:
        def transaction(self, *, readonly, isolation):
            assert readonly and isolation == "repeatable_read"
            return Transaction()

        async def fetch(self, sql, *args):
            calls.append((sql, args))
            return []

        async def fetchval(self, sql, *args):
            calls.append((sql, args))
            if sql == "SELECT public.aads_internal_tenant_id()::text":
                return tenant
            return 0

        async def close(self):
            pass

    async def connect(**kwargs):
        assert kwargs["server_settings"]["default_transaction_read_only"] == "on"
        return Connection()

    monkeypatch.setitem(sys.modules, "asyncpg", types.SimpleNamespace(connect=connect))
    result = await m3.inventory(tenant, "AADS", tmp_path)
    assert result["goal_documents"]["rows"] == 0
    scoped_calls = [args for sql, args in calls
                    if sql != "SELECT public.aads_internal_tenant_id()::text"]
    assert len(scoped_calls) == 8
    assert all(args == (tenant, "AADS") for args in scoped_calls)
    assert ("SELECT public.aads_internal_tenant_id()::text", ()) in calls
    calls.clear()
    with pytest.raises(ValueError, match="tenant does not match"):
        await m3.inventory("00000000-0000-0000-0000-000000000002", "AADS", tmp_path)
    assert not any(args for _, args in calls)
