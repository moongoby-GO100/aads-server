import importlib.util
import os
from pathlib import Path


ROOT = Path(__file__).parents[2]
APPLY_SCRIPT = ROOT / "scripts/apply_migration.sh"
BACKFILL_SCRIPT = ROOT / "scripts/backfill_schema_migrations.py"


def load_backfill_module():
    spec = importlib.util.spec_from_file_location("backfill_schema_migrations", BACKFILL_SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_scripts_exist_and_are_executable():
    for script in (APPLY_SCRIPT, BACKFILL_SCRIPT):
        assert script.is_file()
        assert os.access(script, os.X_OK)


def test_apply_script_has_skip_and_checksum_mismatch_block():
    script = APPLY_SCRIPT.read_text()

    assert 'echo "SKIP $filename (same sha256)"' in script
    assert 'echo "BLOCK $filename: sha256 mismatch' in script
    assert "exit 3" in script
    assert "--force" in script


def test_collect_migration_files_scans_both_dirs_and_excludes_backups(tmp_path):
    module = load_backfill_module()
    migrations = tmp_path / "migrations"
    script_migrations = tmp_path / "scripts/migrations"
    migrations.mkdir()
    script_migrations.mkdir(parents=True)

    expected = [migrations / "001.sql", script_migrations / "002.sql"]
    for path in expected:
        path.write_text("SELECT 1;\n")
    (migrations / "003.sql.bak").write_text("SELECT 2;\n")
    (script_migrations / "004.sql.bak_aads").write_text("SELECT 3;\n")
    (script_migrations / "notes.txt").write_text("not sql\n")

    assert module.collect_migration_files(tmp_path) == expected
