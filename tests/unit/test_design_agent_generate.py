"""D2 handlers tested with an in-memory connection; no database, model, or network."""
import json
import os

import pytest

from app.services import design_audit_service
from app.services.design_agent import audit, generate, importer, runner
from app.services.design_agent.budget import RunBudget
from app.services.design_agent.writer import DesignAgentPermissionError, DesignAgentWriter

TENANT = 'tenant-1'
PROJECT = 'AADS'


class FakeConn:
    """Mimics design_token_sets: version is TEXT, UNIQUE(tenant, project, version, mode)."""

    def __init__(self):
        self.rows = []
        self.audits = []

    async def fetchval(self, sql, *args):
        if sql.lstrip().startswith('SELECT 1 FROM design_projects'):
            return 1
        assert sql.startswith('INSERT INTO design_token_sets'), sql
        columns = [c.strip() for c in sql.split('(', 1)[1].split(')', 1)[0].split(',')]
        row = dict(zip(columns, args))
        assert isinstance(row['version'], str), 'design_token_sets.version is TEXT'
        key = (row['tenant_id'], row['project_key'], row['version'], row['mode'])
        assert key not in {(r['tenant_id'], r['project_key'], r['version'], r['mode'])
                           for r in self.rows}, 'UNIQUE(tenant_id, project_key, version, mode)'
        row['tokens'] = json.loads(row['tokens'])
        self.rows.append(row)
        return f'row-{len(self.rows)}'

    async def execute(self, sql, *args):
        self.audits.append(args)


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / 'proj'
    root.mkdir()
    monkeypatch.setitem(design_audit_service.ALLOWED_PROJECT_ROOTS, PROJECT, root)
    return root


def writer_for(conn):
    return DesignAgentWriter(conn, TENANT, PROJECT, 'run-1')


def trigger(trigger_id, event='project_created', **payload):
    return {'id': trigger_id, 'event_type': event, 'payload': payload}


def write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')
    return path


def test_extract_css_variables(project):
    write(project, 'src/app.css', ':root {\n  --color-primary: #0a84ff;\n  --space-md: 16px;\n'
          '  --radius-card: 8px;\n  --font-body: "Pretendard", sans-serif;\n  --other-x: 1;\n}\n')
    tokens = generate.extract_tokens('.', PROJECT)
    assert tokens == {'color': {'primary': '#0a84ff'}, 'spacing': {'md': '16px'},
                      'radius': {'card': '8px'},
                      'typography': {'body': '"Pretendard", sans-serif'}}


def test_extract_without_variables_raises(project):
    write(project, 'a.css', 'body { color: red; }\n')
    with pytest.raises(ValueError):
        generate.extract_tokens('.', PROJECT)


def test_scss_and_cjs_mjs_configs_are_read(project):
    write(project, 'styles/theme.scss', '$x: 1;\n:root { --color-scss: #111; }\n')
    write(project, 'web/tailwind.config.cjs',
          "module.exports = { theme: { extend: { colors: { cjs: '#222' } } } };\n")
    write(project, 'other/tailwind.config.mjs',
          "export default {\n theme: {\n  extend: {\n   colors: {\n    mjs: '#333',\n   },\n  },\n },\n};\n")
    tokens = generate.extract_tokens('.', PROJECT)
    assert tokens['color'] == {'scss': '#111', 'cjs': '#222', 'mjs': '#333'}


def test_tailwind_nested_paths_and_section_end(project):
    write(project, 'tailwind.config.js', """
/** @type {import('tailwindcss').Config} */
module.exports = {
  prefix: 'tw-',
  theme: {
    extend: {
      colors: {
        brand: {
          500: '#111111',
          600: '#222222',
        },
        accent: { 500: '#333333', DEFAULT: '#444444' },
        plain: '#555555',
      },
      spacing: { 18: '4.5rem' },
      borderRadius: { xl: '12px' },
      fontFamily: {
        sans: [
          'Inter',
          'system-ui',
        ],
        mono: ['Fira Code', 'monospace'],
      },
    },
  },
  separator: '_',
  important: '#app',
};
""")
    tokens = generate.extract_tokens('.', PROJECT)
    assert tokens['color'] == {'brand-500': '#111111', 'brand-600': '#222222',
                               'accent-500': '#333333', 'accent': '#444444',
                               'plain': '#555555'}
    assert 'tw-' not in tokens['color'].values() and 'prefix' not in tokens['color']
    assert tokens['spacing'] == {'18': '4.5rem'}
    assert tokens['radius'] == {'xl': '12px'}
    assert tokens['typography'] == {'sans': 'Inter', 'mono': 'Fira Code'}


def test_symlinked_file_and_dir_outside_project_are_not_read(project, tmp_path):
    outside = tmp_path / 'other-project'
    outside.mkdir()
    write(outside, 'secret.css', ':root { --color-leak: #bad; }\n')
    write(project, 'ok.css', ':root { --color-own: #123456; }\n')
    os.symlink(outside / 'secret.css', project / 'linked.css')
    os.symlink(outside, project / 'linked-dir')
    assert generate.extract_tokens('.', PROJECT)['color'] == {'own': '#123456'}


def test_symlink_only_project_has_no_tokens(project, tmp_path):
    outside = write(tmp_path / 'o', 'x.css', ':root { --color-leak: #bad; }\n')
    os.symlink(outside, project / 'linked.css')
    with pytest.raises(ValueError):
        generate.extract_tokens('.', PROJECT)


@pytest.mark.parametrize('requested', ['../escape', '/etc', '', None, '   ', 5])
def test_paths_outside_allowlist_or_missing_are_rejected(project, requested):
    with pytest.raises(ValueError):
        generate.extract_tokens(requested, PROJECT)


def test_unknown_project_key_rejected(project):
    with pytest.raises(ValueError):
        generate.extract_tokens('.', 'NOT_ALLOWED')


def test_three_directions_with_six_states_and_touch_target():
    tokens = {'color': {}, 'spacing': {'md': '16px', 'lg': '1rem', 'x': 'auto'},
              'radius': {}, 'typography': {}}
    directions = generate.build_direction_drafts(tokens)
    assert [d['id'] for d in directions] == ['keep', 'contrast', 'spacing']
    for direction in directions:
        states = direction['states']
        assert [s['state'] for s in states] == ['기본', '로딩', '빈 상태', '오류', '권한 없음', '오프라인']
        for state in states:
            assert state['min_touch_target_px'] == 44
            assert state['recovery_button']['min_touch_target_px'] == 44
            assert state['message'] and state['recovery_button']['label']
    assert directions[2]['adjustments']['scaled_spacing'] == {'md': '20px', 'lg': '1.25rem'}
    assert generate.build_direction_drafts(tokens) == directions


@pytest.mark.asyncio
async def test_handle_generate_writes_one_draft_without_llm(project):
    write(project, 'a.css', ':root { --color-primary: #fff; }\n')
    conn = FakeConn()
    budget = RunBudget()
    await generate.handle_generate(trigger('t-1', project_root='.'), writer_for(conn), budget)
    assert budget.llm_calls == 0
    (row,) = conn.rows
    assert (row['source'], row['status'], row['mode']) == ('extracted', 'draft', 'light')
    assert row['version'] == 'generate-t-1'
    assert len(row['tokens']['directions']) == 3
    assert row['tokens']['color'] == {'primary': '#fff'}


@pytest.mark.asyncio
async def test_follow_up_generation_for_same_project_does_not_collide(project):
    write(project, 'a.css', ':root { --color-primary: #fff; }\n')
    conn = FakeConn()
    await generate.handle_generate(trigger('t-1', project_root='.'), writer_for(conn), RunBudget())
    write(project, 'b.css', ':root { --space-sm: 4px; }\n')
    await generate.handle_generate(trigger('t-2', 'new_screen_requested', project_root='.'),
                                   writer_for(conn), RunBudget())
    await importer.handle_import(
        trigger('t-3', 'manual', kind='genspark_import', raw='{"colors": {"a": "#000"}}'),
        writer_for(conn), RunBudget())
    assert len({r['version'] for r in conn.rows}) == 3


@pytest.mark.asyncio
async def test_generate_requires_project_root_trigger_id_and_tokens(project):
    conn = FakeConn()
    with pytest.raises(ValueError):
        await generate.handle_generate(trigger('t-1'), writer_for(conn), RunBudget())
    with pytest.raises(ValueError):
        await generate.handle_generate(trigger('t-1', project_root='.'), writer_for(conn),
                                       RunBudget())
    write(project, 'a.css', ':root { --color-primary: #fff; }\n')
    with pytest.raises(ValueError):
        await generate.handle_generate({'payload': {'project_root': '.'}}, writer_for(conn),
                                       RunBudget())
    assert conn.rows == []


@pytest.mark.asyncio
async def test_writer_still_rejects_non_draft_and_foreign_tenant():
    conn = FakeConn()
    base = {'tenant_id': TENANT, 'project_key': PROJECT, 'version': 'v', 'mode': 'light',
            'tokens': {}, 'source': 'extracted'}
    with pytest.raises(DesignAgentPermissionError):
        await writer_for(conn).write('design_token_sets', dict(base, status='approved'))
    with pytest.raises(DesignAgentPermissionError):
        await writer_for(conn).write('design_token_sets', dict(base, tenant_id='other'))
    assert conn.rows == []


@pytest.mark.asyncio
@pytest.mark.parametrize('raw', [
    '{\n  "colors": {"brand": {"value": "#ff0000"}},  "spacing": {"md": "8px"},\n'
    '  "fonts": {"body": "Inter"}, "script": "<script>alert(1)</script>"\n}\n',
    {'colors': {'brand': '#f00'}, 'spacing': {'md': '8px'}, 'fonts': {'body': 'Inter'}},
])
async def test_genspark_original_preserved_exactly(raw):
    conn = FakeConn()
    budget = RunBudget()
    await importer.handle_import(trigger('t-9', 'manual', kind='genspark_import', raw=raw),
                                 writer_for(conn), budget)
    (row,) = conn.rows
    assert row['tokens']['raw_original'] == raw
    assert (row['source'], row['status']) == ('imported_genspark', 'draft')
    assert row['tokens']['color']['brand'] in {'#ff0000', '#f00'}
    assert row['tokens']['spacing'] == {'md': '8px'}
    assert row['tokens']['typography'] == {'body': 'Inter'}
    assert row['tokens']['radius'] == {}
    assert budget.llm_calls == 0


@pytest.mark.asyncio
async def test_genspark_size_limit_and_required_keys():
    conn = FakeConn()
    head = '{"colors": {"a": "#000"}, "pad": "'
    exact = head + 'x' * (importer.MAX_RAW_BYTES - len(head) - 2) + '"}'
    assert len(exact.encode()) == importer.MAX_RAW_BYTES
    await importer.handle_import(trigger('t-1', 'manual', kind='genspark_import', raw=exact),
                                 writer_for(conn), RunBudget())
    for bad in (exact + ' ', None, '', '{not json', '[]', '{"other": 1}',
                {'unrelated': {'x': 1}}):
        with pytest.raises(ValueError):
            await importer.handle_import(
                trigger('t-2', 'manual', kind='genspark_import', raw=bad),
                writer_for(conn), RunBudget())
    with pytest.raises(ValueError):
        await importer.handle_import(trigger('t-2', 'manual', raw='{"colors": {"a": "#000"}}'),
                                     writer_for(conn), RunBudget())
    assert len(conn.rows) == 1


def test_handlers_mapping():
    assert runner.HANDLERS['project_created'] is generate.handle_generate
    assert runner.HANDLERS['new_screen_requested'] is runner.handle_new_screen
    assert runner.HANDLERS['manual'] is runner.handle_manual
    assert runner.HANDLERS['deploy_succeeded'] is audit.handle_deploy_audit
    assert runner.HANDLERS['screen_change_requested'] is runner.noop


@pytest.mark.asyncio
async def test_manual_dispatch(monkeypatch):
    conn = FakeConn()
    await runner.handle_manual(trigger('t-1', 'manual', situation='S8'), writer_for(conn),
                               RunBudget())
    assert conn.rows == []
    await runner.handle_manual(
        trigger('t-2', 'manual', kind='genspark_import', raw='{"colors": {"a": "#000"}}'),
        writer_for(conn), RunBudget())
    assert len(conn.rows) == 1


class GateConn(FakeConn):
    """FakeConn plus design_intake_sessions: {id: (project_key, screen_ref, confirmed)}."""

    def __init__(self, sessions):
        super().__init__()
        self.sessions = sessions
        self.order = []

    async def fetchval(self, sql, *args):
        if 'brief_confirmed_at FROM design_intake_sessions' in sql:
            self.order.append('confirm')
            row = self.sessions.get(args[0])
            return row[2] if row else None
        return await super().fetchval(sql, *args)

    async def fetchrow(self, sql, *args):
        if 'FOR SHARE' in sql:
            self.order.append('lock')
        row = self.sessions.get(args[0])
        return {'project_key': row[0], 'screen_ref': row[1]} if row else None


def new_screen(trigger_id='t-9', **payload):
    return trigger(trigger_id, 'new_screen_requested', project_root='.', **payload)


@pytest.mark.asyncio
async def test_new_screen_confirmed_brief_writes_same_draft(project):
    write(project, 'a.css', ':root { --color-primary: #fff; }\n')
    conn = GateConn({'s1': (PROJECT, 'login', 'ts')})
    budget = RunBudget()
    await runner.handle_new_screen(new_screen(intake_session_id='s1', screen_ref='login'),
                                   writer_for(conn), budget)
    plain = FakeConn()
    await generate.handle_generate(new_screen(), writer_for(plain), RunBudget())
    (row,) = conn.rows
    assert row == plain.rows[0] and budget.llm_calls == 0


@pytest.mark.asyncio
async def test_new_screen_locks_session_before_confirm_check(project):
    write(project, 'a.css', ':root { --color-primary: #fff; }\n')
    conn = GateConn({'s1': (PROJECT, None, 'ts')})
    await runner.handle_new_screen(new_screen(intake_session_id='s1'), writer_for(conn),
                                   RunBudget())
    assert conn.order == ['lock', 'confirm']


@pytest.mark.asyncio
@pytest.mark.parametrize('payload,sessions', [
    ({}, {'s1': (PROJECT, None, 'ts')}),
    ({'intake_session_id': 's1'}, {'s1': (PROJECT, None, None)}),
    ({'intake_session_id': 'missing'}, {}),
    ({'intake_session_id': 's1'}, {'s1': ('OTHER', None, 'ts')}),
    ({'intake_session_id': 's1', 'screen_ref': 'login'}, {'s1': (PROJECT, 'cart', 'ts')}),
    ({'intake_session_id': 's1', 'screen_ref': 'login'}, {'s1': (PROJECT, None, 'ts')}),
    ({'intake_session_id': 's1'}, {'s1': (PROJECT, 'cart', 'ts')}),
])
async def test_new_screen_blocked_without_writes(project, payload, sessions):
    write(project, 'a.css', ':root { --color-primary: #fff; }\n')
    conn = GateConn(sessions)
    with pytest.raises(runner.BriefBlocked) as info:
        await runner.handle_new_screen(new_screen(**payload), writer_for(conn), RunBudget())
    assert info.value.reason == 'design_brief_unconfirmed'
    assert conn.rows == [] and conn.audits == []


@pytest.mark.asyncio
async def test_project_created_has_no_gate(project):
    write(project, 'a.css', ':root { --color-primary: #fff; }\n')
    conn = GateConn({})
    await runner.HANDLERS['project_created'](trigger('t-1', project_root='.'),
                                             writer_for(conn), RunBudget())
    assert len(conn.rows) == 1


class RunConn(GateConn):
    def __init__(self, sessions, trigger_row):
        super().__init__(sessions)
        self.trigger_row, self.runs, self.done = trigger_row, [], []

    def transaction(self):
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def cm():
            yield
        return cm()

    async def fetchrow(self, sql, *args):
        if 'design_agent_triggers' in sql:
            row, self.trigger_row = self.trigger_row, None
            return row
        return await super().fetchrow(sql, *args)

    async def fetchval(self, sql, *args):
        if 'FROM design_agent_runs' in sql and 'EXISTS' in sql:
            return any(r['status'] == 'succeeded' and r['hash'] == args[3] for r in self.runs)
        if 'COALESCE(SUM(cost_usd)' in sql:
            return 0
        if sql.lstrip().startswith('INSERT INTO design_agent_runs'):
            self.runs.append({'hash': args[4], 'status': args[5]})
            return 'run-1'
        return await super().fetchval(sql, *args)

    async def execute(self, sql, *args):
        if 'SET status=$3,\n                llm_calls' in sql or 'llm_calls=$4' in sql:
            self.runs[-1]['status'] = args[2]
        elif 'design_agent_triggers' in sql:
            self.done.append(args[2] if len(args) > 2 else 'failed')


@pytest.mark.asyncio
async def test_blocked_run_is_not_a_duplicate_after_confirmation(project, monkeypatch):
    from types import SimpleNamespace

    from app.services.design_agent.config import Config
    write(project, 'a.css', ':root { --color-primary: #fff; }\n')
    monkeypatch.setattr(runner, 'required_skills', lambda conn, situation: _no_skills())
    payload = {'project_root': '.', 'intake_session_id': 's1'}

    def row():
        return {'id': 't-1', 'event_type': 'new_screen_requested', 'project_key': PROJECT,
                'payload': payload}
    conn = RunConn({'s1': (PROJECT, None, None)}, row())
    pool = SimpleNamespace(acquire=lambda: _Acquire(conn))
    first = await runner.process_one(pool, TENANT, Config(enabled=True))
    assert (first['status'], first['reason']) == ('blocked', 'design_brief_unconfirmed')
    assert conn.runs[-1]['status'] != 'succeeded' and conn.rows == []
    conn.sessions['s1'] = (PROJECT, None, 'ts')
    conn.trigger_row = row()
    second = await runner.process_one(pool, TENANT, Config(enabled=True))
    assert second['status'] == 'succeeded' and len(conn.rows) == 1


async def _no_skills():
    return []


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        return False
