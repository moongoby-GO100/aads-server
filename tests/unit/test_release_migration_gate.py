"""로컬 격리 셸에서 승인 바인딩과 plan 분류를 검증한다."""
import hashlib
import os

import pytest

from tests.unit import test_deploy_release_migrations as migration_tests

RUNNER = migration_tests.RUNNER
release = migration_tests.release


@pytest.fixture
def gated(release):
    path = release.root / 'scripts/apply_release_migrations.sh'
    # Keep the fixture ledger transport and deny approval by default.
    path.write_text(path.read_text().replace('    return 0\n}', '    return 1\n}', 1))
    return release


def test_pending_without_approval_stops(gated):
    gated.write('001.sql', 'SELECT 1;')
    result = gated.run()
    assert result.returncode == 7
    assert 'BLOCK-CEO-APPROVAL' in result.stdout
    assert 'migrations/001.sql|' in result.stdout
    assert not gated.applied()


def test_zero_pending_passes_without_source(gated):
    assert gated.run().returncode == 0


def stub_source(gated, digest, *, fail=False):
    """Simulate a future trusted DB verifier in a disposable script, never via env."""
    path = gated.root / 'scripts/apply_release_migrations.sh'
    script = path.read_text()
    replacement = 'return 1' if fail else (
        '[[ "$1" == "' + 'a' * 40 + '" && "$2" == '
        "'migrations/001.sql|" + digest + "'" + "$'\\n' ]]"
    )
    path.write_text(script.replace(
        '    return 1\n}', '    ' + replacement + '\n}', 1,
    ))


def test_trusted_source_matching_sha_and_manifest_pass(gated, monkeypatch):
    path = gated.write('001.sql', 'SELECT 1;')
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    monkeypatch.setenv('AADS_RELEASE_SHA', 'a' * 40)
    stub_source(gated, digest)
    assert gated.run().returncode == 0
    assert gated.applied() == ['migrations/001.sql']


@pytest.mark.parametrize('change', ['file', 'release'])
def test_changed_approval_binding_stops(gated, monkeypatch, change):
    path = gated.write('001.sql', 'SELECT 1;')
    stub_source(gated, hashlib.sha256(path.read_bytes()).hexdigest())
    monkeypatch.setenv('AADS_RELEASE_SHA', ('b' if change == 'release' else 'a') * 40)
    if change == 'file':
        path.write_text('SELECT 2;')
    assert gated.run().returncode == 7
    assert not gated.applied()


def test_source_failure_stops(gated):
    gated.write('001.sql', 'SELECT 1;')
    stub_source(gated, '', fail=True)
    assert gated.run().returncode == 7
    assert not gated.applied()


def test_environment_cannot_self_approve(gated, monkeypatch):
    gated.write('001.sql', 'SELECT 1;')
    monkeypatch.setenv('AADS_MIGRATION_CEO_APPROVED', 'true')
    assert gated.run().returncode == 7


def fake_catalog(gated, monkeypatch, body):
    binary = gated.root / 'docker'
    binary.write_text('#!/bin/bash\n' + body)
    binary.chmod(0o755)
    monkeypatch.setenv('PATH', str(gated.root) + os.pathsep + os.environ['PATH'])


def test_plan_reports_unledgered_columns_and_constraints(gated, monkeypatch):
    gated.write('001.sql', '''ALTER TABLE public.items
      ADD COLUMN IF NOT EXISTS tenant_id uuid,
      ADD CONSTRAINT items_unique UNIQUE (tenant_id);''')
    fake_catalog(gated, monkeypatch, '''case "$*" in
      *"SELECT count(*) FROM information_schema.columns"*) echo 1;;
      *"SELECT count(*) FROM information_schema.table_constraints"*) echo 1;;
      *) exit 99;; esac\n''')
    result = gated.run('--plan')
    assert result.returncode == 0, result.stderr
    assert 'unledgered COLUMN public.items.tenant_id already exists' in result.stdout
    assert 'unledgered CONSTRAINT public.items.items_unique already exists' in result.stdout
    assert 'PENDING migrations/001.sql' in result.stdout
    assert not gated.applied()


def test_catalog_failure_does_not_report_clean_plan(gated, monkeypatch):
    gated.write('001.sql', 'ALTER TABLE items ADD COLUMN tenant_id uuid;')
    fake_catalog(gated, monkeypatch, 'exit 1\n')
    assert gated.run('--plan').returncode == 8
    assert not gated.applied()
