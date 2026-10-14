import re
from pathlib import Path

ROOT = Path(__file__).parents[2]
MIGRATION = ROOT / "migrations" / "20261014_obys_journal_source_clobe.sql"
ROLLBACK = ROOT / "migrations" / "rollback" / "20261014_obys_journal_source_clobe.down.sql"
GUARD = "to_regclass('public.yeoljeong_journal_vouchers') IS NULL"


def _code_lines(path: Path) -> str:
    sql = path.read_text(encoding="utf-8")
    return "\n".join(line.split("--", 1)[0] for line in sql.splitlines())


def _flat(path: Path) -> str:
    return re.sub(r"\s+", " ", _code_lines(path))


def test_migration_is_guarded_by_to_regclass_before_any_ddl():
    code = _code_lines(MIGRATION)

    guard = code.index(GUARD)
    assert "RETURN;" in code[guard:]
    first_ddl = min(code.index(token) for token in ("ALTER TABLE", "DROP CONSTRAINT", "VALIDATE"))
    assert guard < first_ddl


def test_migration_keeps_every_original_statement():
    flat = _flat(MIGRATION)

    assert "DROP CONSTRAINT IF EXISTS yeoljeong_journal_vouchers_source_type_check" in flat
    assert "ADD CONSTRAINT yeoljeong_journal_vouchers_source_type_check" in flat
    assert (
        "CHECK (source_type IN ('uploaded_ledger_row','manual_ledger_entry',"
        "'card_transaction','bank_transaction','clobe')) NOT VALID"
    ) in flat
    assert "VALIDATE CONSTRAINT yeoljeong_journal_vouchers_source_type_check" in flat
    assert flat.index("DROP CONSTRAINT") < flat.index("ADD CONSTRAINT") < flat.index("VALIDATE CONSTRAINT")


def test_migration_has_no_unguarded_top_level_table_statements():
    code = _code_lines(MIGRATION)
    outside = re.sub(r"DO \$mig\$.*?\$mig\$;", "", code, flags=re.S)

    assert "yeoljeong_journal_vouchers" not in outside
    assert outside.count("$mig$") == 0


def test_migration_stays_non_destructive():
    code = _code_lines(MIGRATION).upper()

    for token in ("DROP TABLE", "DROP COLUMN", "TRUNCATE", "DELETE FROM", "UPDATE YEOLJEONG"):
        assert token not in code


def test_rollback_is_guarded_and_keeps_clobe_row_check():
    code = _code_lines(ROLLBACK)
    flat = _flat(ROLLBACK)

    guard = code.index(GUARD)
    assert "RETURN;" in code[guard:]
    assert guard < code.index("source_type = 'clobe'")
    assert guard < code.index("DROP CONSTRAINT")
    assert "RAISE EXCEPTION" in code
    assert "'bank_transaction')) NOT VALID" in flat
    assert "'clobe'))" not in flat
    outside = re.sub(r"DO \$mig\$.*?\$mig\$;", "", code, flags=re.S)
    assert "yeoljeong_journal_vouchers" not in outside
