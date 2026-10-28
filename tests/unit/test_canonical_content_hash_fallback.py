"""정본 lookup(file_path)/register 의 content_hash 보조 매칭: source_path 가 NULL 인 기존 리비전."""
from __future__ import annotations

import asyncio
import hashlib

from fastapi import HTTPException

from app.api import canonical_documents as docs
from app.services import canonical_document_tools as cdt
from tests.unit.test_canonical_document_tools import (  # noqa: F401  (autouse 게이트 스텁 포함)
    HEAD_ID,
    REV_ID,
    TENANT,
    FakeConn,
    FakePool,
    _head,
    _register,
    _stub_gate,
)

PATH = "docs/reports/20261028_GO100_doc-a.md"
BODY = "# doc-a\nbody"
DIGEST = hashlib.sha256(BODY.encode()).hexdigest()


class HashConn(FakeConn):
    """source_path 질의는 빈 결과, content_hash 질의는 rows 를 돌려준다."""

    def __init__(self, hash_rows, **kw):
        super().__init__(**kw)
        self.hash_rows = hash_rows
        self.hash_args = None

    async def fetch(self, sql, *args):
        self.sql.append(sql)
        if "r.content_hash=$3" in sql:
            self.hash_args = args
            return [row for row in self.hash_rows if args[2] == DIGEST]
        return []


def _stub_remote(monkeypatch, raw=None, error=None):
    async def fake(target, source):
        if error:
            raise error
        return raw

    monkeypatch.setattr(docs, "_read_remote_source", fake)


def _lookup(conn, **over):
    inp = {"project": "GO100", "file_path": PATH}
    inp.update(over)
    return asyncio.run(cdt.lookup_documents(tenant_id=TENANT, inp=inp, pool=FakePool(conn)))


def test_lookup_hash_match_when_source_path_is_null(monkeypatch):
    _stub_remote(monkeypatch, BODY.encode())
    conn = HashConn([_head(generation=1)])
    out = _lookup(conn)
    assert out["registered"] is True and out["matched_by"] == "content_hash"
    assert out["documents"][0]["matched_by"] == "content_hash"
    assert conn.hash_args == (TENANT, "GO100", DIGEST, cdt._LOOKUP_LIMIT)
    assert all(s.lstrip().upper().startswith("SELECT") for s in conn.sql)


def test_lookup_hash_mismatch_is_not_registered(monkeypatch):
    _stub_remote(monkeypatch, "doc-b".encode())
    out = _lookup(HashConn([_head(generation=1)]))
    assert out["registered"] is False and "matched_by" not in out


def test_lookup_remote_read_failure_is_not_registered_and_does_not_raise(monkeypatch):
    for error in (HTTPException(502, "remote_source_unavailable"), HTTPException(422, "invalid_source_path")):
        _stub_remote(monkeypatch, error=error)
        conn = HashConn([_head(generation=1)])
        out = _lookup(conn)
        assert out["registered"] is False and conn.hash_args is None


def test_lookup_source_path_match_skips_hash_fallback(monkeypatch):
    _stub_remote(monkeypatch, error=AssertionError("must not read source"))
    out = _lookup(FakeConn(rows=[_head(generation=1)]))
    assert out["registered"] is True and "matched_by" not in out


def test_lookup_local_project_reads_local_source(monkeypatch):
    monkeypatch.setattr(docs, "_read_source", lambda source: BODY.encode())
    out = _lookup(HashConn([_head(generation=1)]), project="AADS", file_path="docs/plans/doc-a.md")
    assert out["registered"] is True and out["matched_by"] == "content_hash"


def _revision_row(version="1.0.0"):
    return {"id": REV_ID, "revision": 1, "version": version, "content_hash": DIGEST, "idempotency_key": None}


def test_register_same_hash_returns_idempotent_without_new_revision(monkeypatch):
    _stub_remote(monkeypatch, BODY.encode())
    conn = FakeConn(head=_head(generation=1), revisions=1, rows=[_revision_row()])
    out = _register(conn, {"project": "GO100", "document_key": "doc-a", "kind": "spec",
                           "title": "문서 a", "file_path": PATH})
    assert out["registered"] is True and out["idempotent"] is True
    assert out["matched_by"] == "content_hash" and out["revision"] == 1 and out["version"] == "1.0.0"
    assert out["revision_id"] == str(REV_ID) and out["document_id"] == str(HEAD_ID)
    assert out["approved"] is False
    assert not any("INSERT INTO project_document_revisions" in s or s.lstrip().upper().startswith(("UPDATE", "DELETE"))
                   for s in conn.sql)


def test_register_same_hash_with_explicit_other_version_is_idempotent(monkeypatch):
    _stub_remote(monkeypatch, BODY.encode())
    conn = FakeConn(head=_head(generation=1), revisions=1, rows=[_revision_row()])
    out = _register(conn, {"project": "GO100", "document_key": "doc-a", "kind": "spec",
                           "title": "문서 a", "file_path": PATH, "version": "2.0.0"})
    assert out["idempotent"] is True and out["matched_by"] == "content_hash"


def test_register_same_hash_without_file_path_still_conflicts():
    conn = FakeConn(head=_head(generation=1), revisions=1, rows=[_revision_row()])
    out = _register(conn, {"project": "GO100", "document_key": "doc-a", "kind": "spec",
                           "title": "문서 a", "content": BODY})
    assert out["status"] == 409 and out["error"] == "revision_conflict"


def test_register_version_taken_by_other_content_still_conflicts(monkeypatch):
    _stub_remote(monkeypatch, BODY.encode())
    other = dict(_revision_row(), content_hash="0" * 64, id=HEAD_ID)
    conn = FakeConn(head=_head(generation=1), revisions=1, rows=[other, _revision_row("1.0.1")])
    out = _register(conn, {"project": "GO100", "document_key": "doc-a", "kind": "spec",
                           "title": "문서 a", "file_path": PATH, "version": "1.0.0"})
    assert out["status"] == 409 and out["error"] == "revision_conflict"
