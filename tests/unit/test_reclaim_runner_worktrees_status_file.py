"""Status fallback with isolated registered Git metadata and read-only Git probes.

No add/commit/worktree commands are executed: fixtures use Git plumbing and
write linked-worktree registration directly. The worktree-list probe is mocked.
"""
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'scripts/reclaim_runner_worktrees.sh'


def git(repo, *args, data=None):
    return subprocess.check_output(['git', '-C', str(repo), *args], input=data, text=True).strip()


@pytest.fixture
def sandbox(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    git(repo, 'init', '-q')
    tree = git(repo, 'mktree', data='')
    sha = git(repo, '-c', 'user.name=Test', '-c', 'user.email=test@example.com',
              'commit-tree', tree, data='fixture\n')
    git(repo, 'update-ref', 'refs/heads/master', sha)
    git(repo, 'update-ref', 'refs/remotes/origin/main', sha)
    paths = []
    for name in ('done', 'error', 'running'):
        path = tmp_path / f'aads-wt-runner-{name}'
        path.mkdir()
        meta = repo / '.git/worktrees' / name
        meta.mkdir(parents=True)
        (meta / 'commondir').write_text('../..\n')
        (meta / 'HEAD').write_text(sha + '\n')
        (meta / 'gitdir').write_text(str(path / '.git') + '\n')
        (path / '.git').write_text(f'gitdir: {meta}\n')
        paths.append(path)
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    for name in ('sed', 'tail', 'date', 'find', 'timeout', 'du', 'awk', 'stat', 'grep'):
        (bin_dir / name).symlink_to(shutil.which(name))
    listing = '\n'.join(f'worktree {p}\n' for p in paths)
    # Only the listing is mocked; rev-parse/status/merge-base use real Git.
    (bin_dir / 'git').write_text(
        '#!/bin/sh\ncase " $* " in *" worktree list "*) cat <<\'ROWS\'\n'
        + listing + '\nROWS\n;; *) exec ' + shutil.which('git') + ' "$@" ;; esac\n')
    (bin_dir / 'cat').symlink_to(shutil.which('cat'))
    for name in ('docker', 'psql', 'lsof'):
        (bin_dir / name).write_text('#!/bin/sh\nexit 1\n')
    for p in bin_dir.iterdir():
        if not p.is_symlink():
            p.chmod(0o755)
    status = tmp_path / 'statuses'
    status.write_text('runner-done|done\nrunner-error|error\nrunner-running|running\n')
    env = os.environ | {
        'PATH': str(bin_dir), 'RUNNER_WT_REPO': str(repo),
        'RUNNER_WT_GLOB': str(tmp_path / 'aads-wt-runner-*'),
        'RUNNER_WT_STATUS_FILE': str(status), 'RUNNER_WT_MAX_KEEP': '0',
        'RUNNER_WT_ENV_FILE': str(tmp_path / 'absent'),
        'RUNNER_TEMP_PG_REAP': '0', 'DRY_RUN': '1',
    }
    return paths, status, env, bin_dir


def run(sandbox, recent=False, **updates):
    paths, _, env, _ = sandbox
    for i, path in enumerate(paths):
        stamp = time.time() - (600 if recent else 30 * 3600) - i * 60
        os.utime(path / '.git', (stamp, stamp))
        os.utime(path, (stamp, stamp))
    result = subprocess.run(['/bin/bash', str(SCRIPT)], env=env | updates,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert all(p.exists() for p in paths)
    return result


def test_fallback_terminal_candidates(sandbox):
    result = run(sandbox)
    assert 'status_source=file' in result.stdout
    for name in ('done', 'error'):
        assert f'DRY_RUN candidate={sandbox[0][0].parent}/aads-wt-runner-{name}' in result.stdout
    assert f'DRY_RUN candidate={sandbox[0][2]}' not in result.stdout
    print(result.stdout)


@pytest.mark.parametrize('kind', ['missing', 'empty', 'stale', 'invalid'])
def test_unusable_snapshot_preserves_all(sandbox, kind):
    _, status, _, _ = sandbox
    if kind == 'missing':
        status.unlink()
    elif kind == 'stale':
        stamp = time.time() - 121 * 60
        os.utime(status, (stamp, stamp))
    else:
        status.write_text('broken\nrunner-done|done|extra\n' if kind == 'invalid' else '')
    result = run(sandbox)
    assert 'DRY_RUN candidate=' not in result.stdout
    assert 'reclaimed=0' in result.stdout
    assert 'status_source=none' in result.stdout
    if kind == 'invalid':
        assert 'invalid rows=2' in result.stderr


def test_cap_uses_snapshot(sandbox):
    result = run(sandbox, recent=True, RUNNER_WT_MAX_KEEP='1')
    assert f'DRY_RUN candidate={sandbox[0][1]}' in result.stdout
    assert 'over_cap=1' in result.stdout
    assert f'DRY_RUN candidate={sandbox[0][2]}' not in result.stdout


def test_mixed_rows_and_database_priority(sandbox):
    sandbox[1].write_text('bad\nrunner-done|done\n')
    result = run(sandbox)
    assert 'invalid rows=1' in result.stderr
    assert f'DRY_RUN candidate={sandbox[0][0]}' in result.stdout
    psql = sandbox[3] / 'psql'
    psql.write_text("#!/bin/sh\nprintf 'runner-done|running\\n'\n")
    result = run(sandbox)
    assert 'status_source=db' in result.stdout
    assert 'DRY_RUN candidate=' not in result.stdout


@pytest.mark.parametrize('failure', ['', 'query', 'scp', 'mv', 'invalid'])
def test_export_atomic_publish(tmp_path, failure):
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    remote = tmp_path / 'remote.txt'
    remote.write_text('old\n')
    snapshot = tmp_path / 'uploaded'
    calls = tmp_path / 'calls'
    for name in ('mktemp', 'rm', 'awk', 'timeout'):
        (bin_dir / name).symlink_to(shutil.which(name))
    scripts = {
        'psql': 'exit 1' if failure == 'query' else (
            "printf 'invalid\\n'" if failure == 'invalid' else "printf 'runner-one|done\\n'"),
        'scp': 'exit 1' if failure == 'scp' else f'cp "$5" "{snapshot}"',
        'ssh': f'''echo "$*" >> "{calls}"
case "$*" in
  *mktemp*) echo /var/lib/aads-runner/.job_status.ABC123 ;;
  *"mv -f"*) {'exit 1' if failure == 'mv' else f'cp "{snapshot}" "{remote}"'} ;;
esac''',
    }
    # scp options: -q -o BatchMode=yes -o ConnectTimeout=5 SOURCE DEST
    scripts['scp'] = scripts['scp'].replace('"$5"', '"$6"')
    (bin_dir / 'cp').symlink_to(shutil.which('cp'))
    for name, body in scripts.items():
        p = bin_dir / name
        p.write_text('#!/bin/sh\n' + body + '\n')
        p.chmod(0o755)
    env = os.environ | {'PATH': str(bin_dir), 'PG_CONTAINER': '',
                        'RUNNER_STATUS_TARGETS': 'root@test-host'}
    result = subprocess.run(['/bin/bash', str(ROOT / 'scripts/export_runner_job_status.sh')],
                            env=env, capture_output=True, text=True, timeout=10)
    assert (result.returncode == 0) == (failure == ''), result.stderr
    assert remote.read_text() == ('runner-one|done\n' if not failure else 'old\n')
    if failure in ('query', 'invalid'):
        assert not calls.exists()
