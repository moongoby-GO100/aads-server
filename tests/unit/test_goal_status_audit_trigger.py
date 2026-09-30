"""goals.status 감사 트리거 마이그레이션 — DB 없이 도는 구조 검사.

AADS-GOAL-STATUS-AUDIT-DB-TRIGGER-20260930: 자동경로 5곳이 무감사였다. 트리거가 모든 전이를
잡고, 애플리케이션이 같은 전이를 또 INSERT 하면 병합 트리거가 1행으로 합친다.
"""
import re
from pathlib import Path

ROOT = Path(__file__).parents[2]
UP_PATH = ROOT / "migrations/20260930_goal_status_audit_trigger.sql"
DOWN_PATH = ROOT / "migrations/rollback/20260930_goal_status_audit_trigger.down.sql"


def _code(path: Path) -> str:
    """주석을 뺀 SQL. 설명문 속 단어가 검사를 통과시키거나 막지 않게 한다."""
    return "\n".join(line.split("--", 1)[0] for line in path.read_text().splitlines())


def _norm(sql: str) -> str:
    return re.sub(r"\s+", " ", sql)


def test_up_migration_exists():
    assert UP_PATH.is_file()
    assert DOWN_PATH.is_file()


def test_capture_trigger_fires_only_on_real_status_change():
    sql = _norm(_code(UP_PATH))
    assert "CREATE OR REPLACE FUNCTION goal_status_audit_capture() RETURNS trigger" in sql
    assert "AFTER UPDATE OF status ON goals" in sql
    assert "FOR EACH ROW" in sql
    assert "WHEN (OLD.status IS DISTINCT FROM NEW.status)" in sql
    assert "NULLIF(current_setting('aads.audit_actor', true), '')" in sql
    assert "COALESCE(NULLIF(current_setting('aads.audit_source', true), ''), 'db_trigger')" in sql
    assert "NEW.tenant_id" in sql


def test_merge_trigger_suppresses_duplicate_app_insert():
    sql = _norm(_code(UP_PATH))
    assert "CREATE OR REPLACE FUNCTION goal_status_audit_merge() RETURNS trigger" in sql
    assert "BEFORE INSERT ON goal_status_audit" in sql
    assert "interval '5 seconds'" in sql
    assert "RETURN NULL" in sql
    # 애플리케이션이 준 actor/source 를 보존: source 는 트리거 기본값일 때만 덮는다.
    assert "COALESCE(NEW.actor, a.actor)" in sql
    assert "a.source = 'db_trigger'" in sql
    # 트리거 자신이 넣는 행은 병합하지 않는다 — 5초 안의 실제 반복 전이를 잃지 않게.
    assert "pg_trigger_depth() > 1" in sql


def test_triggers_never_abort_the_status_change():
    sql = _norm(_code(UP_PATH))
    assert sql.count("EXCEPTION WHEN others THEN") >= 2
    assert "RAISE WARNING" in sql


def test_up_migration_is_additive():
    upper = _code(UP_PATH).upper()
    for forbidden in ("DROP TABLE", "TRUNCATE", "DELETE FROM", "DROP COLUMN"):
        assert forbidden not in upper, forbidden


def test_down_drops_only_triggers_and_functions():
    sql = _norm(_code(DOWN_PATH))
    upper = sql.upper()
    assert "DROP TRIGGER IF EXISTS trg_goal_status_audit_capture ON goals" in sql
    assert "DROP TRIGGER IF EXISTS trg_goal_status_audit_merge ON goal_status_audit" in sql
    assert "DROP FUNCTION IF EXISTS goal_status_audit_capture()" in sql
    assert "DROP FUNCTION IF EXISTS goal_status_audit_merge()" in sql
    # 파괴적 롤백 회귀 방지: 감사 테이블과 데이터는 절대 건드리지 않는다.
    assert "DROP TABLE" not in upper
    assert "TRUNCATE" not in upper
    assert "DELETE FROM GOAL_STATUS_AUDIT" not in upper
    assert "DELETE" not in upper
