"""Server registration stays draft-only and fails closed on unverified files."""
import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from app.services import doc_auto_fix as fix
from app.services import doc_auto_resolve as res
from app.api import canonical_documents as docs


class Conn:
    def __init__(self):
        self.exists = False
        self.notes = []
        self.job = dict(job_id='runner-test', tenant_id='00000000-0000-0000-0000-000000000001',
                        project='GO100', status='done', commit_hash='a' * 40,
                        instruction=res.marker_line('doc-a', 'base', 'investigate'),
                        result_output='DOC_REVISION_FILE: docs/문서.md\nDOC_CHANGE_SUMMARY: 실측한 사실을 반영해 본문을 수정했습니다')

    @asynccontextmanager
    async def transaction(self):
        yield self

    async def fetchrow(self, sql, *args):
        if 'pipeline_jobs' in sql:
            return self.job
        if 'project_document_heads' in sql:
            return dict(id=1, kind='prd', generation=2)
        return dict(title='한글 문서', version='1.0.0')

    async def fetchval(self, sql, *args):
        return '1.0.0' if 'SELECT version' in sql else self.exists

    async def execute(self, sql, *args):
        self.notes.append(args[-1])

    @asynccontextmanager
    async def acquire(self):
        yield self


@pytest.fixture
def world(monkeypatch):
    conn = Conn()
    reader = AsyncMock(return_value='# 한글 본문')
    create = AsyncMock()
    monkeypatch.setattr(res, '_read_job_revision', reader)
    monkeypatch.setattr(docs, '_body_for_project', AsyncMock(return_value=('# 한글 본문', None, 'hash')))
    monkeypatch.setattr(docs, 'create_revision_in_tx', create)
    return conn, reader, create


def test_four_builders_delegate_registration():
    head, revision = {'document_key': 'doc-a'}, {'id': 'base', 'version': '1.0.0'}
    plan = fix.FixPlan(action='fix', reasons=['test'], round=1, fingerprint='a' * 16, feedback='검수')
    texts = [fix.build_instruction('AADS', head, revision, plan)]
    texts += [builder('AADS', head, revision, '검수') for builder in
              (res.build_revise_instruction, res.build_investigate_instruction, res.build_split_instruction)]
    for text in texts:
        assert 'DOC_REVISION_FILE:' in text and 'DOC_CHANGE_SUMMARY:' in text
        assert '정본 등록은 서버가' in text and '정본 테이블에 직접 쓰지 않는다' in text
        assert '판본을 등록한다' not in text and '판본을 정본에 등록한다' not in text
        assert fix.parse_markers(text) or res.parse_markers(text)


def test_register_once_and_retry_no_call(world):
    conn, reader, create = world
    asyncio.run(res.register_revision_from_job(conn, 'runner-test'))
    create.assert_awaited_once()
    body = create.await_args.args[-1]
    assert body.source_task_id == body.idempotency_key == 'runner-test'
    assert body.version == '1.0.1' and body.expected_generation == 2
    assert body.source_path == 'docs/문서.md'
    conn.exists = True
    create.reset_mock()
    reader.reset_mock()
    asyncio.run(res.register_revision_from_job(conn, 'runner-test'))
    create.assert_not_awaited()
    reader.assert_not_awaited()


@pytest.mark.parametrize('failure', ['read', 'missing', 'mismatch', 'summary', 'not_done', 'no_marker'])
def test_skips_without_registering(world, monkeypatch, failure):
    conn, reader, create = world
    if failure == 'read':
        reader.side_effect = ValueError('pushed_file_unreadable')
    elif failure == 'missing':
        conn.job['result_output'] = '완료'
    elif failure == 'mismatch':
        monkeypatch.setattr(docs, '_body_for_project', AsyncMock(return_value=('다른 내용', None, 'hash')))
    elif failure == 'summary':
        conn.job['result_output'] = conn.job['result_output'].split('DOC_CHANGE_SUMMARY:')[0] + 'DOC_CHANGE_SUMMARY: 짧음'
    elif failure == 'not_done':
        conn.job['status'] = 'running'
    else:
        conn.job['instruction'] = '일반 작업'
    asyncio.run(res.register_revision_from_job(conn, 'runner-test'))
    create.assert_not_awaited()
    if failure not in ('not_done', 'no_marker'):
        assert conn.notes and conn.notes[0].startswith('doc_register_skipped:')


def test_done_hook_is_present():
    from pathlib import Path
    source = Path('app/api/pipeline_runner.py').read_text()
    assert 'if status == "done":\n        from app.services.doc_auto_resolve import register_revision_from_job' in source


def test_split_registers_two_drafts(world):
    conn, _, create = world
    conn.job['instruction'] = res.marker_line('doc-a', 'base', 'split')
    conn.job['result_output'] += '\nDOC_REVISION_FILE: docs/미해결.md\nDOC_CHANGE_SUMMARY: 미해결 항목을 별도 문서로 분리했습니다'
    asyncio.run(res.register_revision_from_job(conn, 'runner-test'))
    assert create.await_count == 2
    assert [call.args[-1].document_key for call in create.await_args_list] == ['doc-a', 'doc-a-open-items']


def test_split_read_failure_registers_neither(world):
    conn, reader, create = world
    conn.job['instruction'] = res.marker_line('doc-a', 'base', 'split')
    conn.job['result_output'] += '\nDOC_REVISION_FILE: docs/미해결.md\nDOC_CHANGE_SUMMARY: 미해결 항목을 별도 문서로 분리했습니다'
    reader.side_effect = ['# 한글 본문', ValueError('pushed_file_unreadable')]
    asyncio.run(res.register_revision_from_job(conn, 'runner-test'))
    create.assert_not_awaited()
    assert conn.notes


def test_remote_reader_pins_commit_and_checks_pushed_ancestry(monkeypatch):
    process = AsyncMock()
    process.communicate.return_value = ('# 한글 본문'.encode(), b'')
    process.returncode = 0
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(res.asyncio, 'create_subprocess_exec', spawn)
    assert asyncio.run(res._read_job_revision('GO100', 'docs/문서.md', 'a' * 40)) == '# 한글 본문'
    command = spawn.await_args.args[-1]
    assert 'merge-base --is-ancestor ' + 'a' * 40 + ' origin/main' in command
    assert 'ls-tree -z ' + 'a' * 40 in command
    assert '/root/kis-autotrade-v4' in command
    assert '|| exit 4' in command


@pytest.mark.parametrize('project', ['ACCT', 'AADS'])
def test_local_project_job_reads_pinned_aads_repository(monkeypatch, project):
    conn = Conn()
    conn.job['project'] = project
    process = AsyncMock()
    process.communicate.return_value = ('# 한글 본문'.encode(), b'')
    process.returncode = 0
    spawn = AsyncMock(return_value=process)
    create = AsyncMock()
    monkeypatch.setattr(res.asyncio, 'create_subprocess_exec', spawn)
    monkeypatch.setattr(docs, '_body_for_project', AsyncMock(return_value=('# 한글 본문', None, 'hash')))
    monkeypatch.setattr(docs, 'create_revision_in_tx', create)

    asyncio.run(res.register_revision_from_job(conn, 'runner-test'))

    create.assert_awaited_once()
    assert not conn.notes
    args = spawn.await_args.args
    assert 'root@host.docker.internal' in args
    command = args[-1]
    assert '/root/aads/aads-server' in command
    assert 'merge-base --is-ancestor ' + 'a' * 40 + ' origin/main' in command
    assert 'ls-tree -z ' + 'a' * 40 in command
    assert '|| exit 4' in command


def test_unmapped_project_job_records_reason(monkeypatch):
    conn = Conn()
    conn.job['project'] = 'UNMAPPED_PROJECT'
    spawn = AsyncMock()
    create = AsyncMock()
    monkeypatch.setattr(res.asyncio, 'create_subprocess_exec', spawn)
    monkeypatch.setattr(docs, 'create_revision_in_tx', create)

    asyncio.run(res.register_revision_from_job(conn, 'runner-test'))

    assert conn.notes == ['doc_register_skipped:project_unmapped']
    spawn.assert_not_awaited()
    create.assert_not_awaited()
