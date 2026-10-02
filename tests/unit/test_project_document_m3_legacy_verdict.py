import csv
import importlib.util
import io
import re
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "project_document_m3_inventory_verdict", ROOT / "scripts/project_document_m3_inventory.py")
m3 = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m3)

TENANT = "00000000-0000-0000-0000-000000000001"


def judge(**overrides):
    base = dict(path_class="docs_relative", file_status="ok", secret=False, status="active",
                is_latest=True, key_active_count=1, kind="prd", canonical_exact=False,
                canonical_kind_version=False, canonical_hash_match=False, legacy_linked=False)
    base.update(overrides)
    return m3.judge(**base)


def row(rid, path, **overrides):
    base = {"id": rid, "goal_id": "g1", "tenant_id": TENANT, "project": "AADS", "kind": "prd",
            "doc_path": path, "title": "t", "document_key": f"prd:{rid}", "version": "1.0.0",
            "status": "active", "is_latest": True, "change_summary": None}
    base.update(overrides)
    return base


@pytest.mark.parametrize("path,expected", [
    ("docs/a.md", "docs_relative"),
    ("reports/a.md", "reports_relative"),
    ("app/static/reports/x.html", "app_static"),
    ("https://example.com/x", "external_url"),
    ("/root/aads/aads-server/docs/a.md", "server_absolute"),
    ("/reports/x.html", "server_absolute"),
    ("src/a.md", "other"),
    ("", "other"),
    (None, "other"),
])
def test_classify_path(path, expected):
    assert m3.classify_path(path) == expected


def test_judge_covers_each_verdict():
    assert judge()[0] == "needs_canonical_import"
    assert judge(canonical_exact=True)[0] == "ready"
    assert judge(legacy_linked=True)[0] == "ready"
    assert judge(path_class="server_absolute", file_status=None)[0] == "path_not_allowed"
    assert judge(file_status="invalid_path")[0] == "path_not_allowed"
    for state in ("missing_or_unreadable", "empty", "too_large", "non_utf8"):
        assert judge(file_status=state)[0] == "missing_file"
    assert judge(secret=True)[0] == "secret_detected"
    assert judge(status="superseded", is_latest=False)[0] == "superseded"
    assert judge(status="archived", is_latest=False)[0] == "superseded"
    assert judge(key_active_count=2)[0] == "duplicate_key"


def test_judge_priority_and_reasons():
    assert judge(secret=True, status="superseded", is_latest=False,
                 path_class="external_url")[0] == "secret_detected"
    assert judge(status="superseded", is_latest=False, path_class="server_absolute",
                 file_status=None)[0] == "superseded"
    assert judge(path_class="app_static", file_status=None, key_active_count=3)[0] == "path_not_allowed"
    assert judge(file_status="empty", key_active_count=3)[0] == "missing_file"
    assert "hash" in judge(canonical_hash_match=True, canonical_kind_version=True)[1]
    assert "kind/version" in judge(canonical_kind_version=True)[1]
    assert "not a canonical kind" in judge(kind="prototype")[1]
    assert judge(kind="prototype")[0] == "needs_canonical_import"


def test_build_rows_uses_every_row_and_never_emits_bodies_or_absolute_paths(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "ok.md").write_text("plain body text", encoding="utf-8")
    (tmp_path / "docs" / "secret.md").write_text("api_key = abcdefghijklmnop", encoding="utf-8")
    (tmp_path / "docs" / "big.md").write_bytes(b"x" * (m3.MAX_BYTES + 1))
    digest = m3.source_hash("docs/ok.md", tmp_path)[1]
    rows = [
        row(1, "docs/ok.md"),
        row(2, "docs/secret.md"),
        row(3, "docs/missing.md"),
        row(4, "docs/big.md"),
        row(5, "/root/aads/aads-server/docs/ok.md"),
        row(6, "https://example.com/doc"),
        row(7, "app/static/x.html"),
        row(8, "docs/ok.md", status="superseded", is_latest=False, document_key="dup"),
        row(9, "docs/ok.md", document_key="dup"),
        row(10, "docs/ok.md", document_key="dup"),
        row(11, "docs/ok.md", kind="plan", document_key="plan:ready"),
    ]
    revisions = [{"tenant_id": TENANT, "project_key": "AADS", "kind": "plan", "version": "1.0.0",
                  "source_path": "docs/ok.md", "content_hash": digest}]
    out = m3.build_verdict_rows(rows, revisions, [], tmp_path)
    assert len(out) == len(rows)
    verdicts = {r["id"]: r["verdict"] for r in out}
    assert verdicts == {1: "needs_canonical_import", 2: "secret_detected", 3: "missing_file",
                        4: "missing_file", 5: "path_not_allowed", 6: "path_not_allowed",
                        7: "path_not_allowed", 8: "superseded", 9: "duplicate_key",
                        10: "duplicate_key", 11: "ready"}
    by_id = {r["id"]: r for r in out}
    assert by_id[9]["key_row_count"] == 3 and by_id[9]["key_active_count"] == 2
    assert by_id[4]["too_large"] is True and by_id[1]["too_large"] is False
    assert by_id[3]["file_exists"] == "false" and by_id[1]["file_exists"] == "true"
    assert by_id[5]["file_exists"] == "n/a" and by_id[5]["public_path"] is None
    assert by_id[1]["file_sha256"] == digest
    assert by_id[2]["secret_detected"] is True and by_id[1]["secret_detected"] is False
    assert by_id[11]["canonical_exact_match"] is True and by_id[11]["canonical_kind_version_exists"] is True
    text = m3.rows_to_csv(out)
    assert "plain body text" not in text and "abcdefghijklmnop" not in text
    assert "/root/aads" not in text and "example.com" not in text
    parsed = list(csv.DictReader(io.StringIO(text)))
    assert len(parsed) == len(rows) and list(parsed[0]) == list(m3.ROW_COLUMNS)


def test_metadata_secret_is_redacted_and_blocks(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "ok.md").write_text("fine", encoding="utf-8")
    secret_title = "password=hunter2hunter2"
    out = m3.build_verdict_rows([row(1, "docs/ok.md", title=secret_title,
                                     document_key="prd:key")], [], [], tmp_path)
    assert out[0]["verdict"] == "secret_detected"
    leaked = m3.build_verdict_rows([row(2, "docs/ok.md", document_key="access_token=abcdefghijklmnopqrstu")],
                                   [], [], tmp_path)
    assert leaked[0]["document_key"] == "[redacted]"
    assert "hunter2" not in m3.rows_to_csv(out + leaked)


def test_canonical_match_is_tenant_and_project_scoped(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "ok.md").write_text("fine", encoding="utf-8")
    digest = m3.source_hash("docs/ok.md", tmp_path)[1]
    other = [{"tenant_id": "other", "project_key": "AADS", "kind": "prd", "version": "1.0.0",
              "source_path": "docs/ok.md", "content_hash": digest},
             {"tenant_id": TENANT, "project_key": "ACCT", "kind": "prd", "version": "1.0.0",
              "source_path": "docs/ok.md", "content_hash": digest}]
    out = m3.build_verdict_rows([row(1, "docs/ok.md")], other, [], tmp_path)
    assert out[0]["verdict"] == "needs_canonical_import"
    assert out[0]["canonical_kind_version_exists"] is False


def test_already_linked_row_is_ready_and_lineage_head_is_not_duplicate(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "ok.md").write_text("fine", encoding="utf-8")
    lineage = [row(1, "docs/ok.md", document_key="k", status="superseded", is_latest=False),
               row(2, "docs/ok.md", document_key="k", version="1.1.0")]
    out = m3.build_verdict_rows(lineage, [], [2], tmp_path)
    assert [r["verdict"] for r in out] == ["superseded", "ready"]
    out = m3.build_verdict_rows(lineage, [], [], tmp_path)
    assert [r["verdict"] for r in out] == ["superseded", "needs_canonical_import"]
    assert out[1]["key_row_count"] == 2


def test_summary_counts_sum_to_total_and_flag_db_mismatch():
    rows = [{"verdict": "ready", "project": "A"}, {"verdict": "superseded", "project": "A"},
            {"verdict": "missing_file", "project": "B"}]
    summary = m3.summarize_verdicts(rows, 3)
    assert set(summary["by_verdict"]) == set(m3.VERDICTS) and len(m3.VERDICTS) == 7
    assert sum(summary["by_verdict"].values()) == summary["total_rows"] == 3
    assert summary["total_matches_db"] and summary["verdict_sum_matches_total"]
    assert summary["by_project"]["A"]["total"] == 2
    assert not m3.summarize_verdicts(rows, 4)["total_matches_db"]


def test_csv_neutralises_formula_cells():
    assert m3._cell("=cmd") == "'=cmd"
    assert m3._cell(True) == "true" and m3._cell(None) == ""


def test_verdict_sql_is_select_only_and_unlimited():
    for sql in (m3.VERDICT_ROWS_SQL, m3.VERDICT_ROWS_PROJECT_SQL, m3.VERDICT_COUNT_SQL,
                m3.VERDICT_COUNT_PROJECT_SQL, m3.VERDICT_REVISIONS_SQL, m3.VERDICT_LINKED_SQL):
        assert sql.lstrip().upper().startswith("SELECT")
        assert not re.search(r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|CREATE)\b", sql, re.I)
        assert "LIMIT" not in sql.upper()
    assert "LEFT JOIN goals" in m3.VERDICT_ROWS_SQL
    assert m3.VERDICT_COUNT_SQL == "SELECT count(*) FROM goal_documents"


def test_secret_pattern_matches_canonical_api_pattern():
    source = (ROOT / "app/api/canonical_documents.py").read_text(encoding="utf-8")
    start = source.index("SECRET = re.compile(")
    end = source.index("\n)\n", start) + 3
    namespace = {"re": re}
    exec(source[start:end], namespace)  # only the regex literal assignment
    assert namespace["SECRET"].pattern == m3.SECRET.pattern


@pytest.mark.asyncio
async def test_verdict_inventory_reads_without_limit_in_readonly_transaction(monkeypatch, tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "a.md").write_text("fine", encoding="utf-8")
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
            calls.append(sql)
            if sql == m3.VERDICT_ROWS_SQL:
                return [row(i, "docs/a.md", document_key=f"k{i}") for i in range(1, 206)]
            return []

        async def fetchval(self, sql, *args):
            calls.append(sql)
            return 205

        async def close(self):
            pass

    async def connect(**kwargs):
        assert kwargs["server_settings"]["default_transaction_read_only"] == "on"
        return Connection()

    monkeypatch.setitem(sys.modules, "asyncpg", types.SimpleNamespace(connect=connect))
    rows, db_count = await m3.verdict_inventory(None, tmp_path)
    assert len(rows) == db_count == 205
    assert m3.VERDICT_COUNT_SQL in calls
    with pytest.raises(ValueError):
        await m3.verdict_inventory("BAD/KEY", tmp_path)
