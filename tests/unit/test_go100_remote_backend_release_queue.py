"""GO100 원격 백엔드 대기열: 실제 SSH/서비스 호출 없이 실행한다."""
import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TEXT = (ROOT / 'scripts/pipeline-runner.sh').read_text()
SHA = 'a' * 40
BLOB = 'b' * 40


def function(name):
    return re.search(r'^' + name + r'\(\) \{\n.*?^\}', TEXT, re.M | re.S)[0]


def run(body):
    return subprocess.run(['bash', '-c', body], text=True, capture_output=True,
                          env={**os.environ}, timeout=10)


@pytest.mark.parametrize('rc', [0, 1, 124, 137, 255])
def test_dispatch_queue_result_and_no_restart(rc):
    start = TEXT.index('            local _go100_be_changed="true"')
    end = TEXT.index('            # GO100 Frontend:', start)
    api = TEXT[start:end]
    helpers = '\n'.join(function(n) for n in (
        'go100_deploy_is_remote', 'go100_queue_backend_release'))
    r = run(helpers + f'''
GO100_DEPLOY_SSH_HOST=mock GO100_REPO_DIR=/runtime
session_id=s job_id=j
log() {{ echo "$*"; }}
post_to_chat() {{ echo "CHAT $*"; }}
git() {{ echo {BLOB}; }}
go100_deploy_call() {{ echo "CALL $*"; return {rc}; }}
go100_restart_freeze_window() {{ echo freeze; return 0; }}
runit() {{
local _deploy_files_ok=true _deploy_files=backend/app.py _pre_sha=x current_sha={SHA}
local worktree_dir=/source _deploy_deferred="" _build_fail=""
{api}
echo "FAIL=$_build_fail DEFERRED=$_deploy_deferred"
}}
runit
''')
    assert r.returncode == 0, r.stderr
    assert f'CALL go100_remote_queue_release /runtime {SHA} {BLOB}' in r.stdout
    label = f'release_queued({SHA})' if rc == 0 else 'release_queue_unavailable'
    assert f'FAIL= DEFERRED=go100-api:{label}' in r.stdout
    assert 'systemctl' not in r.stdout and 'RESTART go100' not in r.stdout
    assert '릴리스 대기열/수동 blue-green' in r.stdout


@pytest.mark.parametrize('matching,mode,expected', [(True, True, 0), (False, True, 5), (True, False, 6)])
def test_remote_script_gate(tmp_path, matching, mode, expected):
    script = tmp_path / 'deploy-go100.sh'
    script.write_text('#!/bin/bash\ncase "$1" in\n' +
                      ('queue-release) echo "QUEUED $GO100_RELEASE_SHA $1";;\n' if mode else '') +
                      '*) exit 9;;\nesac\n')
    script.chmod(0o755)
    actual = BLOB if matching else 'c' * 40
    r = run(function('go100_remote_queue_release') + f'''
git() {{ echo {actual}; }}
go100_remote_queue_release '{tmp_path}' {SHA} {BLOB}
''')
    assert r.returncode == expected, r.stderr
    assert ('QUEUED' in r.stdout) == (expected == 0)
    if expected == 0:
        assert f'QUEUED {SHA} queue-release' in r.stdout


def test_rollback_never_restarts_and_records_not_done():
    start = TEXT.index('                GO100)\n                    if go100_deploy_is_remote; then')
    end = TEXT.index('                    else\n', start)
    remote = TEXT[start:end].split('then\n', 1)[1]
    r = run(f'''
go100_queue_backend_release() {{ echo "QUEUE $1"; GO100_RELEASE_QUEUE_LABEL="go100-api:release_queued($1)"; }}
log() {{ echo "$*"; }}
rollback_sha={SHA} worktree_dir=/source job_id=j
runit() {{ {remote}
}}
runit
''')
    assert r.returncode == 0, r.stderr
    assert f'QUEUE {SHA}' in r.stdout
    assert 'ROLLBACK_REMOTE_RESTART_NOT_DONE' in r.stdout
    assert 'systemctl' not in remote and 'go100_remote_api_restart' not in remote


def test_blob_mismatch_never_invokes_queue_command(tmp_path):
    script = tmp_path / 'deploy-go100.sh'
    script.write_text('#!/bin/bash\ncase "$1" in\nqueue-release) echo REGISTERED;;\nesac\n')
    script.chmod(0o755)
    helpers = '\n'.join(function(n) for n in (
        'go100_remote_queue_release', 'go100_queue_backend_release'))
    r = run(helpers + f'''
GO100_REPO_DIR='{tmp_path}'
session_id=s job_id=j
git() {{
    if [[ "$*" == *rev-parse* ]]; then echo {BLOB}; else echo {'c' * 40}; fi
}}
log() {{ echo "$*"; }}
post_to_chat() {{ echo "CHAT $*"; }}
go100_deploy_call() {{ "$@"; }}
go100_queue_backend_release {SHA} /source
echo "LABEL=$GO100_RELEASE_QUEUE_LABEL"
''')
    assert r.returncode == 0, r.stderr
    assert 'LABEL=go100-api:release_queue_unavailable' in r.stdout
    assert 'REGISTERED' not in r.stdout
    assert 'CHAT' in r.stdout


def test_deferred_terminal_is_done_without_deployed_timestamp():
    start = TEXT.index('    elif [[ -n "$_deploy_deferred" ]]; then')
    end = TEXT.index('    else\n', start)
    deferred = TEXT[start:end]
    assert "status='done'" in deferred
    assert 'deployed_at=NULL' in deferred
    assert '배포 완료' not in deferred


def test_runner_copies_are_identical():
    assert (ROOT / 'scripts/pipeline-runner.sh').read_bytes() == (
        ROOT / 'scripts/pipeline-runner.sh.local').read_bytes()
