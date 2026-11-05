"""반려 뒤 자동 재신고 차단 (AADS-MILESTONE-GATE-AFTER-REJECT-20261104).

GO100 M4 를 주도가 4회 반려했지만 연결 task 4건이 그대로 완료라서 1~2분 안에
review 로 되돌아왔다. 반려 이후에 새로 연결된 작업이 없으면 올리지 않는다.
"""
import asyncio
from datetime import datetime, timezone

import asyncpg
import pytest

from app.core import db_pool
from app.services import goal_dispatch, goal_manager, goal_work_item_sync, milestone_review

GOAL = "11111111-1111-4111-8111-111111111111"
MS = "33333333-3333-4333-8333-333333333333"
REJECTED = datetime(2026, 11, 4, 5, 33, tzinfo=timezone.utc)


class Store:
    def __init__(self, rejected_at=REJECTED, has_col=True, new_link=False, status="in_progress",
                 criteria="완료기준 있음", auto_advance=True):
        self.ms = {"status": status, "version": 3, "rejected_at": rejected_at}
        self.criteria = criteria
        self.auto_advance = auto_advance
        self.new_link_query = ""
        self.has_col = has_col
        self.new_link = new_link
        self.updates = []
        self.new_link_args = None

    def transaction(self):
        class _Tx:
            async def __aenter__(self):
                return None

            async def __aexit__(self, *_):
                return False

        return _Tx()

    async def fetchrow(self, query, *args):
        if "FOR UPDATE" in query:
            return {"status": self.ms["status"], "version": self.ms["version"],
                    "auto_advance": self.auto_advance, "completion_criteria": self.criteria,
                    "completion_checklist": None, "has_reject_col": self.has_col,
                    "review_rejected_at": self.ms["rejected_at"] if self.has_col else None}
        raise AssertionError(query)

    async def fetchval(self, query, *args):
        assert "goal_task_links" in query and "created_at" in query
        self.new_link_query = query
        self.new_link_args = args
        return self.new_link

    async def fetch(self, query, *args):
        return [{"task_type": "pipeline_job", "task_id": "j1", "status": "completed", "goal_id": GOAL}]

    async def execute(self, query, *args):
        self.updates.append(query)
        if "SET status = 'completed'" in query:
            self.ms["status"] = "completed"
        if "SET status = 'review'" in query:
            assert self.has_col or "review_rejected_at" not in query
            self.ms["status"] = "review"
            if "review_rejected_at = NULL" in query:
                self.ms["rejected_at"] = None
        return "UPDATE 1"


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class _Acq:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *_):
                return False

        return _Acq()


@pytest.fixture
def gate(monkeypatch):
    link_columns = {"cols": set()}

    async def columns(_conn):
        return link_columns["cols"]

    monkeypatch.setattr(goal_manager, "link_optional_columns", columns)
    monkeypatch.setattr(goal_manager, "_reject_col_warned", False)

    def run(store, link_cols=()):
        link_columns["cols"] = set(link_cols)
        return asyncio.run(goal_manager.GoalStateMachine._complete_milestone_if_checklist_met(store, MS, ["j1"]))

    return run


def test_no_new_link_after_reject_does_not_rereport(gate):
    store = Store(new_link=False)
    result = gate(store)
    assert result["result"] == "gated_after_reject"
    assert store.ms["status"] == "in_progress"
    assert store.updates == []
    assert store.new_link_args == (MS, REJECTED)


def test_new_link_after_reject_rereports_and_clears_mark(gate):
    store = Store(new_link=True)
    result = gate(store)
    assert result["result"] == "gated"
    assert store.ms["status"] == "review"
    assert store.ms["rejected_at"] is None


def test_reject_blocks_auto_complete_without_gate_conditions(gate):
    """auto_advance=true 이고 completion_criteria 가 비어도 반려 뒤 새 연결 없이는 completed 로 가지 않는다."""
    store = Store(new_link=False, criteria="", auto_advance=True)
    result = gate(store)
    assert result["result"] == "gated_after_reject"
    assert store.ms["status"] == "in_progress"
    assert store.updates == []


def test_new_link_after_reject_lets_ungated_milestone_complete(gate):
    store = Store(new_link=True, criteria="", auto_advance=True)
    assert gate(store)["result"] == "completed"
    assert store.ms["status"] == "completed"


def test_never_rejected_ungated_milestone_completes_as_before(gate):
    store = Store(rejected_at=None, criteria="", auto_advance=True)
    assert gate(store)["result"] == "completed"
    assert store.new_link_args is None


def test_relinked_task_counts_by_milestone_linked_at(gate):
    """created_at 이 아니라 milestone_linked_at 으로 새 연결을 판정한다(ON CONFLICT 재연결은 created_at 유지)."""
    store = Store(new_link=True)
    assert gate(store, link_cols={"milestone_linked_at"})["result"] == "gated"
    assert "COALESCE(milestone_linked_at, created_at) > $2" in store.new_link_query


def test_created_at_fallback_when_milestone_linked_at_missing(gate):
    store = Store(new_link=False)
    assert gate(store, link_cols=set())["result"] == "gated_after_reject"
    assert "milestone_linked_at" not in store.new_link_query
    assert "created_at > $2" in store.new_link_query


def test_never_rejected_behaves_as_before(gate):
    store = Store(rejected_at=None)
    result = gate(store)
    assert result["result"] == "gated"
    assert store.new_link_args is None
    assert store.ms["status"] == "review"


def test_missing_column_falls_back_with_warning(gate, caplog):
    store = Store(has_col=False)
    with caplog.at_level("WARNING", logger=goal_manager.logger.name):
        result = gate(store)
    assert result["result"] == "gated"
    assert store.ms["status"] == "review"
    assert all("review_rejected_at" not in q for q in store.updates)
    assert any("review_rejected_at" in r.getMessage() for r in caplog.records)


def test_check_completion_maps_gated_after_reject(monkeypatch):
    store = Store(new_link=False)
    machine = goal_manager.GoalStateMachine()

    async def pool():
        return _Pool(store)

    async def columns(_conn):
        return set()

    async def noop(*_a, **_k):
        return None

    async def forbidden(*_a, **_k):
        raise AssertionError("반려 게이트에서는 다음 단계로 넘어가면 안 된다")

    monkeypatch.setattr(machine, "_pool", pool)
    monkeypatch.setattr(goal_manager, "link_optional_columns", columns)
    monkeypatch.setattr(machine, "_mark_superseded_failures", noop)
    monkeypatch.setattr(goal_manager, "mark_explicit_retry_supersession", noop)
    monkeypatch.setattr(goal_manager, "archive_exhaustion_note", forbidden)
    monkeypatch.setattr(machine, "_advance_after_milestone", forbidden)
    result = asyncio.run(machine.check_milestone_completion(MS))
    assert result == {"milestone_id": MS, "completed": False, "status": "in_progress",
                      "reason": "gated_after_reject"}


class ReviewConn:
    def __init__(self, status, missing_col=False):
        self.status = status
        self.missing_col = missing_col
        self.queries = []

    async def fetchrow(self, query, *args):
        return {"status": self.status, "title": "M4 마일스톤", "goal_id": GOAL,
                "completion_criteria": "", "dispatched_at": None, "dispatch_count": 0}

    async def execute(self, query, *args):
        if self.missing_col and "review_rejected_at" in query:
            raise asyncpg.UndefinedColumnError('column "review_rejected_at" of relation "milestones" does not exist')
        self.queries.append(query)
        return "UPDATE 1"


@pytest.fixture
def review_env(monkeypatch):
    holder = {}
    monkeypatch.setattr(db_pool, "get_pool", lambda: _Pool(holder["conn"]))

    async def noop(*_a, **_k):
        return None

    monkeypatch.setattr(goal_work_item_sync, "sync_milestone", noop)
    monkeypatch.setattr(goal_dispatch, "note_record_reset", lambda *a, **k: None)
    monkeypatch.setattr(goal_manager.goal_state_machine, "_update_goal_progress", noop)

    class _C:
        @staticmethod
        def conn(conn):
            holder["conn"] = conn
            return conn

    return _C


def test_owner_direct_report_is_allowed_and_clears_rejection(review_env):
    conn = review_env.conn(ReviewConn("in_progress"))
    evidence = {"summary": "반려 사유를 반영해 재작업했고 결과를 확인했다. 캡처 첨부."}
    result = asyncio.run(milestone_review.report_done(MS, evidence, ""))
    assert result["status"] == "review"
    assert "review_rejected_at = NULL" in conn.queries[-1]


def test_reject_records_timestamp(review_env):
    conn = review_env.conn(ReviewConn("review"))
    result = asyncio.run(milestone_review.confirm(MS, ok=False, reason="증거 부족"))
    assert result["status"] == "in_progress"
    assert "review_rejected_at = NOW()" in conn.queries[-1]


def test_reject_and_report_fall_back_when_column_missing(review_env):
    conn = review_env.conn(ReviewConn("review", missing_col=True))
    assert asyncio.run(milestone_review.confirm(MS, ok=False, reason="증거 부족"))["status"] == "in_progress"
    assert "review_rejected_at" not in conn.queries[-1] and "reported_at = NULL" in conn.queries[-1]
    conn.status = "in_progress"
    evidence = {"summary": "반려 사유를 반영해 재작업했고 결과를 확인했다. 캡처 첨부."}
    assert asyncio.run(milestone_review.report_done(MS, evidence, ""))["status"] == "review"
    assert "review_rejected_at" not in conn.queries[-1] and "status = 'review'" in conn.queries[-1]


def test_migration_is_additive_nullable():
    from pathlib import Path

    sql = (Path(__file__).resolve().parents[2] / "migrations" / "20261104_milestone_review_rejected_at.sql").read_text()
    assert "ADD COLUMN IF NOT EXISTS review_rejected_at timestamptz NULL" in sql
    assert "DROP COLUMN IF EXISTS review_rejected_at" in sql


def _migration_sql() -> str:
    from pathlib import Path

    return (Path(__file__).resolve().parents[2] / "migrations" / "20261104_milestone_review_rejected_at.sql").read_text()


def _code_only(sql: str) -> str:
    return "\n".join(line for line in sql.splitlines() if not line.lstrip().startswith("--"))


def test_migration_stamps_milestone_linked_at_on_relink():
    sql = _migration_sql()
    assert "ALTER TABLE goal_task_links ADD COLUMN IF NOT EXISTS milestone_linked_at timestamptz NULL" in sql
    assert "NEW.milestone_id IS DISTINCT FROM OLD.milestone_id" in sql
    assert "NEW.milestone_linked_at IS NULL" in sql
    assert "DROP COLUMN IF EXISTS milestone_linked_at" in sql
    assert "CREATE OR REPLACE TRIGGER trg_goal_task_links_milestone_linked_at" in sql
    assert "BEFORE INSERT OR UPDATE OF milestone_id ON goal_task_links" in sql
    assert "FOR EACH ROW EXECUTE FUNCTION goal_task_links_stamp_milestone_linked_at()" in sql


def test_migration_never_overwrites_existing_function_or_trigger():
    code = _code_only(_migration_sql()).upper()
    assert "CREATE OR REPLACE FUNCTION" not in code  # FUNCTION은 쓰지 않지만 최상위 TRIGGER는 사용
    assert "DROP TRIGGER" not in code and "DROP FUNCTION" not in code
    assert "CREATE FUNCTION goal_task_links_stamp_milestone_linked_at()".upper() in code
    assert code.count("RAISE EXCEPTION") >= 3
    assert "P.PROSRC = V_BODY" in code and "V_TRG.TGFOID = V_FN_OID" in code and "V_TRG.TGTYPE = 23" in code


def test_migration_sets_local_lock_timeout_and_has_no_index_or_reset():
    sql = _migration_sql()
    code = _code_only(sql).upper()
    assert "SET LOCAL lock_timeout = '5s';" in sql
    assert "SET LOCAL statement_timeout = '60s';" in sql
    assert "RESET " not in code and "CREATE INDEX" not in code


def test_migration_header_documents_rollback_safety_and_objects():
    sql = _migration_sql()
    for needle in (
        "goal_task_links_stamp_milestone_linked_at()",
        "trg_goal_task_links_milestone_linked_at",
        "이전 버전 API 코드는 읽지도 쓰지도 않는다",
        "덮어쓰지 않는다",
        "NOW() 대입뿐",
        "DROP TRIGGER IF EXISTS trg_goal_task_links_milestone_linked_at ON goal_task_links;",
        "DROP FUNCTION IF EXISTS goal_task_links_stamp_milestone_linked_at();",
        "DROP COLUMN IF EXISTS review_rejected_at",
    ):
        assert needle in sql, needle


def test_migration_no_split_keywords():
    """쪼갠 키워드('UPD' || 'ATE' 형태)가 없어야 한다."""
    sql = _migration_sql()
    assert "'UPD' || 'ATE'" not in sql


def test_migration_passes_autoverify_additive_classifier():
    import sys
    from pathlib import Path

    scripts = str(Path(__file__).resolve().parents[2] / "scripts")
    sys.path.insert(0, scripts)
    try:
        import release_migration_autoverify as av
    finally:
        sys.path.remove(scripts)
    path = Path(__file__).resolve().parents[2] / "migrations" / "20261104_milestone_review_rejected_at.sql"
    assert av.classify_files([path]) == []
