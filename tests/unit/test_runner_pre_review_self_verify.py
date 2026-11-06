"""러너 검수 전 게이트의 실제 셸 함수와 안전한 실행을 검증한다."""
import json
import os
import sys
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = (ROOT / 'scripts/pipeline-runner.sh').read_text()
NAMES = ('self_verify_extract', 'self_verify_classify', 'pre_review_self_verify')


def function(source, name):
    return re.search(rf'^{name}\(\) \{{\n.*?^\}}', source, re.M | re.S).group()


FUNCTIONS = '\n'.join(function(SRC, name) for name in NAMES)
STUB = '''
record_runner_event() { echo "EVENT:$2:${9-}"; }
sql_escape() { printf "'%s'" "${1//\\'/\\'\\'}"; }
db_update() { echo "DB:$*"; }
_fail_job() { echo "FAIL:$3:$4"; }
'''


def run(body, instruction='', **env):
    return subprocess.run(['bash', '-euc', FUNCTIONS + '\n' + STUB + '\n' + body],
                          input=instruction, text=True, capture_output=True,
                          env={**os.environ, **env})


def test_extraction_order_limit_and_duplicates():
    result = run('self_verify_extract', 'pytest a\n`pytest a`\n`ruff check .`\n'
                 'python3 -m pytest b\nnpm test\n')
    assert result.returncode == 0
    assert result.stdout.splitlines() == ['command\tpytest a', 'command\truff check .',
                                          'command\tpython3 -m pytest b']


@pytest.mark.parametrize('command', ['pytest x; rm -rf /', 'pytest x && echo bad',
                                    'pytest x | cat', 'pytest > x', 'pytest $(id)',
                                    'python3 -c evil', 'npm run build'])
def test_unsafe_commands_not_selected(command):
    result = run('self_verify_extract', f'`{command}`')
    assert result.stdout.startswith('skipped_unsafe\t')
    assert 'command\t' not in result.stdout


@pytest.mark.parametrize(('command', 'rc', 'expected'), [
    ('pytest x', 0, 'pass'), ('bash scripts/run_unit_tests.sh x', 2, 'unavailable'),
    ('scripts/run_unit_tests.sh x', 2, 'unavailable'), ('pytest x', 127, 'unavailable'),
    ('pytest x', 2, 'fail'), ('pytest x', 1, 'fail'), ('pytest x', 124, 'fail')])
def test_rc_table(command, rc, expected):
    result = run('self_verify_classify "$CMD" "$RC"', CMD=command, RC=str(rc))
    assert result.stdout == expected


def gate(tmp_path, instruction, **env):
    return run('pre_review_self_verify job session "$DIR" "$INST"; '
               'printf "PREFIX:%s" "$SELF_VERIFY_PREFIX"',
               DIR=str(tmp_path), INST=instruction, RUNNER_HEAVY_DIR=str(tmp_path / 'lane'), **env)


def test_no_commands(tmp_path):
    result = gate(tmp_path, '설명만 있음')
    assert result.returncode == 0
    assert 'self_verify_skipped_no_cmd' in result.stdout


@pytest.mark.parametrize('switch', ['0', 'false', 'off', 'no'])
def test_disabled(tmp_path, switch):
    result = gate(tmp_path, 'pytest broken', PIPELINE_SELF_VERIFY=switch)
    assert result.stdout == 'PREFIX:'


def test_instruction_opt_out(tmp_path):
    assert gate(tmp_path, 'NO_SELF_VERIFY\npytest broken').stdout == 'PREFIX:'


@pytest.mark.parametrize(('rc', 'event', 'success'), [(0, 'self_verify_pass', True),
                                                   (127, 'self_verify_unavailable', True),
                                                   (1, 'self_verify_failed', False)])
def test_execution_and_failure_gate(tmp_path, rc, event, success):
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    executable = bin_dir / 'pytest'
    executable.write_text(f'#!/bin/bash\npwd\necho test-output\nexit {rc}\n')
    executable.chmod(0o755)
    result = gate(tmp_path, 'pytest target', PATH=f'{bin_dir}:{os.environ["PATH"]}')
    assert (result.returncode == 0) == success
    assert event in result.stdout
    if success:
        assert f'PREFIX:[Runner self-verify] pytest target rc={rc}' in result.stdout
    else:
        assert 'test-output' in result.stdout
        assert str(tmp_path) in result.stdout
        assert '[Runner self-verify]' in result.stdout


def test_unsafe_is_not_executed(tmp_path):
    result = gate(tmp_path, '`pytest x; touch SENTINEL`')
    assert 'EVENT:skipped_unsafe' in result.stdout
    assert not (tmp_path / 'SENTINEL').exists()


def test_functions_synced_and_gate_precedes_review():
    other = (ROOT / 'scripts/pipeline-runner.sh.local').read_text()
    for name in NAMES:
        assert function(SRC, name) == function(other, name)
    assert SRC.index('if ! pre_review_self_verify') < SRC.index("SET phase='ai_review'")


def test_real_timeout_blocks_review(tmp_path):
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    executable = bin_dir / 'pytest'
    executable.write_text('#!/bin/bash\necho timeout-output\nsleep 3\n')
    executable.chmod(0o755)
    result = gate(tmp_path, 'pytest target', PATH=f'{bin_dir}:{os.environ["PATH"]}',
                  PIPELINE_SELF_VERIFY_TIMEOUT_SEC='1')
    assert result.returncode == 1
    assert 'self_verify_failed' in result.stdout
    assert 'rc=124' in result.stdout


def test_unit_script_unavailable_and_quoted_arguments(tmp_path):
    scripts = tmp_path / 'scripts'
    scripts.mkdir()
    (scripts / 'run_unit_tests.sh').write_text('#!/bin/bash\nprintf "arg=%s\\n" "$1"\nexit 2\n')
    result = gate(tmp_path, 'bash scripts/run_unit_tests.sh "test with spaces"')
    assert result.returncode == 0
    assert 'self_verify_unavailable' in result.stdout
    assert 'arg=test with spaces' in result.stdout


def test_failure_feedback_keeps_only_last_eighty_lines(tmp_path):
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    executable = bin_dir / 'pytest'
    executable.write_text('#!/bin/bash\nfor i in {1..100}; do echo "output-$i-end"; done\nexit 1\n')
    executable.chmod(0o755)
    result = gate(tmp_path, 'pytest target', PATH=f'{bin_dir}:{os.environ["PATH"]}')
    assert result.returncode == 1
    assert 'output-20-end' not in result.stdout
    assert 'output-21-end' in result.stdout
    assert 'output-100-end' in result.stdout


def test_busy_lock_fails_open_and_reaches_review(tmp_path):
    import fcntl
    lane = tmp_path / 'lane'
    lane.mkdir()
    with (lane / 'slot-0').open('w') as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        result = run('pre_review_self_verify job session "$DIR" "$INST"; '
                     'echo AI_REVIEW; printf "%s" "$SELF_VERIFY_PREFIX"',
                     DIR=str(tmp_path), INST='pytest target',
                     RUNNER_HEAVY_DIR=str(lane), PIPELINE_SELF_VERIFY_LOCK_WAIT_SEC='1',
                     PIPELINE_SELF_VERIFY_TIMEOUT_SEC='1')
    assert result.returncode == 0
    assert 'self_verify_lock_unavailable' in result.stdout
    assert 'self_verify_failed' not in result.stdout
    assert 'AI_REVIEW' in result.stdout
    assert 'skipped: lock unavailable' in result.stdout


def test_lock_wait_is_outside_test_timeout(tmp_path):
    import fcntl
    import threading
    lane = tmp_path / 'lane'
    lane.mkdir()
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    executable = bin_dir / 'pytest'
    executable.write_text('#!/bin/bash\nsleep 0.4\n')
    executable.chmod(0o755)
    with (lane / 'slot-0').open('w') as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        timer = threading.Timer(1.2, lambda: fcntl.flock(held, fcntl.LOCK_UN))
        timer.start()
        try:
            result = gate(tmp_path, 'pytest target',
                          PATH=f'{bin_dir}:{os.environ["PATH"]}',
                          PIPELINE_SELF_VERIFY_LOCK_WAIT_SEC='4',
                          PIPELINE_SELF_VERIFY_TIMEOUT_SEC='1')
        finally:
            timer.join()
    assert result.returncode == 0
    assert 'self_verify_pass' in result.stdout


def test_prose_noise_and_unsafe_aggregation(tmp_path):
    prose = '\n'.join(f'일반 설명 문장 {i}' for i in range(30))
    result = gate(tmp_path, prose)
    assert 'EVENT:skipped_unsafe' not in result.stdout
    result = gate(tmp_path, prose + '\n' + '\n'.join(
        f'`pytest x{i}; echo unsafe`' for i in range(5)))
    assert result.stdout.count('EVENT:skipped_unsafe') == 1
    assert '"count": 5' in result.stdout
    assert 'x2;' in result.stdout
    assert 'x3;' not in result.stdout


@pytest.mark.parametrize('enabled', ['1', '0'])
@pytest.mark.parametrize('diff_prefix', ['', 'diff truncated'])
def test_output_and_review_instruction_preserved(tmp_path, enabled, diff_prefix):
    # Execute the production gate and request construction, with transport/DB stubbed.
    start = SRC.index('            if ! pre_review_self_verify')
    end = SRC.index('            while [[ $review_attempt', start)
    section = SRC[start:end]
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    executable = bin_dir / 'pytest'
    executable.write_text('#!/bin/bash\nexit 0\n')
    executable.chmod(0o755)
    # Only emulate the two production jq -n request constructors; no host jq needed.
    jq = bin_dir / 'jq'
    jq.write_text(f'#!{sys.executable}\n' + """
import json, sys
args = sys.argv[1:]
assert args.pop(0) == '-n'
values = {}
while args and args[0] == '--arg':
    _, key, value = args[:3]
    values[key] = value
    del args[:3]
body = dict(job_id=values['jid'], project=values['proj'], diff=values['diff'],
            instruction=values['inst'], files_changed=values['files'].split(','))
if 'rid' in values:
    body['request_id'] = values['rid']
print(json.dumps(body))
""")
    jq.chmod(0o755)
    result = run(r'''
review_fixture() {
output=$'원문 RESULT\n끝\n'
original="$output"
instruction='pytest target'
job_id=job; session_id=session; worktree_dir="$DIR"; project=test
parallel_group=''; git_diff=$'diff --git a/a b/a\n+addition'; review_diff_prefix="$DIFF_PREFIX"
get_job_status() { echo running; }
job_diff_changed_files() { echo a; }
''' + section + r'''
[[ "$output" == "$original" ]]
printf 'BODY:%s\n' "$review_body"
}
review_fixture
''', DIR=str(tmp_path), RUNNER_HEAVY_DIR=str(tmp_path / 'lane'),
                 PATH=f'{bin_dir}:{os.environ["PATH"]}',
                 PIPELINE_SELF_VERIFY=enabled, DIFF_PREFIX=diff_prefix)
    assert result.returncode == 0, result.stderr
    body = json.loads(result.stdout.split('BODY:', 1)[1])
    assert body['instruction'] == 'pytest target'
    expected = (diff_prefix + '\n' if diff_prefix else '') + 'diff --git a/a b/a\n+addition'
    if enabled == '0':
        assert body['diff'].encode() == expected.encode()
    else:
        tail = body['diff'][len(expected):]
        assert tail.startswith('\n\n[Runner self-verify]')
        assert not re.search(r'^[+-]', tail, re.M)
        assert all(line.startswith('[Runner self-verify]') for line in tail.splitlines() if line)
    assert '원문 RESULT' not in body['diff']
    assert 'output="${SELF_VERIFY_PREFIX}${output}"' not in SRC
    assert '--arg inst "$instruction"' in section


def test_failure_summary_includes_recall_skip_reason(tmp_path):
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    executable = bin_dir / 'pytest'
    executable.write_text('#!/bin/bash\nexit 1\n')
    executable.chmod(0o755)
    result = run('if pre_review_self_verify job session "$DIR" "pytest target"; '
                 'then exit 9; fi; printf "SUMMARY:%s" "$SELF_VERIFY_PREFIX"',
                 DIR=str(tmp_path), RUNNER_HEAVY_DIR=str(tmp_path / 'lane'),
                 PATH=f'{bin_dir}:{os.environ["PATH"]}')
    assert result.returncode == 0
    assert 'SUMMARY:[Runner self-verify]' in result.stdout
    assert '수정 재호출 생략' in result.stdout.split('SUMMARY:', 1)[1]


def test_flock_error_fails_open(tmp_path):
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    executable = bin_dir / 'flock'
    executable.write_text('#!/bin/bash\nexit 42\n')
    executable.chmod(0o755)
    result = gate(tmp_path, 'pytest target', PATH=f'{bin_dir}:{os.environ["PATH"]}')
    assert result.returncode == 0
    assert 'self_verify_lock_unavailable' in result.stdout
    assert 'self_verify_failed' not in result.stdout


def test_test_rc125_is_not_mistaken_for_lock_failure(tmp_path):
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    executable = bin_dir / 'pytest'
    executable.write_text('#!/bin/bash\nexit 125\n')
    executable.chmod(0o755)
    result = gate(tmp_path, 'pytest target', PATH=f'{bin_dir}:{os.environ["PATH"]}')
    assert result.returncode == 1
    assert 'self_verify_failed' in result.stdout
    assert 'self_verify_lock_unavailable' not in result.stdout


@pytest.mark.parametrize('bad_lane', [False, True])
def test_bad_directory_fails_open(tmp_path, bad_lane):
    lane = tmp_path / 'lane'
    if bad_lane:
        lane.write_text('not a directory')
    result = run('pre_review_self_verify job session "$DIR" "pytest target"; echo AI_REVIEW',
                 DIR=str(tmp_path if bad_lane else tmp_path / 'missing'),
                 RUNNER_HEAVY_DIR=str(lane))
    assert result.returncode == 0
    assert 'self_verify_skipped_bad_dir' in result.stdout
    assert 'self_verify_lock_unavailable' not in result.stdout
    assert 'self_verify_failed' not in result.stdout
    assert 'AI_REVIEW' in result.stdout
