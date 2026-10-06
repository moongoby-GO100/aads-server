"""Execute startup's actual SQL predicates against remote/local row fixtures.

The function is extracted from the real service to avoid application startup.
Only DB transport is replaced; sqlite evaluates the captured WHERE predicates.
No production database, remote process, or application service is used.
"""
from __future__ import annotations

import ast
import asyncio
from contextlib import asynccontextmanager
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import sys
import types

import pytest


SERVICE = Path(__file__).resolve().parents[2] / 'app/services/pipeline_runner_service.py'


class CaptureConnection:
    def __init__(self):
        self.sql = []

    async def fetch(self, sql, *args):
        self.sql.append(sql)
        return []

    async def execute(self, sql, *args):
        self.sql.append(sql)
        return 'UPDATE 0'

    async def fetchval(self, sql, *args):
        return 0


class StartupQueries(list):
    def __init__(self, phase0, all_sql):
        super().__init__(phase0)
        self.all_sql = all_sql


@pytest.fixture
def startup_queries(monkeypatch):
    text = SERVICE.read_text()
    wanted = {
        '_ORPHAN_REQUEUE_MARK', '_ORPHAN_REQUEUE_ELIGIBLE_SQL',
        '_ORPHAN_REQUEUE_COUNT_SQL', '_API_STARTUP_OWNERSHIP_SQL',
        '_orphan_requeue_max', 'recover_interrupted_jobs',
    }
    nodes = []
    for node in ast.parse(text).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in wanted:
            nodes.append(node)
        elif isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id in wanted for t in node.targets
        ):
            nodes.append(node)

    conn = CaptureConnection()

    class Pool:
        @asynccontextmanager
        async def acquire(self):
            yield conn

    async def noop(*args, **kwargs):
        pass

    db = types.ModuleType('app.core.db_pool')
    db.get_pool = lambda: Pool()
    app = types.ModuleType('app')
    core = types.ModuleType('app.core')
    app.core = core
    core.db_pool = db
    for key, module in [('app', app), ('app.core', core), ('app.core.db_pool', db)]:
        monkeypatch.setitem(sys.modules, key, module)
    monkeypatch.delenv('AADS_ORPHAN_REQUEUE_ENABLED', raising=False)
    monkeypatch.delenv('AADS_ORPHAN_REQUEUE_MAX', raising=False)
    scope = {'os': os, 'json': json, 'asyncio': asyncio,
             'logger': logging.getLogger('ownership-regression'),
             '_reconcile_job_goal_links': noop}
    tree = ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[]))
    exec(compile(tree, str(SERVICE), 'exec'), scope)
    asyncio.run(scope['recover_interrupted_jobs']())
    selected = []
    for sql in conn.sql:
        if "SET status = 'queued'" in sql or 'as requeue_exhausted' in sql or (
            "SET status = 'error', phase = 'error'" in sql
        ):
            selected.append(sql)
    assert len(selected) == 3, 'capture requeue/select/error predicates from actual startup'
    return StartupQueries(selected, conn.sql)


def matching_jobs(sql, rows, heartbeat):
    # SET/RETURNING are deliberately never run. Execute only the actual WHERE
    # with equivalent parameter and timestamp syntax for this fixed fixture.
    where = sql.split('WHERE ', 1)[1].split('RETURNING ', 1)[0]
    where = where.split('ORDER BY ', 1)[0]
    where = re.sub(r'--[^\n]*', '', where)
    where = where.replace("now() - interval '15 minutes'", "'2026-10-06 06:45:00'")
    where = where.replace('$1::int', ':requeue_max')
    db = sqlite3.connect(':memory:')
    try:
        db.execute('CREATE TABLE pipeline_jobs(job_id TEXT, status TEXT, phase TEXT, '
                   'commit_hash TEXT, runner_host TEXT, runner_pid INTEGER, review_feedback TEXT)')
        db.execute('CREATE TABLE pipeline_runner_hosts(host TEXT, last_seen_at TEXT)')
        db.executemany('INSERT INTO pipeline_jobs VALUES(?,?,?,?,?,?,?)', rows)
        if heartbeat is not None:
            db.execute('INSERT INTO pipeline_runner_hosts VALUES(?,?)', ('contabo14', heartbeat))
        return [r[0] for r in db.execute(
            'SELECT job_id FROM pipeline_jobs WHERE ' + where, {'requeue_max': 2}
        )]
    finally:
        db.close()


@pytest.mark.parametrize('heartbeat', [None, '2026-10-06 05:00:00', '2026-10-06 07:00:00'])
@pytest.mark.parametrize('pid', [None, 223175])
@pytest.mark.parametrize('phase', ['claude_code_work', 'ai_review', 'deploying'])
def test_external_execution_not_mutated_even_with_unknown_liveness(startup_queries, heartbeat, pid, phase):
    row = ('runner-live', 'running', phase, None, 'contabo14', pid, '')
    for sql in startup_queries:
        assert matching_jobs(sql, [row], heartbeat) == []


@pytest.mark.parametrize('phase', ['claude_code_work', 'ai_review', 'deploying'])
def test_api_local_recovery_still_matches_legacy_error_path(startup_queries, phase):
    row = ('api-owned', 'running', phase, None, None, None, '')
    assert matching_jobs(startup_queries[0], [row], None) == []
    assert matching_jobs(startup_queries[1], [row], None) == ['api-owned']
    assert matching_jobs(startup_queries[2], [row], None) == ['api-owned']


@pytest.mark.parametrize('status,phase', [
    ('running', 'claude_code_detached'), ('running', 'restarting'),
    ('queued', 'queued'), ('awaiting_approval', 'awaiting_approval'), ('done', 'done'),
])
def test_existing_out_of_phase_states_are_preserved(startup_queries, status, phase):
    row = ('already-handled', status, phase, None, None, None, '')
    for sql in startup_queries:
        assert matching_jobs(sql, [row], None) == []


def test_mixed_remote_and_api_owned_jobs_recover_only_api_local(startup_queries):
    rows = [
        ('remote-running', 'running', 'claude_code_work', None, 'contabo14', 223175, ''),
        ('remote-unregistered', 'running', 'claude_code_work', None, 'missing-host', None, ''),
        ('api-interrupted', 'running', 'claude_code_work', None, None, None, ''),
    ]
    assert matching_jobs(startup_queries[0], rows, None) == []
    for sql in startup_queries[1:]:
        assert matching_jobs(sql, rows, None) == ['api-interrupted']


@pytest.mark.parametrize('phase', [
    'claude_code_detached', 'restarting', 'deploying', 'push_done', 'verifying', 'rolling_back',
])
def test_later_recovery_phases_select_only_api_owned_jobs(startup_queries, phase):
    marker = "phase = 'claude_code_detached'" if phase == 'claude_code_detached' else "phase IN ('restarting'"
    sql = next(s for s in startup_queries.all_sql if marker in s)
    rows = [
        ('remote', 'running', phase, None, 'contabo14', 223175, ''),
        ('api-direct-ssh', 'running', phase, None, None, None, ''),
    ]
    assert matching_jobs(sql, rows, None) == ['api-direct-ssh']


@pytest.mark.parametrize('remote_host', ['contabo14', 'missing-host', ''])
def test_past_remote_error_is_not_automatically_replayed(startup_queries, remote_host):
    sql = next(s for s in startup_queries.all_sql if 'FROM pipeline_jobs pj' in s)
    # Run the captured full SELECT, including replay time/cycle predicates.
    sql = sql.replace('pj.chat_session_id::uuid', 'pj.chat_session_id')
    sql = sql.replace("NOW() - INTERVAL '30 minutes'", "'2026-10-06 06:30:00'")
    db = sqlite3.connect(':memory:')
    try:
        db.execute('CREATE TABLE pipeline_jobs(job_id TEXT, chat_session_id TEXT, project TEXT, '
                   'instruction TEXT, cycle INTEGER, max_cycles INTEGER, status TEXT, '
                   'review_feedback TEXT, error_detail TEXT, created_at TEXT, runner_host TEXT)')
        db.execute('CREATE TABLE chat_messages(session_id TEXT, model_used TEXT, created_at TEXT)')
        common = ('session-1', 'GO100', 'recover-original', 1, 3, 'error',
                  '서버 재시작으로 중단됨', 'server_restart_orphan', '2026-10-06 06:59:00')
        db.executemany('INSERT INTO pipeline_jobs VALUES(?,?,?,?,?,?,?,?,?,?,?)', [
            ('remote-error', *common, remote_host), ('api-error', *common, None),
        ])
        assert [r[0] for r in db.execute(sql)] == ['api-error']
    finally:
        db.close()
