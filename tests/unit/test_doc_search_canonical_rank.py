"""정본 본문 검색 상위 k 보장 (AADS-CANONICAL-SEARCH-RANK-F1-20261014) — fake 전용.

DB 없이 돈다. 여기서 증명하는 것은 병합 규칙과 "정본 전용 조회도 같은 scope·같은 가시성 SQL 뒤에
붙는다" 까지다. 권한 술어가 실제 행을 거르는지는 tests/integration/test_doc_search_tenant_guard_postgres.py 몫이다.
"""
from __future__ import annotations

import asyncio
import sys
import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import project_docs
from app.services import doc_index

TENANT_A = "00000000-0000-0000-0000-0000000000a1"
TENANT_B = "00000000-0000-0000-0000-0000000000b2"
GRANTED = "user-granted"
STRANGER = "user-stranger"


def _row(path, sim, title="같은 제목"):
    return {"doc_path": path, "server": "s", "project": "AADS", "title": title, "heading": "h",
            "content": "c", "doc_sha256": "x", "label": "", "mtime": None,
            "indexed_at": None, "similarity": sim}


def _canon(n, sim):
    return _row(f"canonical://AADS/doc-{n}@r1", sim)


def _legacy(n, sim):
    return _row(f"/root/aads/docs/legacy-{n}.md", sim)


def _ctx(uid, tenant=TENANT_A):
    return {"user": {"user_id": uid, "is_internal_admin": False},
            "tenant": {"id": tenant}, "membership": {"role": "member"}}


def _install(monkeypatch, corpus):
    """corpus: [(row, tenant 또는 None, 열람 가능한 user 집합 또는 None)] — 가시성은 fake 가 흉내 낸다."""
    calls = []

    async def fake_search(vec, *, top_k=5, project=None, query_text=None, scope=None,
                          canonical_only=False):
        calls.append({"top_k": top_k, "scope": scope, "canonical_only": canonical_only})
        if scope is None:
            return []
        out = []
        for row, tenant, users in corpus:
            is_canon = row["doc_path"].startswith("canonical://")
            if is_canon and not (tenant == scope.tenant_id and scope.user_id in (users or ())):
                continue
            if canonical_only and not is_canon:
                continue
            out.append(dict(row))
        out.sort(key=lambda r: -r["similarity"])
        return out[:top_k]

    async def fake_embed(q):
        return [0.0] * 8

    async def fake_status(scope=None):
        return {"docs": 1, "chunks": 1, "embedded": 1, "coverage": 100.0}

    async def fake_counts(paths, scope):
        return {p: 1 for p in paths}

    monkeypatch.setattr("app.services.doc_index.search_docs", fake_search)
    monkeypatch.setattr("app.services.doc_index.embed_query", fake_embed)
    monkeypatch.setattr("app.services.doc_index.index_status", fake_status)
    monkeypatch.setattr("app.services.doc_index.visible_chunk_counts", fake_counts)
    return calls


def _get(ctx, limit=5, q="같은 제목 질의"):
    app = FastAPI()
    app.include_router(project_docs.router, prefix="/api/v1")
    app.dependency_overrides[project_docs.require_tenant_member] = lambda: ctx
    res = TestClient(app, raise_server_exceptions=False).get(
        f"/api/v1/project-docs/search?q={q}&limit={limit}")
    assert res.status_code == 200, res.text
    return res.json()


def _paths(body):
    return [r["path"] for r in body["results"]]


def test_canonical_top_k_included_and_first_even_when_legacy_scores_higher(monkeypatch):
    legacy = [(_legacy(i, 0.95 - i * 0.01), None, None) for i in range(8)]
    canon = [(_canon(i, 0.60 - i * 0.01), TENANT_A, {GRANTED}) for i in range(4)]
    _install(monkeypatch, legacy + canon)
    body = _get(_ctx(GRANTED), limit=5)
    paths = _paths(body)
    k = project_docs.CANONICAL_TOP_K
    assert k == 3
    assert paths[:k] == [f"canonical://AADS/doc-{i}@r1" for i in range(k)]
    assert "canonical://AADS/doc-3@r1" not in paths  # k 를 넘는 정본은 보장 대상이 아니다
    assert paths[k:] == [f"/root/aads/docs/legacy-{i}.md" for i in range(5 - k)]
    assert body["count"] == 5


def test_canonical_guarantee_is_capped_by_limit(monkeypatch):
    corpus = [(_legacy(0, 0.9), None, None)] + [
        (_canon(i, 0.5), TENANT_A, {GRANTED}) for i in range(3)]
    _install(monkeypatch, corpus)
    paths = _paths(_get(_ctx(GRANTED), limit=2))
    assert paths == ["canonical://AADS/doc-0@r1", "canonical://AADS/doc-1@r1"]


def test_user_without_grant_or_other_tenant_gets_zero_canonical(monkeypatch):
    corpus = [(_legacy(i, 0.9 - i * 0.01), None, None) for i in range(4)] + [
        (_canon(i, 0.99), TENANT_A, {GRANTED}) for i in range(3)]
    calls = _install(monkeypatch, corpus)
    for ctx in (_ctx(STRANGER), _ctx(GRANTED, tenant=TENANT_B)):
        paths = _paths(_get(ctx, limit=5))
        assert not any(p.startswith("canonical://") for p in paths)
        assert paths == [f"/root/aads/docs/legacy-{i}.md" for i in range(4)]
    # 정본 전용 조회도 항상 인증이 푼 scope 로만 나갔다
    assert calls and all(c["scope"] is not None for c in calls)
    assert {c["scope"].tenant_id for c in calls} <= {TENANT_A, TENANT_B}


def test_canonical_pass_uses_same_scope_as_main_search(monkeypatch):
    calls = _install(monkeypatch, [(_legacy(0, 0.9), None, None)])
    _get(_ctx(GRANTED))
    assert [c["canonical_only"] for c in calls] == [False, True]
    assert calls[0]["scope"] == calls[1]["scope"]
    assert calls[1]["top_k"] >= project_docs.CANONICAL_TOP_K


def test_query_without_canonical_matches_previous_result_order(monkeypatch):
    corpus = [(_legacy(i, 0.9 - i * 0.05), None, None) for i in range(6)]
    # 같은 문서의 낮은 조각은 높은 조각 하나로 접힌다(기존 동작)
    corpus.append((_legacy(0, 0.1), None, None))
    _install(monkeypatch, corpus)
    body = _get(_ctx(GRANTED), limit=4)
    assert _paths(body) == [f"/root/aads/docs/legacy-{i}.md" for i in range(4)]
    assert body["count"] == 4


def test_canonical_pass_failure_degrades_to_previous_behavior(monkeypatch):
    corpus = [(_legacy(0, 0.9), None, None), (_canon(0, 0.5), TENANT_A, {GRANTED})]
    _install(monkeypatch, corpus)
    real = doc_index.search_docs

    async def flaky(*a, canonical_only=False, **k):
        if canonical_only:
            raise RuntimeError("boom")
        return await real(*a, canonical_only=canonical_only, **k)

    monkeypatch.setattr("app.services.doc_index.search_docs", flaky)
    assert _paths(_get(_ctx(GRANTED))) == [
        "canonical://AADS/doc-0@r1", "/root/aads/docs/legacy-0.md"]


def test_non_canonical_rows_from_canonical_pass_are_ignored(monkeypatch):
    async def leaky(vec, *, canonical_only=False, **k):
        return [_legacy(9, 0.99)] if canonical_only else [_legacy(0, 0.5)]

    _install(monkeypatch, [])
    monkeypatch.setattr("app.services.doc_index.search_docs", leaky)
    assert _paths(_get(_ctx(GRANTED))) == ["/root/aads/docs/legacy-0.md"]


# ── SQL: 정본 전용 조회는 가시성 술어를 우회하지 않고 그 뒤에 좁히기만 한다 ──

class _Pool:
    def __init__(self):
        self.calls = []

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        return []


@pytest.mark.parametrize("path", ["legacy", "qwen3"])
@pytest.mark.parametrize("canonical_only", [False, True])
def test_canonical_only_narrows_after_visibility_predicate(monkeypatch, path, canonical_only):
    pool = _Pool()
    mod = types.ModuleType("app.core.db_pool")
    mod.get_pool = lambda: pool
    monkeypatch.setitem(sys.modules, "app.core.db_pool", mod)
    scope = doc_index.DocSearchScope(tenant_id=TENANT_A, user_id=GRANTED)
    if path == "legacy":
        asyncio.run(doc_index.search_docs_legacy(
            [0.1] * 8, top_k=3, scope=scope, canonical_only=canonical_only))
    else:
        asyncio.run(doc_index.search_docs_qwen3(
            [0.1] * doc_index.QWEN_DIMENSION, top_k=3, scope=scope, canonical_only=canonical_only))
    sql, args = pool.calls[0]
    visible_end = sql.index("project_document_grants")
    assert "d.tenant_id = $" in sql and TENANT_A in args and GRANTED in args
    marker = "AND d.doc_path LIKE 'canonical://%'\n"
    if canonical_only:
        assert sql.rindex(marker) > visible_end
        assert sql.rindex("ORDER BY") > sql.rindex(marker)
    else:
        assert marker not in sql


def test_search_docs_without_scope_still_denies_canonical_only(monkeypatch):
    class Boom:
        def __getattr__(self, name):
            raise AssertionError("DB must not be reached without a scope")

    mod = types.ModuleType("app.core.db_pool")
    mod.get_pool = lambda: Boom()
    monkeypatch.setitem(sys.modules, "app.core.db_pool", mod)
    assert asyncio.run(doc_index.search_docs([0.0] * 8, query_text="q", canonical_only=True)) == []
