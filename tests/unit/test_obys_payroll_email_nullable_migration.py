import re
from pathlib import Path

MIGRATION = (
    Path(__file__).parents[2]
    / "migrations"
    / "20261006_obys_payroll_email_nullable.sql"
)


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def _code_lines(sql: str) -> str:
    return "\n".join(line.split("--", 1)[0] for line in sql.splitlines())


def test_migration_is_guarded_by_to_regclass_before_any_ddl():
    code = _code_lines(_sql())

    guard = code.index("to_regclass('public.yeoljeong_payroll_statements') IS NULL")
    assert "RETURN;" in code[guard:]
    first_ddl = min(
        code.index(token)
        for token in ("ALTER TABLE", "UPDATE yeoljeong", "CREATE INDEX")
    )
    assert guard < first_ddl


def test_migration_keeps_every_original_statement():
    sql = _sql()
    flat = re.sub(r"\s+", " ", sql)

    assert "ALTER COLUMN employee_email DROP NOT NULL" in flat
    assert "SET employee_email = NULL WHERE btrim(employee_email) = ''" in flat
    assert "SET employee_email = lower(btrim(employee_email))" in flat
    assert "ADD CONSTRAINT ck_yf_payroll_email_normalized" in flat
    assert (
        "CHECK (employee_email IS NULL OR (employee_email = lower(btrim(employee_email))"
        " AND employee_email <> ''))"
    ) in flat
    assert "NOT VALID" in flat
    assert "VALIDATE CONSTRAINT ck_yf_payroll_email_normalized" in flat
    assert "CREATE INDEX IF NOT EXISTS idx_yf_payroll_email_pending" in flat
    assert "(business_id, employee_name)" in flat
    assert "WHERE employee_email IS NULL AND deleted_at IS NULL" in flat


def test_migration_has_no_unguarded_top_level_table_statements():
    code = _code_lines(_sql())
    outside = re.sub(r"DO \$mig\$.*?\$mig\$;", "", code, flags=re.S)

    assert "yeoljeong_payroll_statements" not in outside
    assert outside.count("$mig$") == 0


def test_migration_stays_non_destructive():
    code = _code_lines(_sql()).upper()

    for token in ("DROP TABLE", "DROP COLUMN", "TRUNCATE", "DELETE FROM"):
        assert token not in code
