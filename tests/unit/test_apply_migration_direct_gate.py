"""Direct migration gates run before any PostgreSQL access."""
import hashlib
import os
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]


def marker(paths):
    rows = ''.join(
        f'{path.resolve()}:{hashlib.sha256(path.read_bytes()).hexdigest()}\n'
        for path in paths
    )
    return hashlib.sha256(rows.encode()).hexdigest()


@pytest.fixture
def harness(tmp_path):
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    log = tmp_path / 'docker.log'
    docker = bin_dir / 'docker'
    docker.write_text(
        '#!/bin/bash\n'
        'printf "%s\\n" "$*" >> "$FAKE_DOCKER_LOG"\n'
        'if [[ "$*" == *"SELECT EXISTS"* ]]; then echo f; '
        'else cat >/dev/null; fi\n'
    )
    docker.chmod(0o755)
    env = dict(os.environ, PATH=f'{bin_dir}:{os.environ["PATH"]}',
               FAKE_DOCKER_LOG=str(log), PG_CONTAINER='nonexistent-test-pg')
    env.pop('AADS_MIGRATION_GATE_MANIFEST', None)

    def run(sqls, *, gate=None, options=()):
        paths = []
        for i, sql in enumerate(sqls):
            path = tmp_path / f'{i}.sql'
            path.write_text(sql)
            paths.append(path)
        if gate is not None:
            env['AADS_MIGRATION_GATE_MANIFEST'] = gate(paths)
        result = subprocess.run(
            ['bash', str(ROOT / 'scripts/apply_migration.sh'), *options,
             *map(str, paths)], env=env, capture_output=True, text=True,
            timeout=20,
        )
        return result, log
    return run


UNSAFE = 'ALTER TABLE demo DROP COLUMN obsolete;'
SAFE = 'CREATE TABLE IF NOT EXISTS demo (id integer);'


@pytest.mark.parametrize('options', [(), ('--force',)])
def test_unsafe_direct_blocks_before_docker(harness, options):
    result, log = harness([UNSAFE], options=options)
    assert result.returncode == 7
    assert 'BLOCK-CEO-APPROVAL' in result.stderr
    assert 'DROP' in result.stderr
    assert not log.exists()


def test_safe_direct_reaches_apply(harness):
    result, log = harness([SAFE])
    assert result.returncode == 0, result.stderr
    assert 'APPLIED' in result.stdout
    assert log.exists()


def test_matching_marker_allows_unsafe(harness):
    result, log = harness([UNSAFE], gate=marker)
    assert result.returncode == 0, result.stderr
    assert 'APPLIED' in result.stdout
    assert log.exists()


@pytest.mark.parametrize('gate', [lambda paths: 'bad',
                                  lambda paths: marker(paths[:1])])
def test_invalid_or_partial_marker_blocks_whole_batch(harness, gate):
    result, log = harness([SAFE, UNSAFE], gate=gate)
    assert result.returncode == 7
    assert not log.exists()


def test_content_change_invalidates_marker(harness):
    def stale_marker(paths):
        value = marker(paths)
        paths[0].write_text(UNSAFE + '\n-- changed bytes\n')
        return value
    result, log = harness([UNSAFE], gate=stale_marker)
    assert result.returncode == 7
    assert not log.exists()


def test_dry_run_does_not_block_unsafe(harness):
    result, log = harness([UNSAFE], options=('--dry-run',))
    assert result.returncode == 0, result.stderr
    assert 'DRY-RUN APPLY' in result.stdout
    assert 'exec -i' not in log.read_text()
