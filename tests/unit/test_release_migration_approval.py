"""승인 검증은 DB fixture만 사용하며 거절 시 SQL 적용 호출이 없어야 한다."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).parents[2]
spec = importlib.util.spec_from_file_location('approval', ROOT / 'scripts/release_migration_approval.py')
a = importlib.util.module_from_spec(spec)
spec.loader.exec_module(a)
SHA = 'a' * 40


@pytest.fixture
def approved(tmp_path):
    (tmp_path / 'migrations').mkdir()
    (tmp_path / 'scripts').mkdir()
    (tmp_path / 'migrations/a.sql').write_text('SELECT 1;')
    items = a.inventory(tmp_path, {})
    data = dict(purpose='migration_apply', project='AADS', tenant_id='tenant', target_db='aads',
                release_sha=SHA, manifest=items, manifest_sha256=a.manifest_hash(items),
                expires_at='2099-01-01T00:00:00+00:00')
    doc = dict(approved_revision_id='rev', latest_revision_id='rev', revision_id='rev',
               action='approved', actor_id='human-id', auth_type='human_session', owner=True)
    state = dict(tenant_id='tenant', target_db='aads', ledger={}, document=doc)
    def encode():
        doc['content'] = '# 릴리스 승인\n```aads-migration-approval\n' + json.dumps(data) + '\n```\n'
        doc['content_hash'] = a.digest(doc['content'].encode())
    encode()
    return tmp_path, state, data, encode


def test_approved(approved):
    root, state, _, _ = approved
    assert a.verify(root, SHA, a.inventory(root, {}), state).startswith('APPROVED revision_id=rev approver=human-id')


def test_retry(approved):
    root, state, data, _ = approved
    item = data['manifest'][0]
    state['ledger'][item['file']] = item['sha256']
    assert a.verify(root, SHA, [], state).startswith('APPROVED')


def run_short_sha_gate(root, state, short_sha, full_sha=None):
    """Run the shell gate against the fixture's DB state and a short SHA."""
    module = (ROOT / 'scripts/release_migration_approval.py').read_text()
    start = module.index('    # One SELECT')
    end = module.index('\n\ndef inventory', start)
    (root / 'scripts/release_migration_approval.py').write_text(
        module[:start] + '    return ' + repr(state) + '\n' + module[end:])
    shell = (ROOT / 'scripts/apply_release_migrations.sh').read_text()
    start = shell.index('read_ledger() {')
    end = shell.index('\n}', start)
    (root / 'scripts/apply_release_migrations.sh').write_text(
        shell[:start] + 'read_ledger() {\n    cat "$AADS_MIGRATION_LEDGER_FILE"' + shell[end:])
    ledger = root / 'ledger'
    ledger.write_text('')
    calls = root / 'calls'
    apply = root / 'apply.sh'
    apply.write_text(f'#!/bin/bash\necho write >> {calls}\n')
    apply.chmod(0o755)
    env = dict(os.environ, AADS_RELEASE_SHA=short_sha,
               AADS_MIGRATION_LEDGER_FILE=str(ledger),
               AADS_APPLY_MIGRATION_BIN=str(apply))
    if full_sha is not None:
        env['AADS_RELEASE_SHA_FULL'] = full_sha
    return subprocess.run(
        ['bash', str(root / 'scripts/apply_release_migrations.sh'), '--root', str(root)],
        env=env, text=True, capture_output=True, timeout=10), calls


def test_short_sha_resolves_from_full_environment_and_approves(approved):
    root, state, _, _ = approved
    result, calls = run_short_sha_gate(root, state, SHA[:12], SHA)
    assert result.returncode == 0, result.stdout + result.stderr
    assert f'RELEASE {SHA}\n' in result.stdout
    assert calls.exists()


def test_short_sha_without_approval_is_blocked(approved):
    root, state, _, _ = approved
    state['document'] = None
    result, calls = run_short_sha_gate(root, state, SHA[:12], SHA)
    assert result.returncode == 7
    assert 'BLOCK-CEO-APPROVAL' in result.stdout
    assert not calls.exists()


def test_unresolvable_short_sha_is_blocked_without_fabrication(approved):
    root, state, _, _ = approved
    result, calls = run_short_sha_gate(root, state, 'f' * 12)
    assert result.returncode == 7
    assert 'cannot resolve full release sha from ' + 'f' * 12 in result.stderr
    assert 'RELEASE ' not in result.stdout
    assert not calls.exists()


def test_mismatched_full_environment_sha_is_not_adopted(approved):
    root, state, _, _ = approved
    result, calls = run_short_sha_gate(root, state, 'b' * 12, SHA)
    assert result.returncode == 7
    assert 'cannot resolve full release sha from ' + 'b' * 12 in result.stderr
    assert 'RELEASE ' not in result.stdout
    assert not calls.exists()


CASES = [
    ('missing', 'not_approved'), ('owner', 'approver_not_owner'),
    ('system', 'approver_not_human'), ('auth', 'approver_not_human'),
    ('expiry', 'expired'), ('archived', 'not_approved'), ('review', 'not_approved'),
    ('revised', 'not_approved'), ('hash', 'hash_mismatch'), ('release', 'release_mismatch'),
    ('extra', 'manifest_mismatch'), ('bytes', 'manifest_mismatch'),
    ('tenant', 'scope_mismatch'), ('db', 'scope_mismatch'),
    ('lookup', 'lookup_failed'), ('omitted', 'manifest_mismatch'),
    ('manifest_hash', 'manifest_mismatch'), ('retry_drift', 'manifest_mismatch'),
]


def mutate(case, root, state, data, encode):
    doc = state['document']
    if case == 'missing': state['document'] = None
    elif case == 'owner': doc['owner'] = False
    elif case == 'system': doc['actor_id'] = 'system:monitor'
    elif case == 'auth': doc.pop('auth_type')
    elif case == 'expiry': data['expires_at'] = '2000-01-01T00:00:00Z'
    elif case in ('archived', 'review'): doc['action'] = case
    elif case == 'revised': doc['latest_revision_id'] = 'new'
    elif case == 'release': data['release_sha'] = 'b' * 40
    elif case == 'extra': (root / 'migrations/b.sql').write_text('SELECT 2;')
    elif case == 'bytes': (root / 'migrations/a.sql').write_text('SELECT 3;')
    elif case == 'tenant': data['tenant_id'] = 'other'
    elif case == 'db': data['target_db'] = 'other'
    elif case == 'omitted': data['manifest'] = []; data['manifest_sha256'] = a.manifest_hash([])
    elif case == 'manifest_hash': data['manifest_sha256'] = '0' * 64
    elif case == 'retry_drift': state['ledger']['migrations/a.sql'] = '0' * 64
    encode()
    if case == 'hash': doc['content'] += 'tampered'


@pytest.mark.parametrize('case,code', CASES)
def test_rejection_prevents_sql(approved, case, code):
    root, state, data, encode = approved
    mutate(case, root, state, data, encode)
    # Run the actual shell gate and actual CLI with only the DB transport replaced.
    module = (ROOT / 'scripts/release_migration_approval.py').read_text()
    replacement = "raise RuntimeError('offline')" if case == 'lookup' else 'return ' + repr(state)
    start = module.index('    # One SELECT')
    end = module.index('\n\ndef inventory', start)
    module = module[:start] + '    ' + replacement + '\n' + module[end:]
    (root / 'scripts/release_migration_approval.py').write_text(module)
    shell = (ROOT / 'scripts/apply_release_migrations.sh').read_text()
    start = shell.index('read_ledger() {')
    end = shell.index('\n}', start)
    shell = shell[:start] + 'read_ledger() {\n    cat "$AADS_MIGRATION_LEDGER_FILE"' + shell[end:]
    (root / 'scripts/apply_release_migrations.sh').write_text(shell)
    ledger = root / 'ledger'
    ledger.write_text(''.join(f'{k}|{v}\n' for k, v in state['ledger'].items()))
    calls = root / 'calls'
    apply = root / 'apply.sh'
    apply.write_text(f'#!/bin/bash\necho write >> {calls}\n')
    apply.chmod(0o755)
    env = dict(os.environ, AADS_RELEASE_SHA=SHA, AADS_MIGRATION_LEDGER_FILE=str(ledger),
               AADS_APPLY_MIGRATION_BIN=str(apply))
    # retry_drift is already recorded, so add a second pending file to invoke the gate.
    if case == 'retry_drift': (root / 'migrations/b.sql').write_text('SELECT 2;')
    result = subprocess.run(['bash', str(root / 'scripts/apply_release_migrations.sh'), '--root', str(root)],
                            env=env, text=True, capture_output=True, timeout=10)
    assert result.returncode == 7, result.stdout + result.stderr
    assert code in result.stdout
    assert not calls.exists()


@pytest.mark.parametrize('items', [None, [{'file': '../x.sql', 'sha256': '0'*64}],
    [{'file': 'migrations/x.sql', 'sha256': 'bad'}],
    [{'file': 'migrations/z.sql', 'sha256': '0'*64}, {'file': 'migrations/a.sql', 'sha256': '0'*64}]])
def test_manifest_format(items):
    with pytest.raises(a.Rejected, match='manifest_mismatch'):
        a.valid_manifest(items)


def test_lookup_uses_only_select_and_readonly(monkeypatch):
    def run(cmd, **kwargs):
        assert 'default_transaction_read_only=on' in cmd[5]
        sql = cmd[-1]
        assert sql.startswith('SELECT ')
        assert not any(x in sql for x in ('INSERT ', 'UPDATE ', 'DELETE '))
        return subprocess.CompletedProcess(cmd, 0, '{}', '')
    monkeypatch.setattr(a.subprocess, 'run', run)
    assert a.lookup(SHA) == {}


def test_draft_stdout_does_not_register_or_approve(approved, monkeypatch, capsys):
    root, state, _, _ = approved
    monkeypatch.setattr(a, 'lookup', lambda sha: state)
    monkeypatch.setattr(a, 'register', lambda *args: pytest.fail('unexpected registration'))
    monkeypatch.setattr(a.sys, 'argv', ['approval', 'draft', '--root', str(root), '--release-sha', SHA])
    assert a.main() == 0
    content = capsys.readouterr().out
    assert content.startswith('# 릴리스 마이그레이션 적용 승인')
    assert 'API 롤백은 적용된 SQL을 되돌리지 않음' in content
    assert '```aads-migration-approval' in content


def test_lookup_error_is_closed(approved, monkeypatch, capsys):
    root, _, _, _ = approved
    def fail(sha):
        raise TimeoutError()
    monkeypatch.setattr(a, 'lookup', fail)
    monkeypatch.setattr(a.sys, 'argv', ['approval', 'verify', '--root', str(root), '--release-sha', SHA])
    assert a.main() == 1
    assert capsys.readouterr().out == 'lookup_failed\n'
