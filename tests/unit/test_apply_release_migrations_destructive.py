import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'scripts/apply_release_migrations.sh'


def reason(path):
    # Source only the gate functions; do not run the migration entrypoint.
    result = subprocess.run(
        ['bash', '-c', 'source /dev/stdin; destructive_reason "$1"', 'gate', str(path)],
        input=SCRIPT.read_text().split('# Prints the first destructive statement kind found, empty when none.', 1)[1]
        .split('if ! ledger=', 1)[0].replace('$(dirname "${BASH_SOURCE[0]}")', str(ROOT / 'scripts')),
        text=True, capture_output=True, check=True,
    )
    return result.stdout.strip()


@pytest.mark.parametrize(('sql', 'expected'), [
    ('CREATE TRIGGER t BEFORE UPDATE OR DELETE OR TRUNCATE ON x FOR EACH STATEMENT EXECUTE FUNCTION f();', ''),
    ('CREATE OR REPLACE CONSTRAINT TRIGGER t AFTER TRUNCATE ON x EXECUTE FUNCTION f();', ''),
    ('TRUNCATE TABLE x;', 'TRUNCATE'),
    ('truncate x;', 'TRUNCATE'),
    ('DROP TABLE x;', 'DROP TABLE'),
    ('-- TRUNCATE x\nSELECT 1;', ''),
    ('CREATE FUNCTION f() RETURNS void AS $$ BEGIN TRUNCATE x; END $$ LANGUAGE plpgsql;', 'TRUNCATE'),
    ('DO $$ BEGIN TRUNCATE x; END $$;', 'TRUNCATE'),
    ("DO $tag$ BEGIN EXECUTE 'TRUNCATE x'; END $tag$;", 'TRUNCATE'),
    ('-- TRUNCATE x', ''),
    ('DELETE FROM x;', 'DELETE without WHERE'),
    ('CREATE TRIGGER t BEFORE UPDATE ON x EXECUTE FUNCTION truncate();', 'TRUNCATE'),
    ('DO $$ BEGIN /* TRUNCATE x */ NULL; END $$;', ''),
    ("SELECT 'TRUNCATE x;'; TRUNCATE x;", 'TRUNCATE'),
    ('/* comment ; TRUNCATE x; */ TRUNCATE x;', 'TRUNCATE'),
    ('CREATE FUNCTION f() RETURNS void AS $$ BEGIN DROP TABLE x; END $$ LANGUAGE plpgsql;', 'DROP TABLE'),
])
def test_gate(tmp_path, sql, expected):
    path = tmp_path / 'migration.sql'
    path.write_text(sql)
    assert reason(path) == expected


def test_actual_trigger_migration():
    assert reason(ROOT / 'migrations/20261118_go100_deploy_approval.sql') == ''
