"""세션 정본 판본별 최신·승인·검토 필드를 실제 조회 SQL로 검증한다."""
import sqlite3

import pytest

from app.services import session_documents as sd


class SQLPool:
    def __init__(self, events=(), approved=1):
        self.calls = 0
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE chat_sessions (id TEXT, tenant_id TEXT);
            CREATE TABLE project_document_heads (
                id INTEGER, project_key TEXT, document_key TEXT,
                latest_revision_id INTEGER, approved_revision_id INTEGER);
            CREATE TABLE project_document_revisions (
                id INTEGER, head_id INTEGER, tenant_id TEXT, revision INTEGER,
                title TEXT, source_path TEXT, created_at TEXT,
                source_session_id TEXT, author_id TEXT);
            CREATE TABLE project_document_events (
                id INTEGER PRIMARY KEY, revision_id INTEGER, action TEXT);
            INSERT INTO chat_sessions VALUES ('session', 'tenant');
        """)
        self.db.execute(
            "INSERT INTO project_document_heads VALUES (1, 'AADS', 'session-doc', 3, ?)",
            (approved,),
        )
        for revision in range(1, 4):
            self.db.execute(
                "INSERT INTO project_document_revisions VALUES (?, 1, 'tenant', ?, "
                "'세션 문서', 'docs/session.md', ?, 'session', 'author')",
                (revision, revision, f"2026-10-30T00:00:0{revision}"),
            )
        self.db.executemany(
            "INSERT INTO project_document_events (revision_id, action) VALUES (?, ?)", events,
        )

    async def fetch(self, sql, *args):
        if "FROM project_document_revisions r" not in sql:
            return []
        self.calls += 1
        assert "ORDER BY r.created_at DESC" in sql
        assert "LIMIT 100" in sql
        # PostgreSQL 전용 세션/배열 문법만 SQLite 문법으로 바꾼다.
        # 상태 계산 SELECT와 JOIN, 이벤트 정렬은 원문 그대로 실행한다.
        sql = sql.replace("left($1::text, 8)", "substr($1, 1, 8)")
        sql = sql.replace("r.source_path = ANY($2::text[])", "r.source_path IN (NULL)")
        return self.db.execute(sql, {"1": str(args[0])}).fetchall()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "events,approved,expected_review",
    [
        ([(1, "approved"), (3, "review")], 1, True),
        ([(1, "approved")], 1, False),  # 최신 초안: 과거 승인과 구분
        ([(3, "review"), (3, "approved")], 3, False),
        ([(3, "review")], 3, False),  # 승인 포인터가 같으면 review여도 제외
        ([(3, "review")], None, True),
        ([], None, False),
        ([(3, "review"), (3, "archived")], 1, False),
        ([(3, "archived"), (3, "review"), (3, "edited")], 1, True),
        ([(2, "review")], 1, False),  # 과거 판본 검토는 최신에 전파하지 않음
    ],
)
async def test_canonical_revision_flags(monkeypatch, events, approved, expected_review):
    pool = SQLPool(events, approved)
    monkeypatch.setattr("app.core.db_pool.get_pool", lambda: pool)
    try:
        result = await sd.collect_session_documents("session")
    finally:
        pool.db.close()
    rows = result["canonical_documents"]
    assert pool.calls == 1  # 판본 수와 무관하게 정본 SQL 한 번
    assert [row["revision"] for row in rows] == [3, 2, 1]
    for row in rows:
        revision = row["revision"]
        assert row["is_latest"] is (revision == 3)
        assert row["superseded"] is (revision != 3)
        assert row["latest_revision"] == 3
        assert row["approved_revision"] == approved
        assert row["approved"] is (revision == approved)
        assert row["in_review"] is (revision == 3 and expected_review)
        assert row["project"] == "AADS"
        assert row["document_key"] == "session-doc"
        assert row["title"] == "세션 문서"
        assert row["at"] == f"2026-10-30T00:00:0{revision}"
        assert row["view"]["api_path"].endswith(f"/documents/session-doc/content?revision={revision}")
        assert row["view"]["status"] == ("approved" if revision == approved else "draft")
