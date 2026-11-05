"""release_migration_autoverify: 추가형만 자동 적용 대상으로 보고, 데이터 변경은 막는다."""
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("autoverify", ROOT / "scripts/release_migration_autoverify.py")
av = importlib.util.module_from_spec(spec)
spec.loader.exec_module(av)


def _classify(tmp_path, sql):
    p = tmp_path / "20990101_x.sql"
    p.write_text(sql)
    return av.classify_files([p])


@pytest.mark.parametrize("sql", [
    "CREATE TABLE IF NOT EXISTS t (id int primary key, note text DEFAULT 'a;b');",
    "ALTER TABLE milestones ADD COLUMN IF NOT EXISTS review_rejected_at timestamptz NULL;",
    "ALTER TABLE t ADD COLUMN a int, ADD COLUMN b text;",
    "CREATE INDEX IF NOT EXISTS ix ON t (a);",
    # 함수 본문의 DELETE 는 적용 시점에 실행되지 않는다.
    "CREATE OR REPLACE FUNCTION prune() RETURNS int LANGUAGE plpgsql AS $$ BEGIN DELETE FROM t WHERE a < 1; RETURN 0; END $$;",
    "DROP TRIGGER IF EXISTS trg ON t; CREATE TRIGGER trg BEFORE INSERT ON t FOR EACH ROW EXECUTE FUNCTION prune();",
    "BEGIN; COMMENT ON TABLE t IS 'x'; COMMIT;",
    "INSERT INTO t (id) VALUES (1) ON CONFLICT DO NOTHING;",
    "-- UPDATE t SET a = 1; 주석은 무시\nCREATE TABLE IF NOT EXISTS u (id int);",
])
def test_additive_sql_is_safe(tmp_path, sql):
    assert _classify(tmp_path, sql) == []


@pytest.mark.parametrize("sql,needle", [
    ("UPDATE goals SET progress = 1;", "허용 목록 밖"),
    ("DELETE FROM t WHERE id = 1;", "허용 목록 밖"),
    ("DROP TABLE t;", "허용 목록 밖"),
    ("ALTER TABLE t DROP COLUMN a;", "ALTER TABLE"),
    ("ALTER TABLE t ALTER COLUMN a TYPE bigint;", "ALTER TABLE"),
    ("ALTER TABLE t ADD COLUMN a int, DROP CONSTRAINT IF EXISTS c;", "ALTER TABLE"),
    ("DO $$ BEGIN UPDATE t SET a = 1; END $$;", "DO 블록"),
    ("INSERT INTO t (id) VALUES (1) ON CONFLICT (id) DO UPDATE SET id = 2;", "DO UPDATE"),
    ("CREATE INDEX CONCURRENTLY IF NOT EXISTS ix ON t (a);", "CONCURRENTLY"),
    ("TRUNCATE t;", "허용 목록 밖"),
])
def test_data_changing_sql_is_blocked(tmp_path, sql, needle):
    problems = _classify(tmp_path, sql)
    assert problems and needle in problems[0], problems


def test_trial_sql_wraps_in_rollback_and_drops_file_commit(tmp_path):
    p = tmp_path / "20990101_x.sql"
    p.write_text("BEGIN;\nCREATE TABLE IF NOT EXISTS t (id int);\nCOMMIT;\n")
    sql = av.trial_sql([p])
    lines = [x for x in sql.splitlines() if x.strip()]
    assert lines[0] == "BEGIN;" and lines[-1] == "ROLLBACK;"
    assert "COMMIT" not in sql
    assert "lock_timeout" in sql


def test_expected_objects_for_postcheck(tmp_path):
    p = tmp_path / "20990101_x.sql"
    p.write_text(
        "CREATE TABLE IF NOT EXISTS pipeline_job_checkpoints (id bigserial);\n"
        "ALTER TABLE milestones ADD COLUMN IF NOT EXISTS review_rejected_at timestamptz;\n"
        "CREATE OR REPLACE FUNCTION aads_prune() RETURNS int LANGUAGE sql AS $$ SELECT 1 $$;\n"
        "CREATE INDEX IF NOT EXISTS idx_pjc ON pipeline_job_checkpoints (id);\n")
    assert av.expected_objects([p]) == [
        ("table", "public", "pipeline_job_checkpoints", ""),
        ("column", "public", "milestones", "review_rejected_at"),
        ("function", "public", "aads_prune", ""),
        ("index", "public", "idx_pjc", ""),
    ]


def test_current_pending_migrations_are_additive():
    files = [ROOT / "migrations/20261104_milestone_review_rejected_at.sql",
             ROOT / "migrations/20261104_pipeline_job_checkpoints.sql"]
    files = [f for f in files if f.exists()]
    assert av.classify_files(files) == []
