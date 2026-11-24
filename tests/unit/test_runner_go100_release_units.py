"""지시서 범위 파싱, 대기열 환경 전달 및 job 간 격리 회귀 검증."""
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SOURCE = (ROOT / 'scripts/pipeline-runner.sh').read_text()


@pytest.fixture(autouse=True)
def without_jq(tmp_path, monkeypatch):
    tools = tmp_path / 'bin'
    tools.mkdir()
    for name in ('bash', 'git', 'grep', 'timeout', 'date'):
        (tools / name).symlink_to(shutil.which(name))
    monkeypatch.setenv('PATH', str(tools))
    assert shutil.which('jq') is None


def function(name):
    return re.search(rf'^{name}\(\) [{{(]\n.*?^[}})]$', SOURCE, re.M | re.S)[0]


def run(body):
    library = '\n'.join(function(n) for n in (
        'go100_release_units_from_instruction', 'go100_release_scope_from_instruction', 'go100_remote_queue_release',
        'go100_deploy_is_remote', 'go100_deploy_call', 'pre_validate',
    ))
    return subprocess.run(['bash', '-c', library + '\n' + body],
                          capture_output=True, text=True,
                          env={'PATH': os.environ['PATH']}, timeout=10)


@pytest.mark.parametrize('instruction,expected', [
    ('GO100_RELEASE_TRADING_UNITS=none', 'none'),
    ('GO100_RELEASE_TRADING_UNITS: card119', 'card119'),
    ('GO100_RELEASE_TRADING_UNITS=all\nGO100_RELEASE_TRADING_UNITS: all', 'all'),
    ('문장 중간 GO100_RELEASE_TRADING_UNITS=none', ''),
    ('"GO100_RELEASE_TRADING_UNITS=none"\n`GO100_RELEASE_TRADING_UNITS=all`', ''),
])
def test_parse(instruction, expected):
    import shlex
    result = run('go100_release_units_from_instruction ' + shlex.quote(instruction))
    assert result.returncode == 0, result.stderr
    assert result.stdout == expected


@pytest.mark.parametrize('instruction', [
    'GO100_RELEASE_TRADING_UNITS=bad', 'GO100_RELEASE_TRADING_UNITS=',
    'GO100_RELEASE_TRADING_UNITS=none\nGO100_RELEASE_TRADING_UNITS=all',
    'GO100_RELEASE_TRADING_UNITS="none"',
])
def test_invalid_prevalidation(instruction):
    import shlex
    result = run('''
_fail_job() { echo "$3"; }
pre_validate job GO100 session ''' + shlex.quote(instruction))
    assert result.returncode == 1
    assert result.stdout.strip() == 'invalid_go100_release_units'


@pytest.mark.parametrize('units,expected', [('none', 'none'), ('', 'unset')])
def test_queue_environment(tmp_path, units, expected):
    # hash-object is read-only; the stub performs no deployment and needs no jq.
    script = tmp_path / 'deploy-go100.sh'
    script.write_text('''#!/bin/bash
case "$1" in
queue-release)
    printf '%s:%s' "${GO100_RELEASE_TRADING_UNITS-unset}" "$GO100_RELEASE_SHA"
    ;;
esac
''')
    script.chmod(0o755)
    result = run(f'''
export GO100_RELEASE_TRADING_UNITS=all
sha={'a' * 40}
expected=$(git hash-object --no-filters -- '{script}')
go100_remote_queue_release '{tmp_path}' "$sha" "$expected" '{units}'
''')
    assert result.returncode == 0, result.stderr
    assert result.stdout == f'{expected}:{"a" * 40}'


def test_job_environment_does_not_leak():
    result = run('''
GO100_DEPLOY_SSH_HOST=''
probe() { printf '%s\\n' "${GO100_RELEASE_TRADING_UNITS-unset}"; }
job() {
    local _go100_release_units=none
    go100_deploy_call probe
}
job
probe
go100_deploy_call probe
''')
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ['none', 'unset', 'unset']


def test_remote_job_environment_does_not_leak():
    # Exercise the shipped script, using bash as a fake SSH transport.
    result = run(function('go100_deploy_remote_script') + '''
GO100_DEPLOY_ENV_PASS_RE='^(GO100_RELEASE_TRADING_UNITS)$'
GO100_DEPLOY_REMOTE_FUNCS=(probe)
GO100_DEPLOY_REMOTE_VARS=()
probe() { printf '%s\\n' "${GO100_RELEASE_TRADING_UNITS-unset}"; }
transport() { bash -s; }
export -f transport
timeout() { shift 2; "$@"; }
GO100_DEPLOY_SSH_BIN=transport
GO100_DEPLOY_SSH_HOST=stub
GO100_DEPLOY_SSH_CONNECT_TIMEOUT=1
GO100_DEPLOY_SSH_TIMEOUT=10
job() {
    local _go100_release_units=none
    go100_deploy_call probe
}
job
probe
go100_deploy_call probe
''')
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ['none', 'unset', 'unset']


def test_wiring_and_mirror():
    assert (ROOT / 'scripts/pipeline-runner.sh.local').read_text() == SOURCE
    assert 'go100_queue_backend_release "$current_sha" "$worktree_dir" "$_go100_release_units"' in SOURCE
    assert 'go100_queue_backend_release "$rollback_sha" "$worktree_dir" "$_go100_release_units"' in SOURCE
    assert '"$_sha" "$_expected" "$_units" "$_scope" || _rc=$?' in SOURCE
    assert 'GO100_DEPLOY_COMPONENT|GO100_RELEASE_SCOPE|GO100_RELEASE_TRADING_UNITS)$' in SOURCE
