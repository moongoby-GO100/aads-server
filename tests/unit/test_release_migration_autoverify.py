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
             ROOT / "migrations/deferred/20261104_pipeline_job_checkpoints.sql"]
    files = [f for f in files if f.exists()]
    assert av.classify_files(files) == []


@pytest.mark.parametrize("action", [
    "ALTER COLUMN model SET DEFAULT NULL",
    "ALTER COLUMN model SET DEFAULT 0",
    "ALTER COLUMN model SET DEFAULT -1.25e+2",
    "ALTER COLUMN model SET DEFAULT TRUE",
    "ALTER COLUMN model SET DEFAULT FALSE",
    "ALTER COLUMN model SET DEFAULT now()",
    "ALTER COLUMN model SET DEFAULT CURRENT_TIMESTAMP",
    "ALTER COLUMN model SET DEFAULT 'x'::character varying",
    "ALTER COLUMN model SET DEFAULT 'it''s, (fine)'::text",
    "ALTER COLUMN model DROP DEFAULT",
    'ALTER COLUMN "model" DROP DEFAULT',
    "ALTER COLUMN model SET DEFAULT NULL, ALTER COLUMN b DROP DEFAULT",
    "ADD COLUMN b text, ALTER COLUMN model SET DEFAULT NULL",
])
def test_default_only_changes_are_safe(tmp_path, action):
    assert _classify(tmp_path, f"ALTER TABLE pipeline_jobs {action};") == []


@pytest.mark.parametrize("action", [
    "ADD COLUMN x text DEFAULT $$a, ALTER COLUMN b DROP DEFAULT,$$, DROP COLUMN c",
    "ADD COLUMN x text DEFAULT $t$a, ALTER COLUMN b SET DEFAULT NULL,$t$, DROP COLUMN c",
    "ADD COLUMN x text DEFAULT 'a', ALTER COLUMN b DROP DEFAULT, DROP COLUMN c",
    "ALTER COLUMN model SET NOT NULL",
    "ALTER COLUMN model TYPE text",
    "ALTER COLUMN model SET DATA TYPE text",
    "ALTER COLUMN model DROP NOT NULL",
    "ALTER COLUMN model TYPE text USING model::text",
    "ALTER COLUMN model SET DEFAULT (SELECT 1)",
    "ALTER COLUMN model SET DEFAULT gen_x()",
    "ALTER COLUMN model SET DEFAULT NULL, DROP COLUMN b",
    "ALTER model SET DEFAULT NULL",
    "ALTER COLUMN model SET DEFAULT NULL::text",
    "ALTER COLUMN model SET DEFAULT 0 + 1",
    "ALTER COLUMN model SET DEFAULT CURRENT_TIMESTAMP AT TIME ZONE 'UTC'",
    "ALTER COLUMN model SET DEFAULT 'a;b'",
    "ALTER COLUMN model SET DEFAULT 'unterminated",
    "ALTER COLUMN model SET DEFAULT $$x$$",
    "ALTER COLUMN model SET DEFAULT NULL; DELETE FROM pipeline_jobs",
    "ALTER COLUMN model SET DEFAULT 'a'::text USING model",
    "ALTER COLUMN model SET DEFAULT 'a'::using",
    "ALTER COLUMN model SET DEFAULT 'a\\b'",
])
def test_unsafe_default_or_column_changes_are_blocked(tmp_path, action):
    assert _classify(tmp_path, f"ALTER TABLE pipeline_jobs {action};")


def test_model_default_null_migration_is_safe():
    assert av.classify_files([ROOT / "migrations/20261106_pipeline_jobs_model_default_null.sql"]) == []
