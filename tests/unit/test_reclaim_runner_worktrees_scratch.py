"""Opt-in scratch reclamation, exercised through the complete shell script."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import time

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / 'scripts/reclaim_runner_worktrees.sh'


@pytest.fixture
def scratch(tmp_path):
    root = tmp_path / 'scratch'
    root.mkdir()
    return root


def old(path, directory=True):
    if directory:
        path.mkdir()
        (path / 'evidence').write_text('old')
        age(path / 'evidence')
    else:
        path.write_text('old')
    age(path)
    return path


def age(path):
    stamp = time.time() - 25 * 3600
    os.utime(path, (stamp, stamp))


def run(root, **settings):
    env = {k: v for k, v in os.environ.items() if not k.startswith(('RUNNER_WT_', 'RUNNER_SCRATCH_'))}
    env.update({
        'RUNNER_WT_REPO': str(root),
        'RUNNER_WT_GLOB': str(root / 'no-worktrees-*'),
        'RUNNER_WT_ENV_FILE': str(root / 'absent.env'),
        'RUNNER_WT_ARCHIVE_DIR': str(root / 'absent-archive'),
        'RUNNER_TEMP_PG_REAP': '0',
        'PG_CONTAINER': '',
        'PGHOST': '/nonexistent-scratch-test-socket',
        'RUNNER_SCRATCH_ROOT': str(root),
        'RUNNER_SCRATCH_RECLAIM': '1',
    })
    env.update(settings)
    env = {k: v for k, v in env.items() if v is not None}
    result = subprocess.run(['bash', str(SCRIPT)], env=env, capture_output=True, text=True, timeout=40)
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_default_disabled(scratch):
    path = old(scratch / 'go100-unit-release-abc')
    assert '[reclaim-scratch]' not in run(scratch, RUNNER_SCRATCH_RECLAIM=None)
    assert path.exists()


def test_old_checkout_and_siblings(scratch):
    paths = [old(scratch / 'go100-unit-release-abc')]
    paths += [old(scratch / ('go100-unit-release-abc' + suffix), False) for suffix in ('.log', '-r2.log', '.rc')]
    output = run(scratch)
    assert not any(p.exists() for p in paths)
    assert 'removed=4 ' in output


def test_recent_nested_file(scratch):
    path = old(scratch / 'go100-review-abc')
    (path / 'evidence').write_text('recent')
    assert 'skipped_recent=1' in run(scratch)
    assert path.exists()


def test_prefix_mismatch(scratch):
    path = old(scratch / 'go100-memwave-cp-x')
    run(scratch)
    assert path.exists()


@pytest.mark.parametrize('name', ['de_x', 'go100-review-ckpt-x', 'go100-review-checkpoint-x', 'pytest-of-root', 'systemd-private-x'])
def test_always_protected(scratch, name):
    path = old(scratch / name)
    assert 'skipped_protect=1' in run(scratch, RUNNER_SCRATCH_PREFIXES='de_ go100- pytest- systemd-')
    assert path.exists()


@pytest.mark.parametrize('absolute', [False, True])
def test_explicit_protection(scratch, absolute):
    path = old(scratch / 'go100-review-abc')
    run(scratch, RUNNER_SCRATCH_PROTECT=str(path) if absolute else path.name)
    assert path.exists()


def test_symlink(scratch):
    target = old(scratch.parent / 'target')
    link = scratch / 'go100-review-link'
    link.symlink_to(target, target_is_directory=True)
    run(scratch)
    assert link.is_symlink() and target.exists()


def test_dry_run(scratch):
    path = old(scratch / 'go100-review-abc')
    output = run(scratch, RUNNER_SCRATCH_DRY_RUN='1')
    assert f'[reclaim-scratch] would-remove {path} ' in output
    assert path.exists() and 'removed=0 ' in output


def test_max_remove(scratch):
    paths = [old(scratch / f'go100-review-{i}') for i in range(3)]
    run(scratch, RUNNER_SCRATCH_MAX_REMOVE='1')
    assert sum(not p.exists() for p in paths) == 1


def test_inuse_cwd(scratch):
    path = old(scratch / 'go100-review-busy')
    # Cooperative shutdown via stdin avoids terminating a process in cleanup.
    child = subprocess.Popen(['python3', '-c', 'import sys; print("ready", flush=True); sys.stdin.read()'], cwd=path, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    try:
        assert child.stdout.readline() == b'ready\n'
        assert 'skipped_inuse=1' in run(scratch)
        assert path.exists()
    finally:
        child.communicate(timeout=5)


def test_invalid_settings_do_not_change_exit_code(scratch):
    path = old(scratch / 'go100-review-abc')
    assert '[reclaim-scratch] warning:' in run(scratch, RUNNER_SCRATCH_RETENTION_HOURS='bad')
    assert path.exists()


def test_retention_floor_and_recent_sidecar(scratch):
    path = old(scratch / 'go100-review-abc')
    stamp = time.time() - 23 * 3600
    os.utime(path / 'evidence', (stamp, stamp))
    log = old(scratch / 'go100-review-abc.log', False)
    os.utime(log, None)
    run(scratch, RUNNER_SCRATCH_RETENTION_HOURS='1')
    assert path.exists() and log.exists()


def test_unsafe_prefix_and_double_dot(scratch):
    path = old(scratch / 'go100-review-..escape')
    run(scratch)
    assert path.exists()
    assert 'warning:' in run(scratch, RUNNER_SCRATCH_PREFIXES='../')


@pytest.mark.parametrize('mode', ['fd', 'cmdline', 'environ'])
def test_inuse_other_references(scratch, mode):
    path = old(scratch / 'go100-review-reference')
    code = 'import sys; print("ready", flush=True); sys.stdin.read()'
    args = ['python3', '-c', code]
    env = os.environ.copy()
    if mode == 'fd':
        args[2] = 'import sys; f=open(sys.stdin.readline().strip()); print("ready", flush=True); sys.stdin.read()'
    elif mode == 'cmdline':
        args.append(str(path))
    else:
        env['SCRATCH_REFERENCE'] = str(path)
    child = subprocess.Popen(args, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    if mode == 'fd':
        child.stdin.write(os.fsencode(path / 'evidence') + b'\n')
        child.stdin.flush()
    try:
        assert child.stdout.readline() == b'ready\n'
        assert 'skipped_inuse=1' in run(scratch)
        assert path.exists()
    finally:
        child.communicate(timeout=5)


def test_sidecar_directories_are_preserved(scratch):
    paths = [old(scratch / ('go100-review-abc' + suffix)) for suffix in ('.log', '-r1.log', '.rc')]
    run(scratch)
    assert all(p.exists() for p in paths)
