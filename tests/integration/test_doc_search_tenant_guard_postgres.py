"""정본 검색 tenant·권한 격리 — **격리 PostgreSQL(pgvector)** 로 실제 행을 거르는지 증명한다.

AADS-DOC-SEARCH-TENANT-GUARD-20261003. fake 전용 단위 테스트(tests/unit/test_doc_search_tenant_guard.py)와
결과를 따로 보고한다 — 여기서만 "SQL 이 진짜로 다른 tenant 청크를 top_k 앞에서 걸러낸다" 를 본다.

환경: AADS_DOC_GUARD_TEST_ADMIN_DSN (예: postgresql://postgres@127.0.0.1:55439/postgres).
  - 호스트는 127.0.0.1 로 고정하고(55439 = 호스트에서 본 격리 컨테이너, 5432 = 그 컨테이너의
    네트워크를 공유한 테스트 컨테이너에서 본 같은 서버), 서버에 템플릿 DB 가 있을 때만 쓴다.
  - 템플릿 DB aads_doc_m1_final 을 복제한 임시 DB 를 만들고 마지막에 지운다.
  - 설정이 없거나 닿지 않으면 skip(AADS_DOC_GUARD_DB_REQUIRED=1 이면 실패).
"""
from __future__ import annotations

import asyncio
import math
import os
import socket
import sys
import types
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import asyncpg
import pytest

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_DB = "aads_doc_m1_final"
INTERNAL = "00000000-0000-0000-0000-000000000001"
TENANT_B = "00000000-0000-0000-0000-0000000000b2"
MIGRATION = ROOT / "migrations" / "20261003_doc_chunks_tenant_scope.sql"
ROLLBACK = ROOT / "migrations" / "rollback" / "20261003_doc_chunks_tenant_scope.down.sql"

pytestmark = pytest.mark.asyncio


def _skip(reason: str):
    if os.getenv("AADS_DOC_GUARD_DB_REQUIRED") == "1":
        pytest.fail(reason)
    pytest.skip(reason)


def _admin_dsn() -> str:
    dsn = os.getenv("AADS_DOC_GUARD_TEST_ADMIN_DSN")
    if not dsn:
        _skip("AADS_DOC_GUARD_TEST_ADMIN_DSN unset — isolated DB integration not run")
    parts = urlsplit(dsn)
    assert parts.hostname == "127.0.0.1" and parts.port in (55439, 5432), "must target the isolated test DB only"
    try:
        with socket.create_connection(("127.0.0.1", parts.port), timeout=2):
            pass
    except OSError as exc:
        _skip(f"isolated DB unreachable: {exc}")
    return dsn


def _with_db(dsn: str, name: str) -> str:
    parts = urlsplit(dsn)
    return parts._replace(path=f"/{name}").geturl()


@pytest.fixture
async def db(monkeypatch):
    admin_dsn = _admin_dsn()
    name = f"aads_doc_guard_{uuid4().hex[:10]}"
    admin = await asyncpg.connect(admin_dsn, timeout=5)
    if not await admin.fetchval("SELECT count(*) FROM pg_database WHERE datname = $1", TEMPLATE_DB):
        await admin.close()
        _skip(f"{TEMPLATE_DB} not on this server — refusing to run against a non-isolated DB")
    try:
        await admin.execute(f'CREATE DATABASE "{name}" TEMPLATE "{TEMPLATE_DB}"')
    except asyncpg.PostgresError as exc:
        await admin.close()
        _skip(f"cannot clone template {TEMPLATE_DB}: {exc}")
    pool = await asyncpg.create_pool(_with_db(admin_dsn, name), min_size=1, max_size=4)
    _install_pool(monkeypatch, pool)
    try:
        yield pool
    finally:
        await pool.close()
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()


# ── 벡터·시드 도우미 ─────────────────────────────────────────────────────

def _vec(dim: int, sim: float) -> str:
    """질문 벡터 e0 와 코사인 유사도가 정확히 sim 인 단위 벡터(문자열)."""
    v = [0.0] * dim
    v[0] = sim
    v[1] = math.sqrt(max(0.0, 1.0 - sim * sim))
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


def _query(dim: int) -> list[float]:
    v = [0.0] * dim
    v[0] = 1.0
    return v


async def _apply_migration(pool) -> None:
    await pool.execute(MIGRATION.read_text(encoding="utf-8"))


async def _tenant(pool, tenant_id: str, slug: str) -> None:
    await pool.execute(
        "INSERT INTO tenants (id, slug, name) VALUES ($1::uuid, $2, $2) ON CONFLICT DO NOTHING",
        tenant_id, slug,
    )


async def _head(pool, tenant: str, project: str, key: str, *, approved=None, latest=None,
                archived=False) -> dict:
    """head 와 revision 들을 만든다. approved/latest 는 revision 번호(1..)."""
    head_id = await pool.fetchval(
        "INSERT INTO project_document_heads (tenant_id, project_key, document_key, kind, title) "
        "VALUES ($1::uuid,$2,$3,'plan',$3) RETURNING id::text", tenant, project, key)
    revs: dict[int, str] = {}
    for n in sorted({x for x in (approved, latest) if x}):
        content = f"{key} 본문 revision {n} " * 20
        revs[n] = await pool.fetchval(
            "INSERT INTO project_document_revisions (head_id,tenant_id,project_key,revision,version,"
            "title,content,content_hash,source_kind,author_id) VALUES ($1::uuid,$2::uuid,$3,$4,$5,$6,$7,"
            "encode(digest(convert_to($7,'UTF8'),'sha256'),'hex'),'api','tester') RETURNING id::text",
            head_id, tenant, project, n, f"1.{n}.0", f"{key} 제목", content)
    await pool.execute(
        "UPDATE project_document_heads SET approved_revision_id=$2::uuid, latest_revision_id=$3::uuid WHERE id=$1::uuid",
        head_id, revs.get(approved) if approved else None, revs.get(latest) if latest else None)
    if archived:
        await pool.execute(
            "INSERT INTO project_document_events (tenant_id,project_key,head_id,action,actor_id) "
            "VALUES ($1::uuid,$2,$3::uuid,'archived','tester')", tenant, project, head_id)
    return {"head_id": head_id, "revs": revs, "tenant": tenant, "project": project, "key": key}


async def _grant(pool, tenant: str, project: str, user: str, access: str = "read") -> None:
    await pool.execute(
        "INSERT INTO project_document_grants (tenant_id,project_key,user_id,access) VALUES ($1::uuid,$2,$3,$4)",
        tenant, project, user, access)


async def _chunk(pool, path: str, *, project="AADS", label="", tenant=None, head=None, revision=None,
                 sim=0.8, legacy=True, qwen=True, idx=0) -> str:
    from app.services import doc_index

    cid = await pool.fetchval(
        "INSERT INTO doc_chunks (server,doc_path,doc_sha256,project,label,title,heading,chunk_index,content,"
        "embedding,tenant_id,canonical_head_id,canonical_revision_id) VALUES ('srv',$1,$2,$3,$4,$5,'h',$6,$7,"
        "$8::vector,$9::uuid,$10::uuid,$11::uuid) RETURNING id::text",
        path, "f" * 64, project, label, f"title {path}", idx, f"본문 {path}",
        _vec(768, sim) if legacy else None, tenant, head, revision)
    if qwen:
        await pool.execute(
            "INSERT INTO doc_chunk_embeddings_qwen3 (chunk_id,model_id,dimension,instruction_version,"
            "content_sha256,embedding,state) VALUES ($1::uuid,$2,1024,$3,$4,$5::vector,'ready')",
            cid, doc_index.QWEN_MODEL_ID, doc_index.QWEN_INSTRUCTION_VERSION, "a" * 64, _vec(1024, sim))
    return cid


async def _canon(pool, h: dict, rev_no: int, *, sim=0.8, tenant=None, **kw) -> str:
    path = f"canonical://{h['project']}/{h['key']}@{h['revs'][rev_no]}"
    return await _chunk(pool, path, project=h["project"], label="정본",
                        tenant=h["tenant"] if tenant is None else tenant,
                        head=h["head_id"], revision=h["revs"][rev_no], sim=sim, **kw)


def _install_pool(monkeypatch, pool):
    mod = types.ModuleType("app.core.db_pool")
    mod.get_pool = lambda: pool
    monkeypatch.setitem(sys.modules, "app.core.db_pool", mod)


def _scope(tenant: str, user: str, elevated: bool = False):
    from app.services import doc_index

    return doc_index.DocSearchScope(tenant_id=tenant, user_id=user, elevated=elevated)


async def _paths(scope, *, path: str, top_k=10, project=None) -> set[str]:
    from app.services import doc_index

    if path == "legacy":
        rows = await doc_index.search_docs_legacy(_query(768), top_k=top_k, project=project, scope=scope)
    else:
        rows = await doc_index.search_docs_qwen3(_query(1024), top_k=top_k, project=project, scope=scope)
    return {r["doc_path"] for r in rows}


async def _world(pool):
    """tenant A(내부)·B 의 정본/파일 문서가 섞인 세계."""
    await _apply_migration(pool)
    await _tenant(pool, TENANT_B, "guard-b")
    a_ok = await _head(pool, INTERNAL, "AADS", "a-approved", approved=1, latest=1)
    a_draft = await _head(pool, INTERNAL, "AADS", "a-draft", latest=1)
    a_both = await _head(pool, INTERNAL, "AADS", "a-both", approved=1, latest=2)
    a_arch = await _head(pool, INTERNAL, "AADS", "a-archived", latest=1, archived=True)
    a_other = await _head(pool, INTERNAL, "KIS", "a-kis", approved=1, latest=1)
    b_doc = await _head(pool, TENANT_B, "AADS", "b-secret", approved=1, latest=1)
    ids = {
        "a_ok": await _canon(pool, a_ok, 1),
        "a_draft": await _canon(pool, a_draft, 1),
        "a_both_approved": await _canon(pool, a_both, 1),
        "a_both_draft": await _canon(pool, a_both, 2),
        "a_arch": await _canon(pool, a_arch, 1),
        "a_kis": await _canon(pool, a_other, 1),
        "b_doc": await _canon(pool, b_doc, 1, sim=0.99),
        "file": await _chunk(pool, "/root/aads/docs/file.md", label="서버 문서(contabo116)", sim=0.7),
    }
    await _grant(pool, INTERNAL, "AADS", "granted-user")
    await _grant(pool, INTERNAL, "KIS", "kis-user", "write")
    await _grant(pool, TENANT_B, "AADS", "b-user")
    return {"a_ok": a_ok, "a_draft": a_draft, "a_both": a_both, "a_arch": a_arch, "a_kis": a_other,
            "b_doc": b_doc, "ids": ids}


def _p(h: dict, n: int) -> str:
    return f"canonical://{h['project']}/{h['key']}@{h['revs'][n]}"


PATHS = ("legacy", "qwen3")


def _psql_bridge(db, loop):
    """index_docs.psql 대역 — 같은 SQL 을 격리 DB 에 asyncpg 로 실행하고 psql -At 모양으로 돌려준다."""
    def cell(v):
        if v is None:
            return ""
        if isinstance(v, bool):
            return "t" if v else "f"
        return str(v)

    def psql(sql, *, quiet=False):
        async def run():
            if sql.lstrip().upper().startswith("SELECT"):
                rows = await db.fetch(sql)
                return "".join("\x1f".join(cell(v) for v in r.values()) + "\n" for r in rows)
            await db.execute(sql)
            return ""
        return asyncio.run_coroutine_threadsafe(run(), loop).result(timeout=30)

    return psql


# ── 1. migration ─────────────────────────────────────────────────────────

async def test_migration_is_idempotent_and_rollback_restores_schema(db):
    cols = "tenant_id,canonical_head_id,canonical_revision_id"

    async def have():
        return await db.fetchval(
            "SELECT count(*) FROM information_schema.columns WHERE table_name='doc_chunks' "
            "AND column_name = ANY(string_to_array($1, ','))", cols)

    assert await have() == 0
    await _apply_migration(db)
    await _apply_migration(db)  # 두 번째는 no-op
    assert await have() == 3
    await _chunk(db, "/docs/legacy.md", sim=0.7)
    await db.execute(ROLLBACK.read_text(encoding="utf-8"))
    assert await have() == 0
    assert await db.fetchval("SELECT count(*) FROM doc_chunks WHERE doc_path='/docs/legacy.md'") == 1
    await _apply_migration(db)
    assert await have() == 3


# ── 2. tenant A/B 같은 질의 ──────────────────────────────────────────────

@pytest.mark.parametrize("path", PATHS)
async def test_same_query_tenant_a_sees_own_and_b_sees_only_own(db, monkeypatch, path):
    w = await _world(db)
    a = await _paths(_scope(INTERNAL, "granted-user"), path=path)
    assert a == {_p(w["a_ok"], 1), _p(w["a_draft"], 1), _p(w["a_both"], 1), _p(w["a_both"], 2),
                 "/root/aads/docs/file.md"}
    assert _p(w["b_doc"], 1) not in a
    assert _p(w["a_kis"], 1) not in a  # 다른 프로젝트 grant 없음
    assert _p(w["a_arch"], 1) not in a  # 보관됨
    b = await _paths(_scope(TENANT_B, "b-user"), path=path)
    assert b == {_p(w["b_doc"], 1), "/root/aads/docs/file.md"}
    assert not any(p.startswith("canonical://") and "/a-" in p for p in b)


@pytest.mark.parametrize("path", PATHS)
async def test_user_without_project_grant_sees_files_only(db, monkeypatch, path):
    await _world(db)
    got = await _paths(_scope(INTERNAL, "nobody"), path=path)
    assert got == {"/root/aads/docs/file.md"}
    assert await _paths(_scope(INTERNAL, "kis-user"), path=path, project="AADS") == {"/root/aads/docs/file.md"}


@pytest.mark.parametrize("path", PATHS)
async def test_project_grant_is_per_project(db, monkeypatch, path):
    w = await _world(db)
    got = await _paths(_scope(INTERNAL, "kis-user"), path=path)
    assert got == {_p(w["a_kis"], 1), "/root/aads/docs/file.md"}


@pytest.mark.parametrize("path", PATHS)
async def test_elevated_admin_sees_all_own_tenant_active_but_never_other_tenant(db, monkeypatch, path):
    w = await _world(db)
    got = await _paths(_scope(INTERNAL, "boss", elevated=True), path=path)
    assert _p(w["a_kis"], 1) in got and _p(w["a_draft"], 1) in got
    assert _p(w["b_doc"], 1) not in got and _p(w["a_arch"], 1) not in got


@pytest.mark.parametrize("path", PATHS)
async def test_elevated_without_user_identity_is_not_elevated(db, monkeypatch, path):
    w = await _world(db)
    got = await _paths(_scope(INTERNAL, "", elevated=True), path=path)
    assert got == {"/root/aads/docs/file.md"}
    assert _p(w["a_ok"], 1) not in got


# ── 3. fail-closed ───────────────────────────────────────────────────────

@pytest.mark.parametrize("path", PATHS)
async def test_canonical_chunks_with_unknown_or_mismatched_owner_are_invisible(db, monkeypatch, path):
    w = await _world(db)
    ok = w["a_ok"]
    bad_null_tenant = await _chunk(db, "canonical://AADS/bad-null@r1", label="정본", head=ok["head_id"],
                                   revision=ok["revs"][1], sim=0.95)
    bad_null_head = await _chunk(db, "canonical://AADS/bad-head@r1", label="정본", tenant=INTERNAL,
                                 revision=ok["revs"][1], sim=0.95)
    wrong_tenant_for_head = await _chunk(db, "canonical://AADS/bad-xt@r1", label="정본", tenant=TENANT_B,
                                         head=ok["head_id"], revision=ok["revs"][1], sim=0.95)
    legacy_unlabeled = await _chunk(db, "canonical://AADS/bad-nolabel@r1", label="", sim=0.95)
    labeled_only = await _chunk(db, "/docs/spoof.md", label="정본", sim=0.95)
    assert all([bad_null_tenant, bad_null_head, wrong_tenant_for_head, legacy_unlabeled, labeled_only])
    for scope in (_scope(INTERNAL, "granted-user"), _scope(INTERNAL, "boss", True), _scope(TENANT_B, "b-user")):
        got = await _paths(scope, path=path)
        assert not [p for p in got if "/bad-" in p or p == "/docs/spoof.md"], got


@pytest.mark.parametrize("path", PATHS)
async def test_stale_revision_disappears_without_reindex(db, monkeypatch, path):
    w = await _world(db)
    scope = _scope(INTERNAL, "granted-user")
    assert _p(w["a_both"], 1) in await _paths(scope, path=path)
    # 새 revision 3 이 최신이 되고 승인도 3 으로 옮겨가면 1·2 는 더 이상 head 가 가리키지 않는다.
    h = w["a_both"]
    r3 = await db.fetchval(
        "INSERT INTO project_document_revisions (head_id,tenant_id,project_key,revision,version,title,content,"
        "content_hash,source_kind,author_id) VALUES ($1::uuid,$2::uuid,'AADS',3,'1.3.0','t','rev three body',"
        "encode(digest(convert_to('rev three body','UTF8'),'sha256'),'hex'),'api','x') RETURNING id::text",
        h["head_id"], INTERNAL)
    await db.execute("UPDATE project_document_heads SET approved_revision_id=$2::uuid, latest_revision_id=$2::uuid "
                     "WHERE id=$1::uuid", h["head_id"], r3)
    got = await _paths(scope, path=path)
    assert _p(w["a_both"], 1) not in got and _p(w["a_both"], 2) not in got


@pytest.mark.parametrize("path", PATHS)
async def test_grant_revocation_applies_immediately(db, monkeypatch, path):
    w = await _world(db)
    scope = _scope(INTERNAL, "granted-user")
    assert _p(w["a_ok"], 1) in await _paths(scope, path=path)
    await db.execute("DELETE FROM project_document_grants WHERE user_id='granted-user'")
    assert _p(w["a_ok"], 1) not in await _paths(scope, path=path)


# ── 4. LIMIT 앞에서 거른다 ───────────────────────────────────────────────

@pytest.mark.parametrize("path", PATHS)
async def test_filter_happens_before_limit(db, monkeypatch, path):
    w = await _world(db)
    # 질문과 완전히 같은 방향(유사도 1.0)인 숨김 청크를 top_k 보다 훨씬 많이 쌓는다.
    hidden = await _head(db, TENANT_B, "AADS", "b-flood", approved=1, latest=1)
    for i in range(30):
        await _chunk(db, f"canonical://AADS/b-flood@{hidden['revs'][1]}#{i}", label="정본", project="AADS",
                     tenant=TENANT_B, head=hidden["head_id"], revision=hidden["revs"][1], sim=1.0, idx=i)
    nogrant = await _head(db, INTERNAL, "AADS", "a-flood", approved=1, latest=1)
    for i in range(30):
        await _chunk(db, f"canonical://AADS/a-flood@{nogrant['revs'][1]}#{i}", label="정본", project="AADS",
                     tenant=INTERNAL, head=nogrant["head_id"], revision=nogrant["revs"][1], sim=1.0, idx=i)
    got = await _paths(_scope(INTERNAL, "nobody"), path=path, top_k=3)
    assert got == {"/root/aads/docs/file.md"}  # 숨김 60건이 top 3 을 채웠다면 빈 결과였을 것
    got_a = await _paths(_scope(INTERNAL, "granted-user"), path=path, top_k=3)
    assert len(got_a) == 3 and not any("b-flood" in p for p in got_a)
    assert w  # 세계가 만들어졌다


# ── 5. 기존 파일 검색 회귀 · 응답 계약 ──────────────────────────────────

@pytest.mark.parametrize("path", PATHS)
async def test_existing_file_search_unchanged_for_any_tenant(db, monkeypatch, path):
    await _apply_migration(db)
    await _chunk(db, "/root/aads/docs/one.md", label="서버 문서(contabo116)", sim=0.9)
    await _chunk(db, "/root/aads/docs/two.md", label="서버 문서(contabo116)", project="KIS", sim=0.6)
    assert await _paths(_scope(INTERNAL, "u"), path=path) == {"/root/aads/docs/one.md", "/root/aads/docs/two.md"}
    assert await _paths(_scope(TENANT_B, "u"), path=path) == {"/root/aads/docs/one.md", "/root/aads/docs/two.md"}
    assert await _paths(_scope(TENANT_B, "u"), path=path, project="KIS") == {"/root/aads/docs/two.md"}


@pytest.mark.parametrize("mode", ["legacy", "shadow", "qwen3", "hybrid"])
async def test_search_docs_modes_respect_scope(db, monkeypatch, mode):
    from app.services import doc_index

    w = await _world(db)

    async def embed(_q):
        return _query(1024)

    monkeypatch.setattr(doc_index, "embed_qwen_query", embed)
    monkeypatch.setattr(doc_index, "_QWEN_MODE", mode)
    rows = await doc_index.search_docs(_query(768), top_k=20, query_text="질문", scope=_scope(TENANT_B, "b-user"))
    assert {r["doc_path"] for r in rows} == {_p(w["b_doc"], 1), "/root/aads/docs/file.md"}
    rows = await doc_index.search_docs(_query(768), top_k=20, query_text="질문", scope=_scope(INTERNAL, "nobody"))
    assert {r["doc_path"] for r in rows} == {"/root/aads/docs/file.md"}
    await asyncio.gather(*tuple(doc_index._shadow_tasks))


async def test_result_row_contract_keeps_label_and_title_prefix(db, monkeypatch):
    from app.services import doc_index

    w = await _world(db)
    rows = await doc_index.search_docs_legacy(_query(768), top_k=10, scope=_scope(INTERNAL, "granted-user"))
    canon = [r for r in rows if r["doc_path"] == _p(w["a_ok"], 1)][0]
    assert canon["label"] == "정본"
    for field in ("doc_path", "title", "heading", "content", "similarity", "doc_sha256", "mtime",
                  "indexed_at", "label"):
        assert field in canon


# ── 6. 색인 현황 · 문서별 조각 수 ────────────────────────────────────────

async def test_index_status_and_chunk_counts_exclude_hidden(db, monkeypatch):
    from app.services import doc_index

    w = await _world(db)
    status_b = await doc_index.index_status(_scope(TENANT_B, "b-user"))
    assert status_b["chunks"] == 2 and status_b["docs"] == 2
    status_none = await doc_index.index_status(_scope(INTERNAL, "nobody"))
    assert status_none["chunks"] == 1 and status_none["docs"] == 1
    counts = await doc_index.visible_chunk_counts(
        [_p(w["b_doc"], 1), _p(w["a_ok"], 1)], _scope(INTERNAL, "granted-user"))
    assert counts == {_p(w["a_ok"], 1): 1}


# ── 7. 색인기(index_docs.py) 가 쓴 값을 가드가 그대로 받아들인다 ─────────

async def test_indexer_written_chunks_are_visible_only_to_granted_tenant_users(db, monkeypatch):
    import importlib.util

    await _apply_migration(db)
    await _tenant(db, TENANT_B, "guard-b")
    approved = await _head(db, INTERNAL, "AADS", "idx-approved", approved=1, latest=1)
    draft = await _head(db, INTERNAL, "AADS", "idx-draft", latest=1)
    foreign = await _head(db, TENANT_B, "AADS", "idx-foreign", approved=1, latest=1)
    await _grant(db, INTERNAL, "AADS", "granted-user")

    spec = importlib.util.spec_from_file_location("index_docs_guard", ROOT / "scripts" / "index_docs.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    loop = asyncio.get_running_loop()

    psql = _psql_bridge(db, loop)

    m.psql = psql
    stats = await asyncio.to_thread(m.index_canonical, "srv")
    assert stats["docs"] == 2 and stats["changed"] == 2  # idx-approved, idx-draft (외부 tenant 의 idx-foreign 은 제외)
    rows = await db.fetch(
        "SELECT doc_path, tenant_id::text AS t, canonical_head_id::text AS h, canonical_revision_id::text AS r "
        "FROM doc_chunks WHERE doc_path LIKE 'canonical://%'")
    assert rows, "색인기가 정본 청크를 쓰지 않았다"
    assert all(r["t"] == INTERNAL and r["h"] and r["r"] for r in rows)
    assert not any("idx-foreign" in r["doc_path"] for r in rows)  # 내부 tenant 외 정본은 색인하지 않는다
    # 같은 내용 재실행은 쓰기 0
    again = await asyncio.to_thread(m.index_canonical, "srv")
    assert again["changed"] == 0
    # 색인기가 쓴 청크에 임베딩을 붙이면 가드 아래에서 검색된다
    for r in rows:
        cid = await db.fetchval("SELECT id::text FROM doc_chunks WHERE doc_path=$1 LIMIT 1", r["doc_path"])
        await db.execute("UPDATE doc_chunks SET embedding=$2::vector WHERE id=$1::uuid", cid, _vec(768, 0.8))
    got = await _paths(_scope(INTERNAL, "granted-user"), path="legacy")
    assert got == {_p(approved, 1), _p(draft, 1)}
    assert await _paths(_scope(INTERNAL, "nobody"), path="legacy") == set()
    assert await _paths(_scope(TENANT_B, "b-user"), path="legacy") == set()
    assert foreign["tenant"] == TENANT_B


async def test_reindex_repairs_legacy_canonical_rows_without_tenant(db, monkeypatch):
    import importlib.util

    await _apply_migration(db)
    h = await _head(db, INTERNAL, "AADS", "legacy-row", approved=1, latest=1)
    path = _p(h, 1)
    content = await db.fetchval("SELECT content FROM project_document_revisions WHERE id=$1::uuid", h["revs"][1])
    chash = await db.fetchval("SELECT content_hash FROM project_document_revisions WHERE id=$1::uuid", h["revs"][1])
    # 마이그레이션 이전에 색인된 정본 청크(칸이 비어 있음) — 같은 내용·같은 제목
    await db.execute(
        "INSERT INTO doc_chunks (server,doc_path,doc_sha256,project,label,title,heading,chunk_index,content) "
        "VALUES ('srv',$1,$2,'AADS','정본','[승인 v1.1.0] legacy-row 제목','',0,$3)", path, chash, content)
    await _grant(db, INTERNAL, "AADS", "granted-user")
    spec = importlib.util.spec_from_file_location("index_docs_guard2", ROOT / "scripts" / "index_docs.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    loop = asyncio.get_running_loop()

    psql = _psql_bridge(db, loop)

    m.psql = psql
    before = await db.fetchval("SELECT tenant_id FROM doc_chunks WHERE doc_path=$1", path)
    assert before is None  # 재색인 전: 격리 근거 없음 → 검색 불가(fail-closed)
    stats = await asyncio.to_thread(m.index_canonical, "srv")
    assert stats["changed"] == 1
    assert str(await db.fetchval("SELECT tenant_id FROM doc_chunks WHERE doc_path=$1", path)) == INTERNAL


# ── 8. API: 실제 SQL 로 end-to-end ───────────────────────────────────────

async def test_search_endpoint_end_to_end_tenant_isolation(db, monkeypatch):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from app.api import project_docs
    from app.services import doc_index

    w = await _world(db)
    _install_pool(monkeypatch, db)

    async def embed(_q):
        return _query(768)

    monkeypatch.setattr(doc_index, "embed_query", embed)
    monkeypatch.setattr(doc_index, "_QWEN_MODE", "legacy")
    app = FastAPI()
    app.include_router(project_docs.router, prefix="/api/v1")

    def ctx(tenant, user, role="member"):
        return {"user": {"user_id": user, "is_internal_admin": False}, "tenant": {"id": tenant},
                "membership": {"role": role}}

    async def call(context, **params):
        app.dependency_overrides[project_docs.require_tenant_member] = lambda: context
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            return await c.get("/api/v1/project-docs/search", params={"q": "질문입니다", "limit": 20, **params})

    res = await call(ctx(INTERNAL, "granted-user"))
    assert res.status_code == 200
    paths = {r["path"] for r in res.json()["results"]}
    assert _p(w["a_ok"], 1) in paths and _p(w["b_doc"], 1) not in paths
    assert res.json()["index"]["chunks"] == 5
    res_b = await call(ctx(TENANT_B, "b-user"), tenant_id=INTERNAL)  # 요청이 준 tenant 는 무시된다
    paths_b = {r["path"] for r in res_b.json()["results"]}
    assert paths_b == {_p(w["b_doc"], 1), "/root/aads/docs/file.md"}
    assert res_b.json()["index"]["chunks"] == 2
    res_n = await call(ctx(INTERNAL, "nobody"))
    assert {r["path"] for r in res_n.json()["results"]} == {"/root/aads/docs/file.md"}
    assert all(r["chunks"] == 1 for r in res_n.json()["results"])
    # 응답 계약 그대로
    assert set(res_n.json()["results"][0]) == {"path", "name", "project", "server", "title", "heading",
                                               "snippet", "similarity", "chunks"}


# ── 9. Auto-RAG 세션 범위 end-to-end ─────────────────────────────────────

async def test_auto_rag_uses_persisted_session_scope(db, monkeypatch):
    from app.services import auto_rag, doc_index

    w = await _world(db)
    _install_pool(monkeypatch, db)
    monkeypatch.setattr(doc_index, "_QWEN_MODE", "legacy")
    sessions = {}
    async with db.acquire() as conn:
        await conn.execute("SET session_replication_role = replica")  # 격리 DB 한정 — workspace FK 생략
        for label, tenant, user in (("granted", INTERNAL, "granted-user"), ("nobody", INTERNAL, "nobody"),
                                    ("b", TENANT_B, "b-user"), ("nouser", INTERNAL, None)):
            sessions[label] = await conn.fetchval(
                "INSERT INTO chat_sessions (workspace_id, tenant_id, user_id) VALUES ($1::uuid,$2::uuid,$3) "
                "RETURNING id::text", str(uuid4()), tenant, user)
        await conn.execute("SET session_replication_role = DEFAULT")

    async def run(label):
        rows = await auto_rag._search_documents(_query(768), "AADS", "질문", sessions[label])
        return {r["path"] for r in rows}

    assert _p(w["a_ok"], 1) in await run("granted")
    assert _p(w["b_doc"], 1) not in await run("granted")
    assert await run("nobody") == {"/root/aads/docs/file.md"}
    assert await run("nouser") == {"/root/aads/docs/file.md"}
    assert await run("b") == {_p(w["b_doc"], 1), "/root/aads/docs/file.md"}
    assert await auto_rag._search_documents(_query(768), "AADS", "질문", str(uuid4())) == []
    assert await auto_rag._search_documents(_query(768), "AADS", "질문", "") == []
