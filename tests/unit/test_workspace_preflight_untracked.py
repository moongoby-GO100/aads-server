"""Preflight excludes unrelated evidence while retaining owned config gates."""
import asyncio
from contextlib import asynccontextmanager

import pytest

from app.services import workspace_change_tracker as tracker


@pytest.mark.parametrize('case', ['snapshot', 'command_delta', 'unknown', 'runtime', 'ignored', 'invalid', 'valid'])
def test_finalize_ownership_and_config(monkeypatch, case):
    path = 'var/ops/evidence/timer.json' if case == 'runtime' else 'config.json'
    row = {'file_path': path, 'source_tool': 'write_remote_file', 'git_status': '??'}
    if case == 'snapshot':
        row['source_tool'] = 'git_status_snapshot'
    if case == 'command_delta':
        row['source_tool'] = 'run_remote_command'
    if case == 'unknown':
        row['owner'] = 'UNKNOWN-PREEXISTING-DIRTY'
    commands = []
    marks = []

    async def run(project, repo, command):
        commands.append(command)
        if 'status --porcelain' in command:
            return (' M ' if case in ('valid', 'invalid') else '?? ') + path
        if 'check-ignore' in command:
            return 'AADS_IGNORED' if case == 'ignored' else ''
        if command.startswith('cat --'):
            content = '{broken' if case == 'invalid' else '{"apply": true}'
            return f'[GO100 명령 실행 — exit=0]\n$ {command}\n{content}'
        if command == 'git rev-parse HEAD':
            return 'a' * 40
        if 'abbrev-ref' in command:
            return 'main'
        return ''

    async def mark(**kwargs):
        marks.append(kwargs)

    async def sensitive(*args):
        return {}

    @asynccontextmanager
    async def lock(*args, **kwargs):
        yield

    monkeypatch.setattr(tracker, '_run_git_command', run)
    monkeypatch.setattr(tracker, '_mark_group', mark)
    monkeypatch.setattr(tracker, '_sensitive_paths', sensitive)
    monkeypatch.setattr(tracker, 'git_project_lock', lock)
    result = asyncio.run(tracker._finalize_group(
        session_id='session', project='GO100', repo='kis-autotrade-v4', rows=[row]))
    if case == 'invalid':
        assert not result['ok']
        assert result['skipped_invalid_config'] == [path]
        assert marks[-1]['status'] == 'dirty'
    elif case == 'valid':
        assert result['ok'] and result['status'] == 'pushed'
        assert any(command.startswith('git commit --only') for command in commands)
        assert 'git push origin main' in commands
    else:
        assert result['ok'] and result['warnings']
        assert not any(command.startswith(('cat --', 'git add', 'git commit', 'git push')) for command in commands)


def test_remote_envelope_is_the_json_failure(monkeypatch):
    import json
    envelope = '[GO100 명령 실행 — exit=0]\n$ cat -- config.json\n{"apply": true}'
    with pytest.raises(json.JSONDecodeError, match='line 1 column 2'):
        json.loads(envelope)

    async def run(*args):
        return envelope
    monkeypatch.setattr(tracker, '_run_git_command', run)
    assert asyncio.run(tracker._config_syntax_errors('GO100', 'repo', ['config.json'])) == {}


def test_snapshot_does_not_claim_untracked_files(monkeypatch):
    recorded = []

    async def ensure():
        pass

    async def git(repo, *args):
        if args[0] == 'status':
            return '## main\n?? timer.json\n M owned.py\n?? var/ops/evidence/event.json'
        return 'a' * 40

    async def record(**kwargs):
        recorded.append(kwargs)

    monkeypatch.setattr(tracker, 'ensure_workspace_change_table', ensure)
    monkeypatch.setattr(tracker, '_run_local_git', git)
    monkeypatch.setattr(tracker, 'record_change', record)
    monkeypatch.setattr(tracker, 'get_pool', lambda: object())
    result = asyncio.run(tracker.sync_git_dirty_snapshot(
        session_id='session', project='GO100', repo='kis-autotrade-v4', mark_stale_clean=False))
    assert [row['file_path'] for row in recorded] == ['owned.py']
    assert result['ignored'] == 2
