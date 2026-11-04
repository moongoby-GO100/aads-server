"""승인 메시지 생성과 commit-msg 훅(제목 줄 검사) 계약의 경계 회귀."""
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = (ROOT / 'scripts/pipeline-runner.sh').read_text()
START = SCRIPT.index('        commit_msg=$(printf')
END = SCRIPT.index('\n        commit_out=', START)
GENERATOR = SCRIPT[START:END]


def generate(instruction, job='runner-e93f7592'):
    result = subprocess.run(
        ['bash', '-c', 'instruction=$1; job_id=$2; ' + GENERATOR + '\nprintf "%s" "$commit_msg"',
         'test', instruction, job], text=True, capture_output=True, check=True,
    )
    return result.stdout


@pytest.mark.parametrize(('instruction', 'summary'), [
    ('metadata\nTITLE: 러너 승인 커밋 오류 수정\nR', '러너 승인 커밋 오류 수정'),
    ('TITLE:\nTITLE: 실제 제목\n본문', '실제 제목'),
    ('첫줄\r\n둘째\t  줄', '첫줄 둘째 줄'),
    ('A' * 78 + '\nR\n본문', 'A' * 78 + ' R'),
    ('TITLE: ' + '한' * 81, '한' * 80),
    ('', '작업 변경 반영'),
    ('\x01\t\r\n\x7f', '작업 변경 반영'),
    ('\x1bTITLE: \u200b안전\t 제목\r\nR', '안전 제목'),
    ('TITLE:   \n본문', 'TITLE: 본문'),
])
def test_generated_message(instruction, summary):
    message = generate(instruction)
    assert message == 'fix(pipeline-runner): runner-e93f7592 — ' + summary
    assert '\n' not in message and '\r' not in message
    assert not any(re.fullmatch(r'(and|Claude Code|update|fix|test|wip|.)', line)
                   for line in message.splitlines())


def test_job_identity_keeps_duplicate_subjects_distinct():
    assert generate('TITLE: 같은 작업', 'runner-1') != generate('TITLE: 같은 작업', 'runner-2')
    assert generate('TITLE: 같은 작업') == generate('TITLE: 같은 작업')


def test_real_hook_contract_accepts_old_message_and_preserves_audit(tmp_path):
    # Use a non-repository cwd: successful subject validation must still encounter
    # the existing missing-pre-commit-signature gate, never bypass it.
    # 무의미 검사는 제목 줄에만 걸리므로, 본문에 한 글자 줄이 있는 옛 메시지도 이제 제목 검사는 통과한다.
    hook = ROOT / 'scripts/hooks/commit-msg'
    message_file = tmp_path / 'message'
    message_file.write_text('Pipeline-Runner: runner-e93f7592 — ' + 'A' * 78 + '\nR')
    old = subprocess.run(['bash', str(hook), str(message_file)], cwd=tmp_path,
                         capture_output=True, text=True, check=False)
    assert '무의미' not in old.stdout
    assert old.returncode == 1 and 'pre-commit hook 서명 없음' in old.stdout
    message_file.write_text(generate('A' * 78 + '\nR\n본문'))
    new = subprocess.run(['bash', str(hook), str(message_file)], cwd=tmp_path,
                         capture_output=True, text=True, check=False)
    assert '무의미' not in new.stdout
    assert new.returncode == 1 and 'pre-commit hook 서명 없음' in new.stdout


def test_approval_templates_and_existing_commit_path_preserved():
    def approval(text):
        start = text.index('commit_job_worktree_for_approval() {')
        return text[start:text.index('\n}\n', start)]
    body = approval(SCRIPT)
    assert body == approval((ROOT / 'scripts/pipeline-runner.sh.local').read_text())
    assert body.index('APPROVAL_COMMIT_ADOPTED_EXISTING') < body.index('commit_msg=$(printf')
    assert 'approval_commit_ready' in body
    assert '--no-verify' not in body
