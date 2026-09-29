"""deploy.sh must apply a release's new migrations through the ledger.

2026-09-29 866df31a: deploy.sh applied three hard-coded SQL files only, so
migrations/20260929_goal_pause_all_paths.sql shipped without its schema and
goal dispatch failed with "column paused_at does not exist".
"""

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[2]
RUNNER = ROOT / "scripts/apply_release_migrations.sh"
BASELINE = ROOT / "scripts/migrations_auto_apply_baseline.txt"
DEPLOY = ROOT / "deploy.sh"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def release(tmp_path):
    """A release tree plus a fake apply_migration.sh that records into the ledger file."""
    (tmp_path / "migrations").mkdir()
    (tmp_path / "scripts").mkdir()
    shutil.copy(RUNNER, tmp_path / "scripts/apply_release_migrations.sh")
    ledger = tmp_path / "ledger.txt"
    ledger.write_text("")
    calls = tmp_path / "calls.txt"
    fake_apply = tmp_path / "fake_apply.sh"
    fake_apply.write_text(
        "#!/bin/bash\n"
        "set -euo pipefail\n"
        'name="migrations/$(basename "$1")"\n'
        f'echo "$name" >> "{calls}"\n'
        'if grep -q FAIL_ME "$1"; then exit 1; fi\n'
        f'echo "$name|$(sha256sum "$1" | cut -d" " -f1)" >> "{ledger}"\n'
    )
    fake_apply.chmod(0o755)
    baseline = tmp_path / "baseline.txt"
    baseline.write_text("")

    class Release:
        root = tmp_path

        def write(self, name: str, sql: str) -> Path:
            path = tmp_path / "migrations" / name
            path.write_text(sql)
            return path

        def record(self, name: str, digest: str) -> None:
            with ledger.open("a") as fh:
                fh.write(f"{name}|{digest}\n")

        def hold(self, name: str) -> None:
            baseline.write_text(baseline.read_text() + f"migrations/{name}\n")

        def applied(self) -> list[str]:
            return calls.read_text().split() if calls.exists() else []

        def run(self, *args: str) -> subprocess.CompletedProcess:
            env = dict(
                os.environ,
                AADS_MIGRATION_LEDGER_FILE=str(ledger),
                AADS_APPLY_MIGRATION_BIN=str(fake_apply),
                AADS_MIGRATION_BASELINE_FILE=str(baseline),
            )
            return subprocess.run(
                ["bash", str(tmp_path / "scripts/apply_release_migrations.sh"), "--root", str(tmp_path), *args],
                env=env, capture_output=True, text=True, timeout=60,
            )

    return Release()


def test_new_migration_is_applied_in_filename_order(release):
    release.write("20260929_b.sql", "ALTER TABLE t ADD COLUMN IF NOT EXISTS b int;\n")
    release.write("20260929_a.sql", "ALTER TABLE t ADD COLUMN IF NOT EXISTS paused_at timestamptz;\n")

    result = release.run()

    assert result.returncode == 0, result.stderr
    assert release.applied() == ["migrations/20260929_a.sql", "migrations/20260929_b.sql"]
    assert "APPLY migrations/20260929_a.sql" in result.stdout
    assert "applied=2" in result.stdout


def test_applied_migration_is_not_reapplied(release):
    old = release.write("150_old.sql", "CREATE TABLE IF NOT EXISTS old(id int);\n")
    release.record("migrations/150_old.sql", sha(old))
    release.write("20260929_new.sql", "CREATE INDEX IF NOT EXISTS ix ON old(id);\n")

    first = release.run()
    second = release.run()

    assert first.returncode == 0 and second.returncode == 0
    assert release.applied() == ["migrations/20260929_new.sql"]
    assert "SKIP migrations/150_old.sql" in first.stdout
    assert "SKIP migrations/20260929_new.sql" in second.stdout
    assert "pending=0 applied=0" in second.stdout


def test_basename_ledger_row_counts_as_applied(release):
    # The 2026-09-29 manual recovery recorded the bare file name.
    path = release.write("20260929_goal_pause_all_paths.sql", "SELECT 1;\n")
    release.record("20260929_goal_pause_all_paths.sql", sha(path))

    result = release.run()

    assert result.returncode == 0
    assert release.applied() == []


def test_changed_applied_migration_is_reported_not_rerun(release):
    release.write("20260919_drift.sql", "SELECT 2;\n")
    release.record("migrations/20260919_drift.sql", "0" * 64)

    result = release.run()

    assert result.returncode == 0
    assert release.applied() == []
    assert "DRIFT migrations/20260919_drift.sql" in result.stdout


def test_verify_rollback_and_baseline_files_are_not_auto_applied(release):
    release.write("20260920_x.verify.sql", "SELECT 1;\n")
    release.write("20260919_hr_rollback.sql", "DROP TABLE hr;\n")
    release.write("20260919_manual.sql", "INSERT INTO seed VALUES (1);\n")
    release.hold("20260919_manual.sql")
    (release.root / "migrations/rollback").mkdir()
    (release.root / "migrations/rollback/20260929_a.down.sql").write_text("DROP TABLE a;\n")

    result = release.run()

    assert result.returncode == 0, result.stdout
    assert release.applied() == []
    assert "EXCLUDE migrations/20260920_x.verify.sql" in result.stdout
    assert "EXCLUDE migrations/20260919_hr_rollback.sql" in result.stdout
    assert "HOLD migrations/20260919_manual.sql" in result.stdout
    assert "down.sql" not in result.stdout


@pytest.mark.parametrize(
    "sql, reason",
    [
        ("DROP TABLE goals;\n", "DROP TABLE"),
        ("ALTER TABLE goals\n    DROP COLUMN paused_at;\n", "DROP COLUMN"),
        ("TRUNCATE goals;\n", "TRUNCATE"),
        ("DELETE FROM goals;\n", "DELETE without WHERE"),
        ("delete from goals where id = 1;\ndelete from goals;\n", "DELETE without WHERE"),
    ],
)
def test_destructive_sql_blocks_before_anything_is_applied(release, sql, reason):
    release.write("20260929_a_safe.sql", "ALTER TABLE t ADD COLUMN IF NOT EXISTS a int;\n")
    release.write("20260929_b_bad.sql", sql)

    for args in ((), ("--plan",)):
        result = release.run(*args)
        assert result.returncode == 4
        assert f"BLOCK-DESTRUCTIVE migrations/20260929_b_bad.sql: {reason}" in result.stdout
    # The safe file sorts first but must not be applied either.
    assert release.applied() == []


def test_non_destructive_look_alikes_pass(release):
    release.write(
        "20260929_ok.sql",
        "-- rollback: DROP TABLE x; TRUNCATE y;\n"
        "CREATE TABLE x (id int REFERENCES y(id) ON DELETE CASCADE);\n"
        "DELETE FROM x\n  WHERE id < 0;\n"
        "DROP INDEX IF EXISTS ix_old;\n",
    )

    result = release.run()

    assert result.returncode == 0, result.stdout
    assert release.applied() == ["migrations/20260929_ok.sql"]


def test_failed_migration_stops_later_ones(release):
    release.write("20260929_a.sql", "SELECT 'FAIL_ME';\n")
    release.write("20260929_b.sql", "SELECT 1;\n")

    result = release.run()

    assert result.returncode == 5
    assert "FAILED migrations/20260929_a.sql" in result.stderr
    assert release.applied() == ["migrations/20260929_a.sql"]


def test_plan_applies_nothing(release):
    release.write("20260929_a.sql", "SELECT 1;\n")

    result = release.run("--plan")

    assert result.returncode == 0
    assert "PENDING migrations/20260929_a.sql" in result.stdout
    assert release.applied() == []


def test_only_limits_to_named_files(release):
    release.write("150_obs.sql", "SELECT 1;\n")
    release.write("20260929_other.sql", "SELECT 2;\n")

    result = release.run("--only", "migrations/150_obs.sql")

    assert result.returncode == 0
    assert release.applied() == ["migrations/150_obs.sql"]


def test_baseline_entries_exist_and_never_hold_the_absorbed_files():
    entries = [
        line.split()[0]
        for line in BASELINE.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert entries
    for entry in entries:
        assert (ROOT / entry).is_file(), entry
    for absorbed in (
        "migrations/150_deploy_observability_v1.sql",
        "migrations/20260921_deploy_session_callbacks.sql",
        "migrations/20260922_next_step_dispatch_dedupe.sql",
        "migrations/20260929_goal_pause_all_paths.sql",
        "migrations/20260929_milestone_directive_draft_lookup.sql",
    ):
        assert absorbed not in entries


def test_deploy_applies_all_migrations_before_candidate_start():
    script = DEPLOY.read_text()

    # No hard-coded file is piped straight into psql any more.
    assert "psql -U aads -d aads -v ON_ERROR_STOP=1 -q \\\n        < " not in script
    assert "scripts/apply_release_migrations.sh" in script
    assert "git -C \"$COMPOSE_DIR\" archive --format=tar HEAD migrations" in script

    plan = script.index('apply_release_schema_migrations "schema_migration_plan" --plan')
    build = script.index('deploy_phase_start "build_candidate_image"')
    apply = script.index('apply_release_schema_migrations "schema_migrations"\n')
    start = script.index('deploy_phase_start "start_candidate_container"')
    assert plan < build < apply < start

    body = script[script.index("apply_release_schema_migrations() {"):]
    body = body[: body.index("\n}\n")]
    assert 'deploy_phase_end "$phase" "blocked" "$DEPLOY_LAST_FAIL_ERROR"' in body
    assert 'record_deploy "blocked"' in body
    assert "exit 1" in body


def test_observability_schema_goes_through_the_ledger():
    script = DEPLOY.read_text()
    body = script[script.index("ensure_deploy_observability_schema() {"):]
    body = body[: body.index("\n}\n")]

    assert "run_release_migrations" in body
    for name in (
        "migrations/150_deploy_observability_v1.sql",
        "migrations/20260921_deploy_session_callbacks.sql",
        "migrations/20260922_next_step_dispatch_dedupe.sql",
    ):
        assert f"--only {name}" in body
    assert "docker exec -i aads-postgres" not in body
