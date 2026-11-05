"""D3 deploy audit tested with an in-memory connection; no database, model, or network."""
import json
import re
from pathlib import Path

import pytest

from app.services import design_audit_service
from app.services.design_agent import audit
from app.services.design_agent.budget import RunBudget
from app.services.design_agent.writer import DesignAgentWriter

TENANT = 'tenant-1'
PROJECT = 'AADS'
MIGRATION_084 = (Path(__file__).resolve().parents[2] / 'migrations'
                 / '084_design_modification_studio.sql').read_text(encoding='utf-8')
REQUEST_TYPES = set(re.findall(r"'(\w+)'", re.search(
    r'design_modification_requests_type_check CHECK \((.*?)\n    \)', MIGRATION_084,
    re.S).group(1))) - {'IN'}
REQUEST_STATUSES = set(re.findall(r"'(\w+)'", re.search(
    r'design_modification_requests_status_check CHECK \((.*?)\n    \)', MIGRATION_084,
    re.S).group(1)))


class FakeConn:
    """Checks column types against migrations 082/084 (INTEGER, JSONB, TEXT, CHECK lists)."""

    def __init__(self):
        self.audit_runs = []
        self.requests = []
        self.audits = []

    async def fetchval(self, sql, *args):
        if sql.lstrip().startswith('SELECT 1 FROM design_projects'):
            return 1
        if sql.startswith('INSERT INTO design_audit_runs'):
            row = self._row(sql, args)
            assert type(row['score']) is int, 'design_audit_runs.score is INTEGER'
            assert type(row['scanned_files']) is int, 'design_audit_runs.scanned_files is INTEGER'
            assert isinstance(row['project_key'], str) and row['status'] == 'draft'
            assert isinstance(json.loads(row['findings']), list), 'findings is JSONB'
            row['findings'] = json.loads(row['findings'])
            self.audit_runs.append(row)
            return f'audit-{len(self.audit_runs)}'
        assert sql.startswith('INSERT INTO design_modification_requests'), sql
        row = self._row(sql, args)
        assert isinstance(row['user_prompt'], str) and row['user_prompt'].strip()
        assert row['request_type'] in REQUEST_TYPES, 'request_type CHECK (migration 084)'
        assert row['status'] in REQUEST_STATUSES
        assert 'tenant_id' not in row and row.get('screen_id') is None
        for column in ('normalized_card', 'allowed_scope', 'forbidden_scope',
                       'acceptance_criteria'):  # migration 084: scopes are JSONB objects
            assert isinstance(row[column], str), f'{column} must be passed as JSONB text'
            row[column] = json.loads(row[column])
        assert isinstance(row['normalized_card'], dict)
        assert isinstance(row['allowed_scope'], dict), 'allowed_scope DEFAULT is {} (object)'
        assert isinstance(row['forbidden_scope'], dict), 'forbidden_scope DEFAULT is {} (object)'
        assert isinstance(row['acceptance_criteria'], list)
        self.requests.append(row)
        return f'req-{len(self.requests)}'

    @staticmethod
    def _row(sql, args):
        columns = [c.strip() for c in sql.split('(', 1)[1].split(')', 1)[0].split(',')]
        assert len(columns) == len(args)
        return dict(zip(columns, args))

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


def trigger(**payload):
    return {'id': 't-1', 'event_type': 'deploy_succeeded', 'payload': payload}


def write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')
    return path


BAD_CSS = '.btn { height: 30px; }\n'


def test_contrast_ratio_basics():
    assert contrast_close(audit.contrast_ratio('#000', '#fff'), 21.0)
    assert contrast_close(audit.contrast_ratio('rgb(0, 0, 0)', '#ffffff'), 21.0)
    assert contrast_close(audit.contrast_ratio('rgb(0 0 0)', 'rgb(100%, 100%, 100%)'), 21.0)


def contrast_close(value, expected):
    return value is not None and abs(value - expected) < 0.01


@pytest.mark.parametrize('fg,bg', [
    ('var(--fg)', '#fff'), ('red', '#fff'), ('#12', '#fff'), ('rgba(0,0,0,0.5)', '#fff'),
    ('#0000', '#fff'), ('rgb(1,2)', '#fff'), ('#fff', 'transparent'), (None, '#fff'),
])
def test_contrast_ratio_unparseable_is_none_not_zero(fg, bg):
    assert audit.contrast_ratio(fg, bg) is None


def test_contrast_boundary():
    assert 4.47 <= audit.contrast_ratio('#777', '#fff') < 4.5
    assert audit.contrast_ratio('#767676', '#fff') >= 4.5
    bad = audit.check_contrast('.a { color: #777; background: #fff; }', 'a.css')
    assert len(bad) == 1 and bad[0]['rule'] == 'contrast' and bad[0]['threshold'] == '4.5:1'
    assert bad[0]['file'] == 'a.css' and bad[0]['line'] == 1 and '#777' in bad[0]['value']
    assert audit.check_contrast('.a { color: #767676; background: #fff; }', 'a.css') == []
    exact = audit.contrast_ratio('#777', '#fff')
    css = '.a { color: #777; background-color: #fff; }'
    assert audit.check_contrast(css, 'a.css', min_ratio=exact) == []
    assert len(audit.check_contrast(css, 'a.css', min_ratio=exact + 0.001)) == 1


def test_contrast_skips_unparseable_and_partial_declarations():
    css = ('.a { color: var(--fg); background: #fff; }\n'
           '.b { color: #777; }\n'
           '.c { color: #777; background: linear-gradient(#fff, #eee); }\n'
           '.d { color: rgba(0,0,0,.4); background: #fff; }\n')
    assert audit.check_contrast(css, 'a.css') == []


def test_contrast_in_jsx_style_html_inline_and_tailwind():
    jsx = "const x = <p style={{ color: '#777', backgroundColor: '#fff' }}>t</p>;\n"
    html = '<p style="color:#777;background:#fff">t</p>\n'
    tw = '<p className="text-[#777] bg-[#fff]">t</p>\n'
    ok = '<p className="text-[#000] bg-[#fff]">t</p>\n'
    for source in (jsx, html, tw):
        assert [v['rule'] for v in audit.check_contrast(source, 'x.tsx')] == ['contrast'], source
    assert audit.check_contrast(ok, 'x.tsx') == []


def test_background_color_is_not_read_as_color():
    css = '.a { background-color: #000; color: #fff; }'
    assert audit.check_contrast(css, 'a.css') == []


@pytest.mark.parametrize('css,count', [
    ('.btn { height: 43px; }', 1), ('.btn { height: 44px; }', 0),
    ('.btn { min-height: 43.9px; }', 1), ('.btn { min-height: 2.75rem; }', 0),
    ('.btn { min-height: 2rem; }', 1), ('a.link { height: 20px; }', 1),
    ('.card { height: 20px; }', 0), ('button:hover { height: 10px; }', 1),
    ('.btn { padding: 8px 12px; font-size: 14px; }', 1),
    ('.btn { padding: 14px 12px; font-size: 14px; line-height: 20px; }', 0),
    ('.btn { padding: 8px 12px; }', 0),
    ('.btn { height: 30px; min-height: 44px; }', 0),
])
def test_touch_target_css_boundary(css, count):
    found = audit.check_touch_target(css, 'a.css')
    assert len(found) == count
    for item in found:
        assert item['rule'] == 'touch_target' and item['threshold'] == '44px'


@pytest.mark.parametrize('source,count', [
    ('<button className="h-8 px-2">x</button>', 1),
    ('<button className="h-10">x</button>', 1),
    ('<button className="h-11">x</button>', 0),
    ('<button className="h-[43px]">x</button>', 1),
    ('<button className="h-[44px]">x</button>', 0),
    ('<button className="h-9 min-h-11">x</button>', 0),
    ('<a class="h-9" href="/x">x</a>', 1),
    ('<Link href="/x" className={cn("h-9", open && "bg-red")}>x</Link>', 1),
    ('<div className="h-8">x</div>', 0),
    ('<button className="md:h-8">x</button>', 0),
    ('<button className="py-2 text-sm">x</button>', 1),
    ('<button className="py-3">x</button>', 0),
    ('<button className="py-1.5 leading-6">x</button>', 0),
    ('<button className="${size}">x</button>', 0),
    ('<button style={{ height: 30 }}>x</button>', 0),
    ('<button style={{ height: "30px" }}>x</button>', 1),
    ('<a style="min-height: 20px">x</a>', 1),
    ('<div style="min-height: 20px">x</div>', 0),
])
def test_touch_target_markup(source, count):
    assert len(audit.check_touch_target(source, 'x.tsx')) == count


def test_touch_target_apply():
    assert len(audit.check_touch_target('.btn { @apply h-9 px-4; }', 'a.css')) == 1
    assert audit.check_touch_target('.btn { @apply h-12 px-4; }', 'a.css') == []


@pytest.mark.parametrize('source,count', [
    ('.t { width: 120px; white-space: nowrap; }', 1),
    ('.t { width: 120px; white-space: nowrap; overflow: hidden; }', 0),
    ('.t { width: 120px; font-size: 14px; }', 1),
    ('.t { width: 120px; font-size: 14px; overflow-wrap: anywhere; }', 0),
    ('.t { width: 120px; white-space: nowrap; overflow-wrap: normal; }', 1),
    ('.t { width: 120px; white-space: nowrap; overflow-wrap: anywhere; }', 1),
    ('.t { width: 120px; white-space: nowrap; text-overflow: ellipsis; }', 1),
    ('.t { width: 120px; white-space: nowrap; text-overflow: ellipsis; overflow: hidden; }', 0),
    ('.t { width: 120px; font-size: 14px; overflow-wrap: normal; }', 1),
    ('.t { width: 120px; font-size: 14px; overflow: visible; }', 1),
    ('.t { width: 120px; white-space: nowrap; overflow: hidden; overflow-x: visible; }', 1),
    ('<p className="w-32 whitespace-nowrap text-ellipsis">x</p>', 1),
    ('<p className="w-32 whitespace-nowrap break-words">x</p>', 1),
    ('<p className="w-32 whitespace-nowrap truncate">x</p>', 0),
    ('<p className="w-32 text-sm overflow-y-auto">x</p>', 1),
    ('.t { width: 120px; font-size: 14px; overflow: auto; }', 0),
    ('.t { width: 100%; white-space: nowrap; }', 0),
    ('.t { max-width: 120px; white-space: nowrap; }', 0),
    ('.t { width: 120px; }', 0),
    ('.t { white-space: nowrap; }', 0),
    ('<p className="w-32 whitespace-nowrap">x</p>', 1),
    ('<p className="w-[200px] text-sm">x</p>', 1),
    ('<p className="w-32 truncate">x</p>', 0),
    ('<p className="w-32 whitespace-nowrap overflow-hidden">x</p>', 0),
    ('<p className="w-full whitespace-nowrap">x</p>', 0),
    ('<p className="w-1/2 whitespace-nowrap">x</p>', 0),
    ('<p style="width: 90px; white-space: nowrap">x</p>', 1),
    ('<p style={{ width: 90, whiteSpace: "nowrap" }}>x</p>', 0),
    ('<p style={{ width: "90px", whiteSpace: "nowrap" }}>x</p>', 1),
])
def test_overflow(source, count):
    found = audit.check_overflow(source, 'x.tsx')
    assert len(found) == count
    for item in found:
        assert item['rule'] == 'overflow' and item['file'] == 'x.tsx' and item['line'] == 1


def test_violation_line_numbers():
    css = 'body { margin: 0; }\n\n.btn {\n  height: 10px;\n}\n'
    assert audit.check_touch_target(css, 'a.css')[0]['line'] == 3


@pytest.mark.asyncio
async def test_no_violations_creates_audit_run_but_no_requests(project):
    write(project, 'ok.css', '.btn { min-height: 48px; color: #000; background: #fff; }\n')
    conn = FakeConn()
    await audit.handle_deploy_audit(trigger(project_root='.'), writer_for(conn), RunBudget())
    assert len(conn.audit_runs) == 1 and conn.requests == []
    run = conn.audit_runs[0]
    assert run['score'] == 100 and run['scanned_files'] == 1 and run['findings'] == []
    assert run['status'] == 'draft'


@pytest.mark.asyncio
async def test_violations_create_grouped_requests(project):
    write(project, 'a.css', '.btn { height: 30px; }\n.link { height: 20px; }\n'
          '.x { color: #777; background: #fff; }\n')
    write(project, 'b.tsx', 'export const B = () => <button className="h-8">x</button>;\n')
    conn = FakeConn()
    await audit.handle_deploy_audit(trigger(project_root='.'), writer_for(conn), RunBudget())
    run = conn.audit_runs[0]
    assert run['scanned_files'] == 2 and run['score'] == 100 - 5 * 4
    assert {(f['rule'], f['file']) for f in run['findings']} == {
        ('touch_target', 'a.css'), ('contrast', 'a.css'), ('touch_target', 'b.tsx')}
    assert all(set(f) >= {'rule', 'file', 'line', 'value', 'threshold'} for f in run['findings'])
    assert len(conn.requests) == 3
    by_key = {(r['normalized_card']['rule'], r['normalized_card']['file']): r
              for r in conn.requests}
    assert by_key[('touch_target', 'a.css')]['normalized_card']['violation_count'] == 2
    for request in conn.requests:
        file = request['normalized_card']['file']
        assert request['status'] == 'draft'
        assert request['allowed_scope'] == {'target_files': [file]}
        assert request['forbidden_scope'] == {'notes': ['다른 파일', '토큰 승인본']}
        assert request['acceptance_criteria'] and all(request['acceptance_criteria'])
        assert request['normalized_card']['source'] == 'audit_fix'
        assert file in request['user_prompt']
    assert by_key[('contrast', 'a.css')]['request_type'] == 'color'
    assert by_key[('touch_target', 'b.tsx')]['request_type'] == 'interaction'
    assert '4.5:1' in ' '.join(by_key[('contrast', 'a.css')]['acceptance_criteria'])
    assert '44px' in ' '.join(by_key[('touch_target', 'a.css')]['acceptance_criteria'])


def test_request_types_satisfy_migration_check():
    assert audit.REQUEST_TYPE and set(audit.REQUEST_TYPE.values()) <= REQUEST_TYPES
    assert 'audit_fix' not in REQUEST_TYPES


@pytest.mark.asyncio
async def test_request_cap_and_truncated_findings(project):
    for i in range(25):
        write(project, f'f{i:02d}.css', BAD_CSS)
    conn = FakeConn()
    await audit.handle_deploy_audit(trigger(project_root='.'), writer_for(conn), RunBudget())
    assert len(conn.requests) == 20
    findings = conn.audit_runs[0]['findings']
    assert len(findings) == 25
    assert sum(1 for f in findings if f.get('truncated')) == 5
    covered = {r['allowed_scope']['target_files'][0] for r in conn.requests}
    assert {f['file'] for f in findings if f.get('truncated')}.isdisjoint(covered)


@pytest.mark.asyncio
async def test_file_scan_limit_is_200(project):
    for i in range(210):
        write(project, f'g{i:03d}.css', '.a { margin: 0; }\n')
    conn = FakeConn()
    await audit.handle_deploy_audit(trigger(project_root='.'), writer_for(conn), RunBudget())
    assert conn.audit_runs[0]['scanned_files'] == 200


@pytest.mark.asyncio
@pytest.mark.parametrize('root', ['../outside', '/etc', '', None, '   '])
async def test_project_root_outside_allowed_is_rejected(project, root):
    conn = FakeConn()
    with pytest.raises(ValueError):
        await audit.handle_deploy_audit(trigger(project_root=root), writer_for(conn), RunBudget())
    assert conn.audit_runs == [] and conn.requests == []


@pytest.mark.asyncio
async def test_project_root_must_be_a_directory(project):
    write(project, 'one.css', BAD_CSS)
    conn = FakeConn()
    with pytest.raises(ValueError):
        await audit.handle_deploy_audit(trigger(project_root='one.css'), writer_for(conn),
                                        RunBudget())
    assert conn.audit_runs == []


@pytest.mark.asyncio
async def test_symlink_to_outside_root_is_not_read(project, tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    write(outside, 'secret.css', BAD_CSS)
    (project / 'link.css').symlink_to(outside / 'secret.css')
    write(project, 'real.css', '.a { margin: 0; }\n')
    conn = FakeConn()
    await audit.handle_deploy_audit(trigger(project_root='.'), writer_for(conn), RunBudget())
    assert conn.audit_runs[0]['scanned_files'] == 1
    assert conn.audit_runs[0]['findings'] == [] and conn.requests == []


def test_scan_project_does_not_open_outside_file(project, tmp_path, monkeypatch):
    outside = write(tmp_path / 'outside', 'secret.css', BAD_CSS)
    (project / 'link.css').symlink_to(outside)
    opened = []
    original = Path.read_text

    def spy(self, *args, **kwargs):
        opened.append(Path(self).name)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, 'read_text', spy)
    violations, scanned = audit.scan_project(project)
    assert (violations, scanned) == ([], 0) and 'secret.css' not in opened


def test_handler_registered_and_enabled_default_unchanged():
    from app.services.design_agent import runner
    from app.services.design_agent.config import Config
    assert runner.HANDLERS['deploy_succeeded'] is audit.handle_deploy_audit
    assert Config().enabled is False


@pytest.mark.parametrize('css,touch,contrast', [
    ('.btn { height: 44px; height: 20px; }', 1, 0),
    ('.btn { height: 20px; height: 44px; }', 0, 0),
    ('.btn { height: 20px !important; height: 44px; }', 1, 0),
    ('.btn { height: 44px !important; height: 20px; }', 0, 0),
    ('.a { color: #777; color: #000; background: #fff; }', 0, 0),
    ('.a { color: #000; color: #777; background: #fff; }', 0, 1),
    ('.a { color: #777; background: #fff; background-color: #000; }', 0, 0),
    ('.a { color: #777; background-color: #000; background: #fff; }', 0, 1),
])
def test_duplicate_declarations_last_one_wins(css, touch, contrast):
    assert len(audit.check_touch_target(css, 'a.css')) == touch
    assert len(audit.check_contrast(css, 'a.css')) == contrast


@pytest.mark.parametrize('source,heights', [
    ('<button className={small ? "h-8" : "h-12"}>x</button>', ['height 32px']),
    ('<button className={small ? "h-12" : "h-8"}>x</button>', ['height 32px']),
    ('<button className={small ? "h-12" : "h-14"}>x</button>', []),
    ('<button className={cn("px-2", a ? "h-8" : b ? "h-12" : "h-9", "x")}>x</button>',
     ['height 32px', 'height 36px']),
    ("<button className={clsx('base', { 'h-8': small })}>x</button>", ['height 32px']),
    ('<button className={cn("h-12", small && "h-8")}>x</button>', []),
    ('<button className={cn("h-8", wide && "px-6")}>x</button>', ['height 32px']),
    ('<button className={`h-8 ${x}`}>x</button>', ['height 32px']),
])
def test_conditional_class_branches_are_judged_separately(source, heights):
    assert [v['value'] for v in audit.check_touch_target(source, 'x.tsx')] == heights


def test_conditional_class_contrast_branch():
    source = '<p className={dim ? "text-[#777] bg-[#fff]" : "text-[#000] bg-[#fff]"}>x</p>'
    assert [v['rule'] for v in audit.check_contrast(source, 'x.tsx')] == ['contrast']


@pytest.mark.asyncio
async def test_conditional_class_violation_creates_request(project):
    write(project, 'b.tsx', 'export const B = () => <button className={s ? "h-8" : "h-12"}>x</button>;\n')
    conn = FakeConn()
    await audit.handle_deploy_audit(trigger(project_root='.'), writer_for(conn), RunBudget())
    assert len(conn.requests) == 1 and conn.requests[0]['normalized_card']['file'] == 'b.tsx'


@pytest.mark.asyncio
async def test_request_scopes_survive_existing_consumers(project):
    from app.services import design_context_pack_service as pack
    from app.services import design_qa_scorer as scorer
    from app.api.design_modifications import missing_design_scope_fields
    write(project, 'a.css', BAD_CSS)
    conn = FakeConn()
    await audit.handle_deploy_audit(trigger(project_root='.'), writer_for(conn), RunBudget())
    row = conn.requests[0]
    assert pack._as_dict(row['allowed_scope']) == {'target_files': ['a.css']}
    assert pack._as_dict(row['forbidden_scope']) == {'notes': ['다른 파일', '토큰 승인본']}
    assert 'a.css' in scorer._extract_candidate_file_paths(row, {})
    assert missing_design_scope_fields(row['allowed_scope'], row['forbidden_scope'],
                                       row['acceptance_criteria']) == []
