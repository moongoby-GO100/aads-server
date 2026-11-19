"""승인 JSON 릴리스 경로 및 초안 요약 절 회귀 검사."""
import importlib.util
import json
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[2]
rubric_spec = importlib.util.spec_from_file_location('rubric_unblock', ROOT / 'app/services/doc_review_rubric.py')
rubric = importlib.util.module_from_spec(rubric_spec)
rubric_spec.loader.exec_module(rubric)
spec = importlib.util.spec_from_file_location('approval_unblock', ROOT / 'scripts/release_migration_approval.py')
approval = importlib.util.module_from_spec(spec)
spec.loader.exec_module(approval)


def test_approval_json_uses_release_tree_when_host_file_absent(tmp_path, monkeypatch):
    sha = subprocess.check_output(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], text=True).strip()
    path = 'scripts/release_migration_approval.py'
    assert not (tmp_path / path).exists()
    monkeypatch.setattr(rubric, '_sanitize', lambda text, limit=None: text)
    monkeypatch.setattr(rubric, '_secret_count', lambda text: 0)
    content = '```aads-migration-approval\n' + json.dumps({'release_sha': sha, 'manifest': [{'file': path}]}) + '\n```\n'
    checks = rubric.run_checks('contract', '승인', content, 'D2', root=tmp_path,
                               repo_dir=ROOT, metadata={'release_sha': '0' * 40})
    result = next(c for c in checks if c['id'] == 'paths_exist')
    assert result['ok']
    assert f'릴리스 {sha[:7]}' in result['detail']
    missing = rubric._check_paths(content.replace(path, 'scripts/definitely_absent_unblock.py'), tmp_path, sha, ROOT)
    assert not missing['ok']


def test_draft_summary_sections(tmp_path, monkeypatch, capsys):
    (tmp_path / 'migrations').mkdir()
    path = tmp_path / 'migrations/example.sql'
    path.write_text('-- 대상: nullable 열\n-- 조건: 사람 승인\n-- 금지: 데이터 삭제\n-- 롤백: 백업 후 down\nSELECT 1;\n')
    monkeypatch.setattr(approval, 'lookup', lambda *args: {'ledger': {}, 'tenant_id': 'internal', 'target_db': 'aads'})
    monkeypatch.setattr('sys.argv', ['approval', 'draft', '--root', str(tmp_path), '--release-sha', 'a' * 40, '--binding', 'manifest'])
    assert approval.main() == 0
    content = capsys.readouterr().out
    for name in ('대상', '조건', '금지', '롤백'):
        assert '## ' + name + '\n' in content
    assert content.index('## 대상') < content.index('## 조건') < content.index('## 금지') < content.index('## 롤백')
    assert content.index('## 롤백') < content.index('```aads-migration-approval')
    assert '직접 검토하세요' not in content
    for text in ('nullable 열', '사람 승인', '데이터 삭제', '백업 후 down'):
        assert text in content
    assert len(approval.summary_sections([{'file': 'migrations/example.sql'}], tmp_path).split('- migrations/example.sql:')) == 5
    monkeypatch.setattr(rubric, '_sanitize', lambda text, limit=None: text)
    monkeypatch.setattr(rubric, '_secret_count', lambda text: 0)
    checks = rubric.run_checks('contract', '승인', content, 'D2', root=tmp_path)
    assert next(c for c in checks if c['id'] == 'required_sections')['ok']


def test_forward_sql_unchanged_and_aliases_equal():
    stems = ['20261111_canonical_document_approval_delegation', '20261112_code_review_requests_revert_of_verified',
             '20261116_monitored_services_acct_fb_public', '20261119_session_relay_context']
    def executable(text):
        return '\n'.join(line for line in text.splitlines() if not line.startswith('--'))
    for stem in stems:
        path = 'migrations/' + stem + '.sql'
        before = subprocess.check_output(['git', '-C', str(ROOT), 'show', 'HEAD:' + path], text=True)
        assert executable(before) == executable((ROOT / path).read_text())
    for stem in stems[:2]:
        assert (ROOT / 'migrations' / (stem + '_rollback.sql')).read_bytes() == (ROOT / 'migrations/rollback' / (stem + '.down.sql')).read_bytes()
    down = (ROOT / 'migrations/rollback/20261119_session_relay_context.down.sql').read_text()
    assert "IS DISTINCT FROM 'absent'" in down
    assert down.index('INTO STRICT') < down.index('DROP COLUMN')


def test_no_release_sha_keeps_host_path_checks(tmp_path, monkeypatch):
    (tmp_path / 'scripts').mkdir()
    (tmp_path / 'scripts/present.py').write_text('# present\n')
    monkeypatch.setattr(rubric, '_sanitize', lambda text, limit=None: text)
    monkeypatch.setattr(rubric, '_secret_count', lambda text: 0)
    assert rubric.extract_release_sha('일반 문서') is None
    assert rubric._check_paths('scripts/present.py', tmp_path)['ok']
    assert not rubric._check_paths('scripts/missing.py', tmp_path)['ok']
