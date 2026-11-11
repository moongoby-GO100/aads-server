"""migration-apply-* 문서의 위임 승인 게이트 테스트 (DB·git 없이)."""
from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import timedelta
from pathlib import Path

import pytest

from app.services import canonical_document_tools as cdt
from app.services import migration_delegation_gate as gate
from tests.unit.test_canonical_document_delegation import (  # noqa: F401  (autouse fixture 포함)
    NOW, PASS_BOTH, REASON, REV_ID, SESSION, TENANT, FakeConn, Pool, _no_enforce, delegation, review,
)

FILE = "migrations/20261111_x_add.sql"
GOOD = ("SET LOCAL lock_timeout = '5s';\n"
        "CREATE TABLE IF NOT EXISTS t_new (id int PRIMARY KEY);\n"
        "ALTER TABLE t_old ADD COLUMN IF NOT EXISTS note text;\n"
        "CREATE INDEX IF NOT EXISTS t_new_idx ON t_new(id);\n")
KEY = "migration-apply-m-abc123abc123"


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def doc(text: str = GOOD, file: str = FILE, digest: str | None = None) -> str:
    body = {"purpose": "migration_apply", "manifest": [{"file": file, "sha256": digest or sha(text)}]}
    return "# 승인\n```aads-migration-approval\n" + json.dumps(body) + "\n```\n"


def reader(files: dict):
    async def read(path):
        return files[path].encode() if path in files else None
    return read


def origin(text: str = GOOD, rollback: bool = True) -> dict:
    files = {FILE: text}
    if rollback:
        files[gate.rollback_path(FILE)] = "DROP TABLE t_new;"
    return files


class MigConn(FakeConn):
    def __init__(self, content: str, **kw):
        super().__init__(**kw)
        self.head["document_key"] = KEY
        self.content = content
        self.last_denial = None

    async def fetchval(self, sql, *args):
        if "delegated_approval_denied" in sql:
            return self.last_denial
        return await super().fetchval(sql, *args)

    async def fetchrow(self, sql, *args):
        if "SELECT content FROM project_document_revisions" in sql:
            return {"content": self.content}
        return await super().fetchrow(sql, *args)


@pytest.fixture
def files(monkeypatch):
    state = {"files": origin()}
    monkeypatch.setattr(gate, "origin_main_reader", lambda: (lambda p: reader(state["files"])(p)))
    return state


def approve(conn):
    inp = {"project": "AADS", "document_key": KEY, "revision_id": str(REV_ID),
           "expected_generation": 3, "reason": REASON}
    return asyncio.run(cdt.approve_document_delegated(tenant_id=TENANT, session_id=SESSION, inp=inp, pool=Pool(conn)))


def denied_reasons(res):
    assert res["approved"] is False and res["code"] == "migration_requires_ceo"
    return res["reasons"]


def test_additive_migration_approved_by_delegation_with_audit(files):
    conn = MigConn(doc(), delegations=[delegation()])
    res = approve(conn)
    assert res["approved"] is True and res["actor"] == f"chat:{SESSION}"
    insert = [e for e in conn.executed if "INSERT INTO project_document_events" in e[0]][0]
    meta = json.loads(insert[1][5])
    assert meta["delegation_id"] == res["delegation_id"]
    assert meta["migration_gate"]["files"][0]["file"] == FILE


REAL_FILE = Path(__file__).resolve().parents[2] / "migrations" / "20261111_canonical_document_approval_delegation.sql"
LOCK = "SET LOCAL lock_timeout = '5s';\n"

# 반례: 모두 대표 승인으로 넘어가야 한다. (이름, SQL 전문)
COUNTEREXAMPLES = [
    # 리뷰 1회차
    ("do_delete_string", LOCK + "DO 'BEGIN DELETE FROM t_old; END';\n"),
    ("alter_column_quoted_type", LOCK + 'ALTER TABLE t_old ALTER COLUMN "a b" TYPE bigint;\n'),
    ("do_dollar_not_null_default", LOCK + "DO $$ BEGIN ALTER TABLE t_old ADD COLUMN b int NOT NULL DEFAULT 0; END $$;\n"),
    ("do_ddl_without_lock_timeout", "DO $$ BEGIN CREATE TABLE t_new (id int); END $$;\n"),
    # 리뷰 2회차
    ("execute_concat", LOCK + "EXECUTE 'DE' || 'LETE FROM t_old';\n"),
    ("not_comment_null_default", LOCK + "ALTER TABLE t_old ADD COLUMN IF NOT EXISTS b int NOT /* 설명 */ NULL DEFAULT 0;\n"),
    ("set_config_where_false", "SELECT set_config('lock_timeout','5s',true) WHERE false;\nCREATE TABLE IF NOT EXISTS t_new (id int);\n"),
    ("lock_timeout_zero_string", "SET LOCAL lock_timeout = '0';\nCREATE TABLE IF NOT EXISTS t_new (id int);\n"),
    ("lock_timeout_0s", "SET LOCAL lock_timeout = '0s';\nCREATE TABLE IF NOT EXISTS t_new (id int);\n"),
    ("lock_timeout_0ms", "SET LOCAL lock_timeout = '0ms';\nCREATE TABLE IF NOT EXISTS t_new (id int);\n"),
    ("lock_timeout_unquoted_zero", "SET LOCAL lock_timeout = 0;\nCREATE TABLE IF NOT EXISTS t_new (id int);\n"),
    # 추가 반례
    ("block_comment_in_create_table", LOCK + "CREATE TABLE IF NOT EXISTS t_new (id int /* x */);\n"),
    ("block_comment_between_statements", LOCK + "/* x */\nCREATE TABLE IF NOT EXISTS t_new (id int);\n"),
    ("quoted_identifier_table", LOCK + 'CREATE TABLE IF NOT EXISTS "t new" (id int);\n'),
    ("quoted_identifier_column", LOCK + 'ALTER TABLE t_old ADD COLUMN IF NOT EXISTS "a" int;\n'),
    ("two_statements_one_line", LOCK + "CREATE TABLE IF NOT EXISTS t_new (id int); ALTER TABLE t_new ADD COLUMN IF NOT EXISTS note text;\n"),
    ("lock_timeout_after_ddl", "CREATE TABLE IF NOT EXISTS t_new (id int);\n" + LOCK),
    ("lock_timeout_missing", "CREATE TABLE IF NOT EXISTS t_new (id int);\n"),
    ("lock_timeout_twice", LOCK + LOCK + "CREATE TABLE IF NOT EXISTS t_new (id int);\n"),
    # 허용 목록 밖 문장·변형
    ("drop_table", LOCK + "DROP TABLE t_old;\n"),
    ("truncate", LOCK + "TRUNCATE t_old;\n"),
    ("delete", LOCK + "DELETE FROM t_old WHERE id=1;\n"),
    ("update", LOCK + "UPDATE t_old SET a=1;\n"),
    ("insert", LOCK + "INSERT INTO t_old VALUES (1);\n"),
    ("rename", LOCK + "ALTER TABLE t_old RENAME TO t_older;\n"),
    ("drop_column", LOCK + "ALTER TABLE t_old DROP COLUMN IF EXISTS a;\n"),
    ("alter_type", LOCK + "ALTER TABLE t_old ALTER COLUMN a TYPE bigint;\n"),
    ("grant", LOCK + "GRANT ALL ON t_old TO x;\n"),
    ("create_function", LOCK + "CREATE FUNCTION f() RETURNS int AS 'select 1' LANGUAGE sql;\n"),
    ("dollar_tag_in_string", LOCK + "COMMENT ON TABLE t_new IS $f$x$f$;\n"),
    ("begin_commit", "BEGIN;\n" + LOCK + "CREATE TABLE IF NOT EXISTS t_new (id int);\nCOMMIT;\n"),
    ("create_table_without_if_not_exists", LOCK + "CREATE TABLE t_new (id int);\n"),
    ("create_table_as_select", LOCK + "CREATE TABLE IF NOT EXISTS t_new AS SELECT * FROM t_old;\n"),
    ("create_table_trailing_clause", LOCK + "CREATE TABLE IF NOT EXISTS t_new (id int) INHERITS (t_old);\n"),
    ("create_index_without_if_not_exists", LOCK + "CREATE INDEX i ON t_new(id);\n"),
    ("create_index_using", LOCK + "CREATE INDEX IF NOT EXISTS i ON t_new USING gin (id);\n"),
    ("add_column_not_null", LOCK + "ALTER TABLE t_old ADD COLUMN IF NOT EXISTS b int NOT NULL;\n"),
    ("add_column_default", LOCK + "ALTER TABLE t_old ADD COLUMN IF NOT EXISTS b int DEFAULT 0;\n"),
    ("add_column_references", LOCK + "ALTER TABLE t_old ADD COLUMN IF NOT EXISTS b int REFERENCES t_new(id);\n"),
    ("add_column_without_if_not_exists", LOCK + "ALTER TABLE t_old ADD COLUMN b int;\n"),
    ("three_part_identifier", LOCK + "CREATE TABLE IF NOT EXISTS a.b.c (id int);\n"),
    ("uppercase_identifier_via_quote_free", LOCK + "CREATE TABLE IF NOT EXISTS T_New (id int);\n"),
    ("escape_string", LOCK + "COMMENT ON TABLE t_new IS E'a\\'';\n"),
    ("forbidden_word_in_comment_string", LOCK + "COMMENT ON TABLE t_new IS 'will DELETE rows';\n"),
    ("inline_line_comment_in_statement", LOCK + "CREATE TABLE IF NOT EXISTS t_new (id int -- x\n);\n"),
    ("empty_file", ""),
]


def _problems(text: str) -> list[str]:
    return gate.static_problems(text)


def test_real_delegation_migration_file_is_delegable():
    """실제 마이그레이션 파일이 허용 목록을 통과해야 한다. 걸리면 허용 목록을 넓히지 말고 보고한다."""
    assert _problems(REAL_FILE.read_text(encoding="utf-8")) == []


def test_good_fixture_passes_allowlist():
    assert _problems(GOOD) == []


def test_two_allowed_statements_on_one_line_rejected():
    """한 줄에 허용 문장 2개가 있으면 둘 다 허용 형식이더라도 multiple_statements_one_line 으로 거절된다."""
    text = LOCK + "CREATE TABLE IF NOT EXISTS t_new (id int); ALTER TABLE t_new ADD COLUMN IF NOT EXISTS note text;\n"
    problems = _problems(text)
    assert problems == ["multiple_statements_one_line"]


def test_multiline_create_table_passes():
    """여러 줄에 걸친 CREATE TABLE 한 문장은 통과해야 한다."""
    text = LOCK + ("CREATE TABLE IF NOT EXISTS t_new (\n"
                   "    id uuid PRIMARY KEY,\n"
                   "    note text\n"
                   ");\n")
    assert _problems(text) == []


@pytest.mark.parametrize("name,text", COUNTEREXAMPLES, ids=[c[0] for c in COUNTEREXAMPLES])
def test_counterexamples_stay_with_ceo(name, text):
    assert _problems(text), name


@pytest.mark.parametrize("name,text", COUNTEREXAMPLES, ids=[c[0] for c in COUNTEREXAMPLES])
def test_counterexamples_denied_end_to_end(files, name, text):
    files["files"] = origin(text)
    res = approve(MigConn(doc(text), delegations=[delegation()]))
    assert res["approved"] is False and res["code"] == "migration_requires_ceo" and res["reasons"], name


def test_line_comments_between_statements_are_stripped():
    text = "-- DROP TABLE x\n" + GOOD + "-- DELETE FROM y\n"
    assert _problems(text) == []


def test_allowed_variants():
    text = ("SET LOCAL lock_timeout = '500ms';\nSET LOCAL statement_timeout = '2min';\n"
            "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS s.i ON s.t (a, b) WHERE a IS NOT NULL;\n"
            "ALTER TABLE s.t ADD COLUMN IF NOT EXISTS c timestamp with time zone;\n"
            "ALTER TABLE t ADD COLUMN IF NOT EXISTS d numeric(10, 2);\n"
            "COMMENT ON COLUMN s.t.c IS 'it''s fine';\n"
            "CREATE TABLE IF NOT EXISTS t2 (id uuid PRIMARY KEY, x int NOT NULL DEFAULT 0 CHECK (x >= 0));\n")
    assert _problems(text) == []


@pytest.mark.parametrize("value", ["0", "0s", "0ms", "00s", "s", "5", "5h", "-1s"])
def test_lock_timeout_value_is_checked_on_raw_text(value):
    text = f"SET LOCAL lock_timeout = '{value}';\nCREATE TABLE IF NOT EXISTS t_new (id int);\n"
    assert _problems(text)


def test_sha_mismatch_with_origin_main(files):
    files["files"] = origin(GOOD + "-- changed\n")
    assert f"sha_mismatch:{FILE}" in denied_reasons(approve(MigConn(doc(), delegations=[delegation()])))


def test_file_missing_on_origin(files):
    files["files"] = {}
    assert f"origin_file_missing:{FILE}" in denied_reasons(approve(MigConn(doc(), delegations=[delegation()])))


def test_rollback_missing(files):
    files["files"] = origin(rollback=False)
    assert f"rollback_missing:{FILE}" in denied_reasons(approve(MigConn(doc(), delegations=[delegation()])))


def test_malformed_document_stays_with_ceo(files):
    reasons = denied_reasons(approve(MigConn("manifest 없음", delegations=[delegation()])))
    assert reasons == ["migration_doc_invalid"]


def test_denial_is_audited_without_approval_row(files):
    bad = GOOD + "DROP TABLE z;\n"
    files["files"] = origin(bad)
    conn = MigConn(doc(bad), delegations=[delegation()])
    res = approve(conn)
    assert res["approved"] is False
    sqls = [e[0] for e in conn.executed]
    assert not any("UPDATE project_document_heads" in q for q in sqls)  # 승인 반영 없음
    inserts = [e for e in conn.executed if "INSERT INTO project_document_events" in e[0]]
    assert len(inserts) == 1
    assert "'goal_linked'" in inserts[0][0] and cdt.DENIED_EVENT_ACTION == "goal_linked"
    assert "'approved'" not in inserts[0][0]
    meta = json.loads(inserts[0][1][5])
    assert meta["code"] == "migration_requires_ceo" and f"forbidden_word_drop:{FILE}" in meta["reasons"]
    assert meta["session_id"] == f"chat:{SESSION}"


def test_denial_event_not_duplicated_for_same_reasons(files):
    bad = GOOD + "DROP TABLE z;\n"
    files["files"] = origin(bad)
    conn = MigConn(doc(bad), delegations=[delegation()])
    conn.last_denial = json.dumps({"reasons": denied_reasons(approve(MigConn(doc(bad), delegations=[delegation()])))})
    approve(conn)
    assert not [e for e in conn.executed if "INSERT INTO project_document_events" in e[0]]


def test_audit_failure_does_not_block_denial(files):
    bad = GOOD + "DROP TABLE z;\n"
    files["files"] = origin(bad)
    conn = MigConn(doc(bad), delegations=[delegation()])
    async def boom(sql, *a):
        raise RuntimeError("no metadata column")
    conn.execute = boom
    assert denied_reasons(approve(conn))


def test_default_reader_fails_closed(monkeypatch):
    from app.api import canonical_documents as docs

    async def boom(path):
        raise RuntimeError("github down")
    monkeypatch.setattr(docs, "_github_fetch", boom)
    assert asyncio.run(gate.origin_main_reader()(FILE)) is None


def test_default_reader_uses_remote_origin_not_local_git(monkeypatch):
    from app.api import canonical_documents as docs
    seen = []

    async def fetch(path):
        seen.append(path)
        return b"x"
    monkeypatch.setattr(docs, "_github_fetch", fetch)
    assert asyncio.run(gate.origin_main_reader()(FILE)) == b"x" and seen == [FILE]


def test_cross_review_still_required(files):
    conn = MigConn(doc(), delegations=[delegation()], reviews=[review("anthropic", "a")])
    assert approve(conn)["code"] == "cross_review_incomplete"


def test_expired_and_revoked_delegation_denied(files):
    expired = delegation(expires=NOW - timedelta(days=1))
    revoked = delegation(revoked=NOW - timedelta(hours=1))
    assert approve(MigConn(doc(), delegations=[expired]))["code"] == "delegation_missing"
    assert approve(MigConn(doc(), delegations=[revoked]))["code"] == "delegation_missing"


def test_no_delegation_keeps_existing_behavior(files):
    conn = MigConn(doc())
    res = approve(conn)
    assert res["code"] == "delegation_missing" and res["reasons"] == ["delegation_missing"]
    assert not any("UPDATE project_document_heads" in e[0] for e in conn.executed)  # 승인 반영 없음(거절 감사만)


def test_non_migration_document_unaffected(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("게이트가 일반 문서에서 호출됨")
    monkeypatch.setattr(gate, "migration_delegation_problems", boom)
    conn = FakeConn(delegations=[delegation()])
    inp = {"project": "AADS", "document_key": "doc-a", "revision_id": str(REV_ID),
           "expected_generation": 3, "reason": REASON}
    res = asyncio.run(cdt.approve_document_delegated(tenant_id=TENANT, session_id=SESSION, inp=inp, pool=Pool(conn)))
    assert res["approved"] is True


def test_auto_approval_still_skips_migration_documents():
    from app.services import doc_auto_approval as auto
    head = {"document_key": KEY, "kind": "contract", "latest_revision_id": REV_ID, "goal_linked": True}
    assert "migration_document" in auto._skip_reasons(head, {"id": REV_ID, "doc_tier": "D2", "title": "t"})


def test_every_denial_carries_reasons_and_is_audited(files):
    """거절 사유가 비어 있는 거절 경로가 없다 — 어떤 거절이든 reasons 가 있고 감사 이벤트가 남는다."""
    from app.services import canonical_document_delegation as deleg
    assert deleg._denied("x")["reasons"] == ["x"]
    assert deleg._denied("x", reasons=[])["reasons"] == ["x"]
    cases = {
        "delegation_missing": MigConn(doc()),
        "cross_review_incomplete": MigConn(doc(), delegations=[delegation()], reviews=[review("anthropic", "a")]),
    }
    for code, conn in cases.items():
        res = approve(conn)
        assert res["approved"] is False and res["code"] == code and res["reasons"], code
        inserts = [e for e in conn.executed if "INSERT INTO project_document_events" in e[0]]
        assert len(inserts) == 1 and json.loads(inserts[0][1][5])["code"] == code, code
        assert not any("UPDATE project_document_heads" in e[0] for e in conn.executed)


@pytest.mark.parametrize("problem", ["cross_review_blocker_present", "cross_review_major_present",
                                     "cross_review_reject_present", "cross_review_unreliable"])
def test_cross_review_blocker_reason_recorded(files, monkeypatch, problem):
    from app.services import canonical_document_delegation as deleg
    monkeypatch.setattr(deleg, "cross_review_problem", lambda rows: problem)
    conn = MigConn(doc(), delegations=[delegation()])
    res = approve(conn)
    assert res["approved"] is False and res["code"] == problem and res["reasons"] == [problem]
    inserts = [e for e in conn.executed if "INSERT INTO project_document_events" in e[0]]
    assert len(inserts) == 1 and json.loads(inserts[0][1][5])["reasons"] == [problem]


def test_all_denial_paths_audited_in_migration_context(files, monkeypatch):
    """거절 경로마다 (평가 거절, 승인 단계 HTTPException, 그 밖의 예외) 감사 이벤트 1건과 비어 있지 않은 reasons 단언."""
    from app.api import canonical_documents as docs

    # 1. 평가 거절 (마이그레이션 정적 검사 위반)
    bad = GOOD + "DROP TABLE z;\n"
    files["files"] = origin(bad)
    conn_eval = MigConn(doc(bad), delegations=[delegation()])
    res_eval = approve(conn_eval)
    assert res_eval["approved"] is False and res_eval["code"] == "migration_requires_ceo" and res_eval["reasons"]
    inserts_eval = [e for e in conn_eval.executed if "INSERT INTO project_document_events" in e[0]]
    assert len(inserts_eval) == 1
    assert json.loads(inserts_eval[0][1][5])["reasons"] == res_eval["reasons"]

    # 2. 승인 단계 HTTPException (last != 'review'): 평가는 통과해야 승인 단계로 진입
    files["files"] = origin(GOOD)
    conn_http = MigConn(doc(), delegations=[delegation()], last="draft")
    res_http = approve(conn_http)
    assert res_http["approved"] is False and res_http["code"] == "revision_not_in_review" and res_http["reasons"] == ["revision_not_in_review"]
    inserts_http = [e for e in conn_http.executed if "INSERT INTO project_document_events" in e[0]]
    assert len(inserts_http) == 1
    assert json.loads(inserts_http[0][1][5])["reasons"] == ["revision_not_in_review"]

    # 3. 그 밖의 예외 (RuntimeError): 평가는 통과해야 승인 단계로 진입
    files["files"] = origin(GOOD)
    async def boom(*a, **kw):
        raise RuntimeError("core failure")
    monkeypatch.setattr(docs, "approve_revision_in_tx", boom)
    conn_exc = MigConn(doc(), delegations=[delegation()])
    res_exc = approve(conn_exc)
    assert res_exc["approved"] is False and res_exc["code"] == "RuntimeError" and res_exc["reasons"] == ["core failure"]
    inserts_exc = [e for e in conn_exc.executed if "INSERT INTO project_document_events" in e[0]]
    assert len(inserts_exc) == 1
    assert json.loads(inserts_exc[0][1][5])["reasons"] == ["core failure"]

