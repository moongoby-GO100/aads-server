"""정본 문서 색인 회귀 테스트 (AADS-DOC-SEARCH-CANONICAL-INCLUDE-20261002).

scripts/index_docs.py 가 project_document_heads/revisions 의 정본을 doc_chunks 에
label='정본' 으로 넣되, 파일 색인(label 이 정본이 아닌 청크)과 서로 건드리지 않는지 확인한다.
DB 는 psql 대역으로 대체한다 — 운영 DB 에는 쓰지 않는다.
"""
import importlib.util
import json
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO / "scripts" / "index_docs.py"
SEP = "\x1f"

BODY = "정본 본문입니다. " * 30


def _load():
    spec = importlib.util.spec_from_file_location("index_docs_canonical", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _rev(rid, version="1.0.0", title="문서", content=BODY, digest=None):
    return {"id": rid, "version": version, "title": title, "content": content,
            "content_hash": digest or (rid * 64)[:64], "mtime": 1790000000}


TENANT = "00000000-0000-0000-0000-0000000000a1"
HEAD = "00000000-0000-0000-0000-0000000000b1"


def _scope_key(rid, tenant=TENANT, head=HEAD):
    return f"{tenant}|{head}|{rid}"


def _head(key="doc-a", project="AADS", approved=None, latest=None, archived=False, revs=()):
    return {"head_id": HEAD, "tenant_id": TENANT, "project_key": project, "document_key": key, "approved_revision_id": approved,
            "latest_revision_id": latest, "archived": archived, "revisions": list(revs)}


# ── 선택 규칙 ──────────────────────────────────────────────────────────────

def test_approved_only_when_latest_is_approved():
    m = _load()
    h = _head(approved="a", latest="a", revs=[_rev("a")])
    picked = m.pick_canonical_revisions([h])
    assert [(s, r["id"]) for s, r in picked] == [("승인", "a")]


def test_newer_latest_adds_draft_next_to_approved():
    m = _load()
    h = _head(approved="a", latest="b", revs=[_rev("a", "1.0.0"), _rev("b", "1.1.0")])
    picked = m.pick_canonical_revisions([h])
    assert [(s, r["id"]) for s, r in picked] == [("승인", "a"), ("초안", "b")]


def test_never_approved_head_indexes_latest_draft_only():
    m = _load()
    h = _head(approved=None, latest="b", revs=[_rev("b")])
    assert [(s, r["id"]) for s, r in m.pick_canonical_revisions([h])] == [("초안", "b")]


def test_archived_head_excluded_entirely():
    m = _load()
    h = _head(approved=None, latest="b", archived=True, revs=[_rev("b")])
    assert m.pick_canonical_revisions([h]) == []


def test_head_without_revisions_is_skipped_without_error():
    m = _load()
    assert m.pick_canonical_revisions([_head()]) == []


# ── 문서 변환: 가상 경로·접두·label·SECRET ──────────────────────────────────

def test_virtual_doc_path_format_and_no_collision_with_files():
    m = _load()
    p = m.canonical_doc_path("AADS", "doc-a", "rev-1")
    assert p == "canonical://AADS/doc-a@rev-1"
    assert m.is_canonical_path(p)
    assert not m.is_canonical_path("/root/aads/aads-server/docs/a.md")


def test_build_docs_uses_label_prefix_project_and_content_hash():
    m = _load()
    heads = [
        _head("doc-a", "AADS", approved="a", latest="b",
              revs=[_rev("a", "1.0.0", "승인문서", digest="1" * 64), _rev("b", "1.1.0", "승인문서", digest="2" * 64)]),
        _head("doc-g", "GO100", approved="c", latest="c", revs=[_rev("c", "2.0.0", "고백", digest="3" * 64)]),
    ]
    docs, skipped = m.build_canonical_docs(heads)
    assert skipped == 0
    by_path = {d["path"]: d for d in docs}
    assert set(by_path) == {"canonical://AADS/doc-a@a", "canonical://AADS/doc-a@b", "canonical://GO100/doc-g@c"}
    assert {d["label"] for d in docs} == {"정본"}
    assert by_path["canonical://AADS/doc-a@a"]["title"] == "[승인 v1.0.0] 승인문서"
    assert by_path["canonical://AADS/doc-a@b"]["title"] == "[초안 v1.1.0] 승인문서"
    assert by_path["canonical://AADS/doc-a@b"]["sha256"] == "2" * 64
    assert by_path["canonical://GO100/doc-g@c"]["project"] == "GO100"


def test_secret_content_or_title_is_not_indexed():
    m = _load()
    # 시크릿 모양 문자열은 소스에 리터럴로 두지 않고 런타임에 합성한다 —
    # 리터럴이면 gitleaks generic-api-key 가 테스트 파일 자체를 실제 키로 오탐한다.
    assignment = "_".join(["api", "key"]) + " = " + "x" * 3 + "0123456789"
    token = "".join(["gh", "p_"]) + "A" * 24
    heads = [
        _head("leak", approved="a", latest="a",
              revs=[_rev("a", content=BODY + "\n" + assignment + "\n")]),
        _head("leak-title", approved="b", latest="b",
              revs=[_rev("b", title="token " + token)]),
        _head("ok", approved="c", latest="c", revs=[_rev("c")]),
    ]
    assert m.SECRET.search(assignment) and m.SECRET.search(token)
    docs, skipped = m.build_canonical_docs(heads)
    assert skipped == 2
    assert [d["path"] for d in docs] == ["canonical://AADS/ok@c"]


def test_secret_pattern_matches_canonical_documents_module():
    m = _load()
    src = (_REPO / "app" / "api" / "canonical_documents.py").read_text(encoding="utf-8")
    ns: dict = {}
    start = src.index("SECRET = re.compile(")
    end = src.index("VIEW = Depends")
    exec("import re\n" + src[start:end], ns)  # noqa: S102 — 정규식 정의 블록만 실행
    assert m.SECRET.pattern == ns["SECRET"].pattern
    assert m.SECRET.flags == ns["SECRET"].flags


def test_short_canonical_document_still_gets_one_chunk():
    m = _load()
    assert m.chunk("짧다") == []
    assert m.canonical_chunks("짧다") == [("", "짧다")]


# ── prune 상호 불간섭 ──────────────────────────────────────────────────────

def test_stale_paths_do_not_cross_between_file_and_canonical():
    m = _load()
    known = {
        "/root/aads/aads-server/docs/gone.md": "x",
        "/root/aads/aads-server/docs/kept.md": "y",
        "canonical://AADS/old@r1": "z",
        "canonical://AADS/live@r2": "w",
    }
    live_files = {"/root/aads/aads-server/docs/kept.md"}
    assert m.stale_paths(known, live_files, canonical=False) == ["/root/aads/aads-server/docs/gone.md"]
    live_canon = {"canonical://AADS/live@r2"}
    assert m.stale_paths(known, live_canon, canonical=True) == ["canonical://AADS/old@r1"]


# ── DB 대역으로 cmd_index / index_canonical 실행 ───────────────────────────

class FakeDB:
    def __init__(self, heads=None, file_known=(), canon_known=(), table=True, scope_columns=True):
        self.scope_columns = scope_columns
        self.heads = heads
        self.file_known = list(file_known)      # (path, sha)
        self.canon_known = list(canon_known)    # (path, sha, title)
        self.table = table
        self.writes: list[str] = []
        self.queries: list[str] = []

    def __call__(self, sql, *, quiet=False):
        if "to_regclass" in sql:
            return "t\n" if self.table else "f\n"
        if "information_schema.columns" in sql:
            return "3\n" if self.scope_columns else "0\n"
        if sql.startswith("SELECT json_build_object"):
            return "".join(json.dumps(h, ensure_ascii=False) + "\n" for h in (self.heads or []))
        if sql.startswith("SELECT doc_path, doc_sha256, coalesce(tenant_id"):
            self.queries.append(sql)
            # canon_known: (path, sha, title) 또는 (path, sha, title, scope_key)
            return "".join(
                SEP.join((r[0], r[1], r[3] if len(r) > 3 else _scope_key(r[0].rsplit("@", 1)[-1]), r[2])) + "\n"
                for r in self.canon_known
            )
        if sql.startswith("SELECT doc_path, doc_sha256 FROM doc_chunks"):
            self.queries.append(sql)
            return "".join(SEP.join(r) + "\n" for r in self.file_known)
        self.writes.append(sql)
        return ""


def _file_doc(path, sha="f" * 64):
    return {"path": path, "project": "AADS", "label": "서버 문서", "sha256": sha,
            "size": 1000, "mtime": 1780000000.0, "title": "파일 문서", "text": BODY}


def _run_cmd_index(m, monkeypatch, db, docs):
    monkeypatch.setattr(m, "psql", db)
    monkeypatch.setattr(m, "collect", lambda: docs)
    monkeypatch.setattr(m, "server_name", lambda: "srv")

    class A:
        limit = 0

    m.cmd_index(A())


def _canon_heads():
    return [_head("doc-a", approved="a", latest="b", revs=[_rev("a", "1.0.0"), _rev("b", "1.1.0")])]


def test_file_stage_never_deletes_canonical_chunks(monkeypatch):
    m = _load()
    db = FakeDB(
        heads=[],  # 정본 단계는 정본이 하나도 없는 상태 — 정본 청크는 정본 단계에서만 지워진다
        file_known=[("/docs/a.md", "old"), ("/docs/gone.md", "g")],
        canon_known=[],
    )
    _run_cmd_index(m, monkeypatch, db, [_file_doc("/docs/a.md")])
    file_query = db.queries[0]
    assert "NOT LIKE 'canonical://%'" in file_query
    deletes = [w for w in db.writes if "DELETE" in w]
    assert any("/docs/gone.md" in w for w in deletes)
    assert not any("canonical://" in w for w in deletes)


def test_file_stage_ignores_canonical_even_if_query_returned_them(monkeypatch):
    m = _load()
    db = FakeDB(
        heads=_canon_heads(),
        file_known=[("/docs/a.md", "old"), ("canonical://AADS/doc-a@a", "x")],
        canon_known=[],
    )
    _run_cmd_index(m, monkeypatch, db, [_file_doc("/docs/a.md")])
    batch_deletes = [w for w in db.writes if "doc_path IN (" in w]
    assert batch_deletes == []


def test_file_chunks_identical_with_and_without_canonical_stage(monkeypatch):
    """정본 외 label 청크 INSERT 가 정본 단계 유무와 무관하게 같다."""
    m = _load()
    docs = [_file_doc("/docs/a.md"), _file_doc("/docs/b.md", "e" * 64)]

    def file_inserts(heads):
        db = FakeDB(heads=heads)
        _run_cmd_index(m, monkeypatch, db, docs)
        return sorted(w for w in db.writes if "INSERT INTO doc_chunks" in w and "'정본'" not in w)

    without = file_inserts([])
    with_canon = file_inserts(_canon_heads())
    assert without and without == with_canon

    db = FakeDB(heads=_canon_heads())
    _run_cmd_index(m, monkeypatch, db, docs)
    canon_txn = [w for w in db.writes if "INSERT INTO doc_chunks" in w and "'정본'" in w]
    assert len(canon_txn) == 1
    assert canon_txn[0].count("INSERT INTO doc_chunks") == 2
    assert "'/docs/" not in canon_txn[0]


def test_canonical_stage_runs_even_when_no_file_changed(monkeypatch):
    m = _load()
    db = FakeDB(heads=_canon_heads(), file_known=[("/docs/a.md", "f" * 64)])
    _run_cmd_index(m, monkeypatch, db, [_file_doc("/docs/a.md")])
    assert any("INSERT INTO doc_chunks" in w and "'정본'" in w for w in db.writes)


def test_canonical_stage_only_prunes_canonical_paths():
    m = _load()
    db = FakeDB(
        heads=_canon_heads(),
        canon_known=[("canonical://AADS/old@r1", "z", "[승인 v0.1.0] 옛것"),
                     ("/docs/sneaky.md", "z", "파일")],
    )
    m.psql = db
    stats = m.index_canonical("srv")
    assert stats["removed"] == 1
    prune = [w for w in db.writes if "doc_path IN (" in w][0]
    assert "canonical://AADS/old@r1" in prune and "/docs/sneaky.md" not in prune


def test_unchanged_canonical_makes_no_writes_and_title_change_reindexes():
    m = _load()
    heads = [_head("doc-a", approved="a", latest="a", revs=[_rev("a", "1.0.0", "문서", digest="a" * 64)])]
    same = FakeDB(heads=heads, canon_known=[("canonical://AADS/doc-a@a", "a" * 64, "[승인 v1.0.0] 문서")])
    m.psql = same
    stats = m.index_canonical("srv")
    assert stats["changed"] == 0 and same.writes == []

    promoted = FakeDB(heads=heads, canon_known=[("canonical://AADS/doc-a@a", "a" * 64, "[초안 v1.0.0] 문서")])
    m.psql = promoted
    stats = m.index_canonical("srv")
    assert stats["changed"] == 1
    assert any("'[승인 v1.0.0] 문서'" in w for w in promoted.writes)


def test_archive_transition_removes_stale_canonical_chunks():
    m = _load()
    heads = [_head("doc-a", approved=None, latest="a", archived=True, revs=[_rev("a")])]
    db = FakeDB(heads=heads, canon_known=[("canonical://AADS/doc-a@a", "a" * 64, "[승인 v1.0.0] 문서")])
    m.psql = db
    stats = m.index_canonical("srv")
    assert stats["docs"] == 0 and stats["removed"] == 1
    assert any("DELETE FROM doc_chunks" in w and "canonical://AADS/doc-a@a" in w for w in db.writes)
    assert not any("INSERT INTO doc_chunks" in w for w in db.writes)


def test_dry_run_writes_nothing():
    m = _load()
    db = FakeDB(heads=_canon_heads())
    m.psql = db
    stats = m.index_canonical("srv", dry_run=True)
    assert stats["changed"] == 2 and db.writes == []


def test_missing_canonical_table_is_skipped_and_prunes_nothing():
    m = _load()
    db = FakeDB(table=False, canon_known=[("canonical://AADS/x@y", "z", "t")])
    m.psql = db
    stats = m.index_canonical("srv")
    assert stats["docs"] == 0 and db.writes == []


def test_heads_sql_is_internal_tenant_only():
    m = _load()
    sql = m.CANONICAL_HEADS_SQL
    assert "public.aads_internal_tenant_id()" in sql
    assert "approved_revision_id IS NULL" in sql and "'archived'" in sql


# ── tenant 격리 칸 (AADS-DOC-SEARCH-TENANT-GUARD-20261003) ─────────────────

def test_built_docs_carry_tenant_head_and_revision_ids():
    m = _load()
    docs, _ = m.build_canonical_docs([_head("doc-a", approved="a", latest="b",
                                            revs=[_rev("a"), _rev("b", "1.1.0")])])
    assert {(d["tenant_id"], d["head_id"], d["revision_id"]) for d in docs} == {
        (TENANT, HEAD, "a"), (TENANT, HEAD, "b")}


def test_canonical_insert_writes_scope_columns():
    m = _load()
    db = FakeDB(heads=[_head("doc-a", approved="a", latest="a", revs=[_rev("a")])])
    m.psql = db
    m.index_canonical("srv")
    insert = [w for w in db.writes if "INSERT INTO doc_chunks" in w][0]
    assert "tenant_id,canonical_head_id,canonical_revision_id" in insert
    assert f"'{TENANT}'::uuid" in insert and f"'{HEAD}'::uuid" in insert and "'a'::uuid" in insert


def test_existing_canonical_rows_without_scope_are_rewritten_even_if_content_same():
    m = _load()
    heads = [_head("doc-a", approved="a", latest="a", revs=[_rev("a", "1.0.0", "문서", digest="a" * 64)])]
    legacy = FakeDB(heads=heads, canon_known=[
        ("canonical://AADS/doc-a@a", "a" * 64, "[승인 v1.0.0] 문서", "||")])
    m.psql = legacy
    assert m.index_canonical("srv")["changed"] == 1
    assert any("INSERT INTO doc_chunks" in w and f"'{TENANT}'::uuid" in w for w in legacy.writes)
    other_tenant = FakeDB(heads=heads, canon_known=[
        ("canonical://AADS/doc-a@a", "a" * 64, "[승인 v1.0.0] 문서",
         _scope_key("a", tenant="00000000-0000-0000-0000-0000000000ff"))])
    m.psql = other_tenant
    assert m.index_canonical("srv")["changed"] == 1


def test_indexer_refuses_to_write_canonical_without_tenant_columns():
    import pytest
    m = _load()
    db = FakeDB(heads=_canon_heads(), scope_columns=False)
    m.psql = db
    with pytest.raises(SystemExit) as exc:
        m.index_canonical("srv")
    assert exc.value.code == 2 and db.writes == []


def test_file_chunk_insert_does_not_write_scope_columns(monkeypatch):
    m = _load()
    db = FakeDB(heads=[])
    _run_cmd_index(m, monkeypatch, db, [_file_doc("/docs/a.md")])
    inserts = [w for w in db.writes if "INSERT INTO doc_chunks" in w]
    assert inserts and not any("tenant_id" in w for w in inserts)


def test_migration_is_additive_nullable_and_has_rollback():
    sql = (_REPO / "migrations" / "20261003_doc_chunks_tenant_scope.sql").read_text(encoding="utf-8")
    down = (_REPO / "migrations" / "rollback" / "20261003_doc_chunks_tenant_scope.down.sql").read_text(encoding="utf-8")
    for col in ("tenant_id", "canonical_head_id", "canonical_revision_id"):
        assert f"ADD COLUMN IF NOT EXISTS {col} uuid;" in sql
        assert f"DROP COLUMN IF EXISTS {col}" in down
    assert "ADD COLUMN IF NOT EXISTS" in sql and " uuid NOT NULL" not in sql and "DROP" not in sql and "DELETE" not in sql and "UPDATE" not in sql
    assert "CREATE INDEX IF NOT EXISTS" in sql
