"""정본 검색 tenant·문서 권한 격리 (AADS-DOC-SEARCH-TENANT-GUARD-20261003) — fake 전용.

이 파일은 DB 없이 돈다. 여기서 증명하는 것은 "가드가 코드 경로 어디에서도 빠지지 않는다" 와
"SQL 에 격리 조건이 limit 앞에 들어 있다" 까지다. 조건이 **실제 행을 거르는지** 는 격리 DB 를 쓰는
tests/integration/test_doc_search_tenant_guard_postgres.py 가 따로 증명한다 — 둘의 결과를 섞어 보고하지 않는다.
"""
from __future__ import annotations

import asyncio
import re
import sys
import types
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.services import doc_index

ROOT = Path(__file__).resolve().parents[2]
TENANT_A = "00000000-0000-0000-0000-0000000000a1"
TENANT_B = "00000000-0000-0000-0000-0000000000b2"
SCOPE_A = doc_index.DocSearchScope(tenant_id=TENANT_A, user_id="user-a")


class _Pool:
    def __init__(self, rows=None, fetchrow=None):
        self.calls: list[tuple[str, tuple]] = []
        self._rows = rows or []
        self._fetchrow = fetchrow

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        return [dict(r) for r in self._rows]

    async def fetchrow(self, sql, *args):
        self.calls.append((sql, args))
        return self._fetchrow


def _install_pool(monkeypatch, pool):
    mod = types.ModuleType("app.core.db_pool")
    mod.get_pool = lambda: pool
    monkeypatch.setitem(sys.modules, "app.core.db_pool", mod)


def _row(path="/docs/a.md", sim=0.9):
    return {"doc_path": path, "server": "s", "project": "AADS", "title": "t", "heading": "h",
            "content": "c", "doc_sha256": "x", "label": "서버 문서", "mtime": None,
            "indexed_at": None, "similarity": sim}


# ── scope 확정 ───────────────────────────────────────────────────────────

def _ctx(role="member", internal=False, uid="u-1", tenant=TENANT_A):
    return {"user": {"user_id": uid, "is_internal_admin": internal},
            "tenant": {"id": tenant}, "membership": {"role": role}}


def test_scope_from_context_roles():
    assert doc_index.scope_from_tenant_context(_ctx()).elevated is False
    assert doc_index.scope_from_tenant_context(_ctx("viewer")).elevated is False
    assert doc_index.scope_from_tenant_context(_ctx("admin")).elevated is True
    assert doc_index.scope_from_tenant_context(_ctx("owner")).elevated is True
    assert doc_index.scope_from_tenant_context(_ctx(internal=True)).elevated is True


def test_scope_rejects_missing_user_or_bad_tenant():
    with pytest.raises(doc_index.DocScopeError):
        doc_index.scope_from_tenant_context(_ctx(uid=""))
    with pytest.raises(doc_index.DocScopeError):
        doc_index.scope_from_tenant_context(_ctx(tenant="not-a-uuid"))
    with pytest.raises(doc_index.DocScopeError):
        doc_index.scope_from_tenant_context({})
    with pytest.raises(doc_index.DocScopeError):
        doc_index.DocSearchScope(tenant_id="", user_id="u")


def test_scope_matches_canonical_documents_elevation_rule():
    """canonical_documents._scope 와 같은 승격 규칙인지 소스로 대조한다."""
    src = (ROOT / "app/api/canonical_documents.py").read_text(encoding="utf-8")
    assert 'is_internal_admin' in src and '("admin", "owner")' in src.replace("'", '"')


# ── scope 없이는 어느 경로도 DB 를 건드리지 않는다 ───────────────────────

def test_no_scope_denies_every_entry_point_without_touching_db(monkeypatch):
    class Boom:
        def __getattr__(self, name):
            raise AssertionError("DB must not be reached without a scope")

    _install_pool(monkeypatch, Boom())
    vec768, vec1024 = [0.0] * 768, [0.0] * doc_index.QWEN_DIMENSION
    for mode in ("legacy", "shadow", "qwen3", "hybrid"):
        monkeypatch.setattr(doc_index, "_QWEN_MODE", mode)
        assert asyncio.run(doc_index.search_docs(vec768, query_text="q")) == []
    assert asyncio.run(doc_index.search_docs_legacy(vec768)) == []
    assert asyncio.run(doc_index.search_docs_qwen3(vec1024)) == []
    assert asyncio.run(doc_index.index_status()) == {}
    assert asyncio.run(doc_index.visible_chunk_counts(["a"], None)) == {}


# ── 두 임베딩 경로 모두 SQL 에서, LIMIT 앞에서 거른다 ────────────────────

def _where_and_tail(sql: str):
    m = re.search(r"WHERE(.*)(ORDER BY \w\.embedding.*)", sql, re.S)
    assert m, sql
    return m.group(1), m.group(2)


@pytest.mark.parametrize("path", ["legacy", "qwen3"])
def test_both_embedding_paths_filter_in_sql_before_limit(monkeypatch, path):
    pool = _Pool(rows=[_row()])
    _install_pool(monkeypatch, pool)
    if path == "legacy":
        out = asyncio.run(doc_index.search_docs_legacy([0.1] * 8, top_k=3, scope=SCOPE_A))
    else:
        out = asyncio.run(doc_index.search_docs_qwen3(
            [0.1] * doc_index.QWEN_DIMENSION, top_k=3, scope=SCOPE_A))
    assert out and len(pool.calls) == 1
    sql, args = pool.calls[0]
    where, tail = _where_and_tail(sql)
    assert "d.tenant_id = $" in where and "project_document_grants" in where
    assert "project_document_heads" in sql and "h.approved_revision_id" in where
    assert "LIMIT $2" in sql and "LIMIT $2" in tail and "LIMIT $2" not in where
    assert TENANT_A in args and "user-a" in args and False in args
    assert TENANT_B not in args


def test_requester_supplied_tenant_or_uri_is_never_bound(monkeypatch):
    pool = _Pool(rows=[_row()])
    _install_pool(monkeypatch, pool)
    asyncio.run(doc_index.search_docs_legacy(
        [0.1] * 8, top_k=3, project="canonical://AADS/x@r1", scope=SCOPE_A))
    sql, args = pool.calls[0]
    # project 는 바인드 값일 뿐이고, 가시성 조건은 항상 같이 붙는다
    assert "d.tenant_id = $" in sql and TENANT_A in args
    assert "canonical://AADS/x@r1" not in sql


def test_visibility_predicate_is_fail_closed_shape():
    sql = doc_index._visible_sql(1, 2, 3)
    # 정본 분기: tenant 일치 + 가상 경로 + head/revision 표지 + 실시간 head 일치 + grant
    for needle in ("d.tenant_id = $1::uuid", "d.canonical_head_id IS NOT NULL",
                   "d.canonical_revision_id IS NOT NULL", "h.id IS NOT NULL",
                   "project_document_grants", "'archived'"):
        assert needle in sql
    # 파일 분기: tenant/정본 표지가 전혀 없는 청크만
    assert "d.tenant_id IS NULL AND d.canonical_head_id IS NULL" in sql
    assert "d.doc_path NOT LIKE 'canonical://%'" in sql
    assert "d.label IS DISTINCT FROM '정본'" in sql


def test_all_chunk_reads_use_visibility_predicate():
    import inspect

    for fn in (doc_index.search_docs_qwen3, doc_index.search_docs_legacy,
               doc_index.index_status, doc_index.visible_chunk_counts):
        assert "_visible_sql(" in inspect.getsource(fn), fn.__name__
    src = (ROOT / "app/services/doc_index.py").read_text(encoding="utf-8")
    assert src.count("_visible_sql(") == 5  # 정의 1 + 사용 4


def test_index_status_and_counts_are_scoped(monkeypatch):
    row = {"chunks": 2, "embedded": 1, "docs": 1, "servers": 1, "last_indexed": None}
    pool = _Pool(rows=[{"doc_path": "/docs/a.md", "chunks": 2}], fetchrow=row)
    _install_pool(monkeypatch, pool)
    status = asyncio.run(doc_index.index_status(SCOPE_A))
    assert status["chunks"] == 2
    counts = asyncio.run(doc_index.visible_chunk_counts(["/docs/a.md"], SCOPE_A))
    assert counts == {"/docs/a.md": 2}
    for sql, args in pool.calls:
        assert "d.tenant_id = $" in sql and TENANT_A in args


# ── Auto-RAG 범위는 영속된 세션에서만 ────────────────────────────────────

def test_auto_rag_scope_comes_from_persisted_session_only(monkeypatch):
    from app.services import auto_rag

    pool = _Pool(fetchrow={"tenant_id": TENANT_A, "user_id": "user-a"})
    _install_pool(monkeypatch, pool)
    scope = asyncio.run(auto_rag._resolve_doc_scope("00000000-0000-0000-0000-00000000cafe"))
    assert scope == doc_index.DocSearchScope(tenant_id=TENANT_A, user_id="user-a", elevated=False)
    assert "FROM chat_sessions" in pool.calls[0][0]


@pytest.mark.parametrize("session_row", [None, {"tenant_id": None, "user_id": "u"},
                                         {"tenant_id": "garbage", "user_id": "u"}])
def test_auto_rag_unresolvable_session_searches_nothing(monkeypatch, session_row):
    from app.services import auto_rag

    _install_pool(monkeypatch, _Pool(fetchrow=session_row))

    async def must_not_search(*a, **k):
        raise AssertionError("search must not run without a scope")

    monkeypatch.setattr("app.services.doc_index.search_docs", must_not_search)
    assert asyncio.run(auto_rag._search_documents([0.1] * 8, "AADS", "q", "sess")) == []
    assert asyncio.run(auto_rag._search_documents([0.1] * 8, "AADS", "q", "")) == []


def test_auto_rag_session_without_user_gets_files_only_scope(monkeypatch):
    from app.services import auto_rag

    _install_pool(monkeypatch, _Pool(fetchrow={"tenant_id": TENANT_A, "user_id": None}))
    scope = asyncio.run(auto_rag._resolve_doc_scope("sess"))
    assert scope is not None and scope.user_id == "" and scope.elevated is False


# ── API: 인증 필수 · scope 전달 ──────────────────────────────────────────

def _client(monkeypatch, *, context=None):
    from app.api import project_docs

    app = FastAPI()
    app.include_router(project_docs.router, prefix="/api/v1")
    if context is not None:
        app.dependency_overrides[project_docs.require_tenant_member] = lambda: context
    return TestClient(app, raise_server_exceptions=False)


def test_search_endpoint_rejects_unauthenticated():
    from app.api import project_docs

    app = FastAPI()
    app.include_router(project_docs.router, prefix="/api/v1")
    res = TestClient(app, raise_server_exceptions=False).get("/api/v1/project-docs/search?q=hello")
    assert res.status_code in (401, 403)


def test_search_endpoint_requires_tenant_member_dependency():
    from app.api import project_docs

    route = next(r for r in project_docs.router.routes if getattr(r, "path", "") == "/project-docs/search")
    deps = [d.call for d in route.dependant.dependencies]
    assert project_docs.require_tenant_member in deps


def test_search_endpoint_passes_scope_and_ignores_query_tenant(monkeypatch):
    seen = {}

    async def fake_embed(q):
        return [0.0] * 768

    async def fake_status(scope=None):
        seen["status_scope"] = scope
        return {"docs": 1, "chunks": 1, "embedded": 1, "coverage": 100.0}

    async def fake_search(vec, *, top_k=5, project=None, query_text=None, scope=None):
        seen["scope"] = scope
        return [_row("/docs/a.md")]

    async def fake_counts(paths, scope):
        seen["counts_scope"] = scope
        return {"/docs/a.md": 3}

    monkeypatch.setattr("app.services.doc_index.embed_query", fake_embed)
    monkeypatch.setattr("app.services.doc_index.index_status", fake_status)
    monkeypatch.setattr("app.services.doc_index.search_docs", fake_search)
    monkeypatch.setattr("app.services.doc_index.visible_chunk_counts", fake_counts)
    client = _client(monkeypatch, context=_ctx(tenant=TENANT_A, uid="user-a"))
    res = client.get(f"/api/v1/project-docs/search?q=hello&tenant_id={TENANT_B}&x_tenant={TENANT_B}",
                     headers={"X-Tenant-ID": TENANT_B})
    assert res.status_code == 200, res.text
    expected = doc_index.DocSearchScope(tenant_id=TENANT_A, user_id="user-a")
    assert seen["scope"] == seen["status_scope"] == seen["counts_scope"] == expected
    body = res.json()
    assert body["results"][0]["chunks"] == 3
    # 응답 계약은 그대로다 — 필드 추가/삭제가 없다.
    assert set(body["results"][0]) == {"path", "name", "project", "server", "title", "heading",
                                      "snippet", "similarity", "chunks"}
    assert set(body) == {"query", "count", "results", "index"}


def test_search_endpoint_user_identity_missing_is_403(monkeypatch):
    client = _client(monkeypatch, context=_ctx(uid=""))
    res = client.get("/api/v1/project-docs/search?q=hello")
    assert res.status_code == 403


# ── 정본이 새는 다른 경로: KG 빌더 ───────────────────────────────────────

def test_kg_builder_excludes_canonical_chunks():
    src = (ROOT / "scripts/build_kg.py").read_text(encoding="utf-8")
    assert "doc_path NOT LIKE 'canonical://%'" in src
    assert "coalesce(label,'') <> '정본'" in src
