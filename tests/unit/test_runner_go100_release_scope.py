"""GO100 scope 파싱과 호출별 환경 격리를 검증한다 (jq 없이 실행)."""
import shlex

import pytest

from tests.unit.test_runner_go100_release_units import (
    ROOT, SOURCE, function, run, without_jq,
)


@pytest.mark.parametrize('instruction,expected', [
    ('GO100_RELEASE_SCOPE=background_only', 'background_only'),
    ('GO100_RELEASE_SCOPE: api_only', 'api_only'),
    ('GO100_RELEASE_SCOPE=full\nGO100_RELEASE_SCOPE: full', 'full'),
    ('문장 중간 GO100_RELEASE_SCOPE=background_only', ''),
    ('"GO100_RELEASE_SCOPE=full"\n`GO100_RELEASE_SCOPE=api_only`', ''),
])
def test_parse(instruction, expected):
    result = run('go100_release_scope_from_instruction ' + shlex.quote(instruction))
    assert result.returncode == 0, result.stderr
    assert result.stdout == expected


@pytest.mark.parametrize('instruction,error', [
    ('GO100_RELEASE_SCOPE=bad', 'invalid_go100_release_scope'),
    ('GO100_RELEASE_SCOPE=', 'invalid_go100_release_scope'),
    ('GO100_RELEASE_SCOPE=full\nGO100_RELEASE_SCOPE=api_only', 'invalid_go100_release_scope'),
    ('GO100_RELEASE_SCOPE=api_only\nGO100_RELEASE_TRADING_UNITS=card119', 'invalid_go100_release_combo'),
    ('GO100_RELEASE_SCOPE=background_only\nGO100_RELEASE_TRADING_UNITS=card119', 'invalid_go100_release_combo'),
])
def test_prevalidation_rejects(instruction, error):
    result = run('_fail_job() { echo "$3"; }; pre_validate job GO100 session ' + shlex.quote(instruction))
    assert result.returncode == 1
    assert result.stdout.strip() == error


@pytest.mark.parametrize('units', ['none', 'all'])
def test_api_only_allowed(units):
    result = run('''
resolve_project_workdir() { echo validated; return 1; }
fail_invalid_aads_target() { echo reached_workdir; }
_fail_job() { echo unexpected; }
pre_validate job GO100 session ''' + shlex.quote(f'GO100_RELEASE_SCOPE=api_only\nGO100_RELEASE_TRADING_UNITS={units}'))
    # Reaching workdir resolution proves the scope/units gate accepted the job.
    assert result.returncode == 1
    assert result.stdout.strip() == 'reached_workdir'


@pytest.mark.parametrize('remote', [False, True])
@pytest.mark.parametrize('scope,units', [('background_only', 'none'), ('full', 'card119'), ('', '')])
def test_queue_environment_and_job_isolation(tmp_path, remote, scope, units):
    script = tmp_path / 'deploy-go100.sh'
    script.write_text('''#!/bin/bash
case "$1" in
queue-release)
    printf '%s:%s\\n' "${GO100_RELEASE_SCOPE-unset}" "${GO100_RELEASE_TRADING_UNITS-unset}"
    ;;
esac
''')
    script.chmod(0o755)
    allowlist = next(line for line in SOURCE.splitlines() if line.startswith('GO100_DEPLOY_ENV_PASS_RE='))
    result = run(function('go100_deploy_remote_script') + '\n' + allowlist + f'''
GO100_DEPLOY_REMOTE_FUNCS=(go100_remote_queue_release)
GO100_DEPLOY_REMOTE_VARS=()
transport() {{ bash -s; }}
export -f transport
timeout() {{ shift 2; "$@"; }}
GO100_DEPLOY_SSH_BIN=transport
GO100_DEPLOY_SSH_HOST={'stub' if remote else "''"}
GO100_DEPLOY_SSH_CONNECT_TIMEOUT=1
GO100_DEPLOY_SSH_TIMEOUT=10
sha={'a' * 40}
expected=$(git hash-object --no-filters -- '{script}')
job() {{
    local _go100_release_scope='{scope}' _go100_release_units='{units}'
    go100_deploy_call go100_remote_queue_release '{tmp_path}' "$sha" "$expected" "$_go100_release_units" "$_go100_release_scope"
}}
job
printf 'daemon:%s:%s\\n' "${{GO100_RELEASE_SCOPE-unset}}" "${{GO100_RELEASE_TRADING_UNITS-unset}}"
export GO100_RELEASE_SCOPE=full GO100_RELEASE_TRADING_UNITS=all
go100_deploy_call go100_remote_queue_release '{tmp_path}' "$sha" "$expected" '' ''
''')
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [f'{scope or "unset"}:{units or "unset"}', 'daemon:unset:unset', 'unset:unset']


def test_wiring():
    assert (ROOT / 'scripts/pipeline-runner.sh.local').read_text() == SOURCE
    for sha in ('current_sha', 'rollback_sha'):
        assert f'go100_queue_backend_release "${sha}" "$worktree_dir" "$_go100_release_units" "$_go100_release_scope"' in SOURCE
    assert 'GO100_BACKEND_RELEASE_DEFERRED units=${_units:-unspecified} scope=${_scope:-unspecified}' in SOURCE


STUB_NO_SCOPE = '''#!/bin/bash
case "$1" in
queue-release)
    echo CALLED
    ;;
esac
'''


def _legacy_prelude(tmp_path, remote):
    """GO100_RELEASE_SCOPE 문자열이 없는 구판 deploy-go100.sh 를 커밋한 저장소와 원격 호출 환경."""
    import subprocess
    script = tmp_path / 'deploy-go100.sh'
    script.write_text(STUB_NO_SCOPE)
    script.chmod(0o755)
    env = {'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@t', 'GIT_COMMITTER_NAME': 't',
           'GIT_COMMITTER_EMAIL': 't@t', 'PATH': '/usr/bin:/bin:/usr/local/bin'}
    for cmd in (['init', '-q'], ['add', 'deploy-go100.sh'], ['commit', '-q', '-m', 'x']):
        subprocess.run(['git', '-C', str(tmp_path), *cmd], check=True, env=env, capture_output=True)
    sha = subprocess.run(['git', '-C', str(tmp_path), 'rev-parse', 'HEAD'], check=True,
                         capture_output=True, text=True).stdout.strip()
    allowlist = next(line for line in SOURCE.splitlines() if line.startswith('GO100_DEPLOY_ENV_PASS_RE='))
    prelude = function('go100_deploy_remote_script') + '\n' + allowlist + f'''
GO100_DEPLOY_REMOTE_FUNCS=(go100_remote_queue_release)
GO100_DEPLOY_REMOTE_VARS=()
transport() {{ bash -s; }}
export -f transport
timeout() {{ shift 2; "$@"; }}
GO100_DEPLOY_SSH_BIN=transport
GO100_DEPLOY_SSH_HOST={'stub' if remote else "''"}
GO100_DEPLOY_SSH_CONNECT_TIMEOUT=1
GO100_DEPLOY_SSH_TIMEOUT=10
GO100_REPO_DIR='{tmp_path}'
sha={sha}
expected=$(git -C '{tmp_path}' rev-parse "$sha:deploy-go100.sh")
'''
    return prelude


@pytest.mark.parametrize('remote', [False, True])
def test_unsupported_scope_fails_closed(tmp_path, remote):
    prelude = _legacy_prelude(tmp_path, remote)
    result = run(prelude + f'''
go100_deploy_call go100_remote_queue_release '{tmp_path}' "$sha" "$expected" none api_only
echo "rc=$?"
go100_deploy_call go100_remote_queue_release '{tmp_path}' "$sha" "$expected" none ''
echo "rc=$?"
''')
    assert result.returncode == 0, result.stderr
    # scope 지정: queue-release 미호출(CALLED 없음) + 고유 rc=7. scope 빈값: 기존 동작대로 호출.
    assert result.stdout.splitlines() == ['rc=7', 'CALLED', 'rc=0']


@pytest.mark.parametrize('remote', [False, True])
def test_backend_release_reports_scope_unsupported(tmp_path, remote):
    prelude = _legacy_prelude(tmp_path, remote)
    result = run(prelude + function('go100_queue_backend_release') + '''
log() { echo "LOG:$*"; }
post_to_chat() { echo "CHAT:$*"; }
job_id=j; session_id=s
go100_queue_backend_release "$sha" "$GO100_REPO_DIR" none api_only
echo "rc=$?"
go100_queue_backend_release "$sha" "$GO100_REPO_DIR" none ''
echo "rc=$?"
''')
    assert result.returncode == 0, result.stderr
    out = result.stdout.splitlines()
    logs = [x for x in out if x.startswith('LOG:')]
    chats = [x for x in out if x.startswith('CHAT:')]
    assert 'reason=release_scope_unsupported' in logs[0] and 'scope=api_only' in logs[0]
    assert 'release_scope_unsupported' in chats[0]
    assert 'release_queued' in logs[1] and 'release_scope_unsupported' not in logs[1]
    assert 'CALLED' in out
    assert out.count('CALLED') == 1
