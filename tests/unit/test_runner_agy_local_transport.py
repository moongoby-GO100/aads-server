"""Local tools must change the runner's worktree, never the central relay's."""
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
EXEC = ROOT / 'scripts/runner_antigravity_exec.py'
spec = importlib.util.spec_from_file_location('agy_local', EXEC)
agy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agy)


@pytest.fixture
def cli(tmp_path, monkeypatch):
    binary = tmp_path / 'agy'
    monkeypatch.setenv('AGY_BIN', str(binary))
    monkeypatch.setenv('ANTIGRAVITY_HOME_ROOT', str(tmp_path))
    creds = tmp_path / 'antigravity-cli/antigravity-oauth-token'
    creds.parent.mkdir()
    creds.write_text(json.dumps({'token': {'refresh_token': 'fixture', 'expiry': '2001-01-01T00:00:00Z'}}))
    binary.write_text('#!/usr/bin/env python3\nprint("unused")\n')
    binary.chmod(0o755)
    return binary


def test_admission_checks_local_auth_and_workspace(cli, tmp_path):
    assert agy.local_preflight() == (True, 'local_cli_ready')
    assert agy.local_preflight(str(tmp_path)) == (False, 'agy_workspace_not_local_worktree')
    (tmp_path / 'antigravity-cli/antigravity-oauth-token').unlink()
    assert agy.local_preflight() == (False, 'agy_login_required')


def test_missing_cli_is_not_a_model_attempt(monkeypatch):
    monkeypatch.setenv('AGY_BIN', '/does/not/exist/agy')
    assert agy.local_preflight() == (False, 'agy_binary_missing')


def _fake(cli, result, exit_code=0, tool=False):
    cli.write_text('#!/usr/bin/env python3\nimport json,sys,pathlib\n'
                   'payload=json.load(sys.stdin)\nassert payload["message"]["content"]=="edit locally"\n'
                   'pathlib.Path("proof.txt").write_text(str(pathlib.Path.cwd()))\n'
                   + ('print(json.dumps({"event":"step_update","step_update":{"step_type":"tool","conversation_id":"x","step_index":1,"state":"DONE"}}))\n' if tool else '')
                   + 'print(' + repr(json.dumps({'event': 'result', 'result': result})) + ')\n'
                   + 'sys.exit(%d)\n' % exit_code)


def test_local_tools_use_exact_cwd_and_success_receipt(cli, tmp_path, capsys):
    _fake(cli, {'status': 'SUCCESS', 'response': 'done'}, tool=True)
    assert agy.run_local('antigravity-pro', str(tmp_path), 'edit locally', 5) == 0
    out = capsys.readouterr()
    assert out.out == 'done\n'
    assert 'agy_model=gemini-4-argon-high mode=runner_local tool_calls=1' in out.err
    assert (tmp_path / 'proof.txt').read_text() == str(tmp_path)


@pytest.mark.parametrize('result,exit_code', [
    ({'status': 'SUCCESS', 'response': 'partial'}, 1),
    ({'status': 'ERROR', 'response': 'error'}, 0),
    ({'response': 'missing status'}, 0),
    ({'status': 'SUCCESS', 'response': ''}, 0),
    ({'status': 'SUCCESS', 'response': 'done', 'denied_actions': ['git push']}, 0),
])
def test_truncated_error_or_denied_result_is_not_success(cli, tmp_path, result, exit_code):
    _fake(cli, result, exit_code)
    assert agy.run_local('antigravity-pro', str(tmp_path), 'edit locally', 5) == 1


def test_timeout_kills_the_cli_group(cli, tmp_path):
    pidfile = tmp_path / 'child.pid'
    cli.write_text('#!/usr/bin/env python3\nimport subprocess,sys,time,pathlib\n'
                   'p=subprocess.Popen([sys.executable,"-c","import time; time.sleep(60)"])\n'
                   'pathlib.Path("child.pid").write_text(str(p.pid))\ntime.sleep(60)\n')
    assert agy.run_local('antigravity-pro', str(tmp_path), 'edit locally', 0.3) == 1
    pid = int(pidfile.read_text())
    # A killed orphan can remain a zombie until host init reaps it; it cannot run.
    stat = Path('/proc/%d/stat' % pid)
    assert not stat.exists() or stat.read_text().split(') ', 1)[1].startswith('Z ')


def test_runner_admission_failure_releases_lock_before_budget():
    for name in ('pipeline-runner.sh', 'pipeline-runner.sh.local'):
        source = (ROOT / 'scripts' / name).read_text()
        branch = source.split('elif [[ "$current_model" == antigravity:* ]]; then', 1)[1]
        admission = branch.index('--preflight-local --workspace')
        release = branch.index('_release_work_lock')
        charge = branch.index('retry_execution_charge_or_abort')
        assert admission < release < charge < branch.index('--local-cli --model')
        assert "!~ '^(antigravity" in source
