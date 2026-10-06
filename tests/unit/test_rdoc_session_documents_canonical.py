"""세션 문서 목록의 정본 등록 현황·R-DOC 판정(테넌트 고정, 가짜 풀)."""
from __future__ import annotations

import datetime as dt
import uuid

import pytest

from app.services import session_documents as sd

SID = uuid.UUID("44444444-4444-4444-4444-444444444444")
NEW_DOC = "reports/20261006_AADS_문서 저장 결과.md"
NOW = dt.datetime(2026, 10, 6, 12, 0, 0)


class FakePool:
    def __init__(self, ledger=(), canonical=()):
        self.ledger, self.canonical = list(ledger), list(canonical)
        self.canonical_args = None

    async def fetch(self, sql, *args):
        if "chat_workspace_change_ledger" in sql:
            return self.ledger
        if "project_document_revisions" in sql:
            self.canonical_args = args
            assert "chat_sessions s WHERE s.id = $1" in sql  # 세션 테넌트로 고정
            return self.canonical
        return []


def _ledger(path):
    return {"repo": "", "file_path": path, "source_tool": "write_remote_file", "at": NOW}


def _canon(path, approved=False):
    return {"project_key": "AADS", "document_key": "rdoc-result", "revision": 2, "title": "문서 저장 결과",
            "source_path": path, "at": NOW, "approved": approved}


@pytest.fixture
def use_pool(monkeypatch):
    def _set(pool):
        monkeypatch.setattr("app.core.db_pool.get_pool", lambda: pool)
        return pool

    return _set


@pytest.mark.asyncio
async def test_unregistered_new_doc_is_marked_incomplete(use_pool):
    use_pool(FakePool(ledger=[_ledger(f"/root/aads/aads-server/{NEW_DOC}")]))
    res = await sd.collect_session_documents(SID)
    assert res["rdoc"]["status"] == "incomplete"
    assert res["rdoc"]["marker"] == "정본 미등록(미완료)"
    assert res["canonical_documents"] == []


@pytest.mark.asyncio
async def test_registered_doc_is_complete_and_has_view_ref(use_pool):
    pool = use_pool(FakePool(ledger=[_ledger(f"/root/aads/aads-server/{NEW_DOC}")], canonical=[_canon(NEW_DOC)]))
    res = await sd.collect_session_documents(SID)
    assert res["rdoc"]["status"] == "complete"
    assert "canonical" in res["sources_used"]
    ref = res["canonical_documents"][0]["view"]
    assert ref["status"] == "draft" and ref["authoritative"] is False
    assert ref["api_path"].endswith("/documents/rdoc-result/content?revision=2")
    # 쿼리는 세션 id 와 문서 상대 경로 목록만 받는다 — 테넌트는 SQL 서브쿼리가 정한다.
    assert pool.canonical_args[0] == SID or str(pool.canonical_args[0]) == str(SID)
    assert NEW_DOC in pool.canonical_args[1]


@pytest.mark.asyncio
async def test_code_only_session_is_not_applicable(use_pool):
    use_pool(FakePool(ledger=[_ledger("/root/aads/aads-server/app/x.py")]))
    res = await sd.collect_session_documents(SID)
    assert res["rdoc"]["status"] == "not_applicable"


@pytest.mark.asyncio
async def test_canonical_source_failure_is_fail_open(use_pool):
    class Boom(FakePool):
        async def fetch(self, sql, *args):
            if "project_document_revisions" in sql:
                raise RuntimeError("db")
            return await super().fetch(sql, *args)

    use_pool(Boom(ledger=[_ledger(f"/root/aads/aads-server/{NEW_DOC}")]))
    res = await sd.collect_session_documents(SID)
    assert res["canonical_documents"] == [] and res["documents"]
