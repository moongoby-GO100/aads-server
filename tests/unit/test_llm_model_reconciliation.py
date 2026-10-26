import ast
from pathlib import Path

from app.services.llm_model_reconciliation import (
    check_execution, observe_compilation, selection_reason_hint, summarize,
)


def test_retry_without_evidence_stays_unexplained():
    result = check_execution(request_override='codex:gpt-test', compiled_model='claude-test', retry_count=3)
    assert result['verdict'] == 'unexplained'
    assert result['ok'] is False
    assert result['reason'] is None


def test_original_override_exposes_db_default_mismatch():
    original = 'codex:gpt-test'
    mutated = 'claude-test'
    record = observe_compilation(request_override=original, session_selected=original,
                                 compiled_model=mutated, intent_override=None)
    assert record['assessment']['verdict'] == 'unexplained'
    tree = ast.parse(Path('app/services/chat_service.py').read_text())
    func = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'send_message_stream')
    first = func.body[1]
    assert isinstance(first, ast.Assign)
    assert first.targets[0].id == '_reconciliation_request_override'
    assert first.value.id == 'model_override'
    calls = [n for n in ast.walk(func) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == 'observe_compilation']
    assert len(calls) == 2
    assert all(next(k.value.id for k in c.keywords if k.arg == 'request_override') == '_reconciliation_request_override' for c in calls)


def test_shared_reason_classification():
    for intent in ['auto_reaction', 'system_trigger', 'runner_response', None]:
        record = observe_compilation(request_override='a', session_selected=None,
                                     compiled_model='b', intent_override=intent)
        assert record['assessment']['reason'] == selection_reason_hint(intent)
        result = summarize([dict(model_reconciliation=record)])
        assert result['recorded_disagreements'] == 0
        assert result['reasons'] == {selection_reason_hint(intent) or 'none': 1}


def test_recorded_verdict_disagreement_counted():
    record = observe_compilation(request_override='a', session_selected=None,
                                 compiled_model='b', intent_override=None)
    record['assessment']['verdict'] = 'exact_match'
    result = summarize([dict(model_reconciliation=record, actual_model='b')])
    assert result['recorded_disagreements'] == 1
    assert result['verdicts'] == {'unexplained': 1}


def test_status_is_not_execution_proof():
    for status in ['interrupted', 'stopped', 'unknown']:
        assert check_execution(request_override='a', compiled_model='a', actual_model=status)['verdict'] == 'unproven'


def test_missing_historical_request_never_guessed():
    result = summarize([dict(compiled_model='a', actual_model='b')])
    assert result['missing_record'] == 1
    assert result['verdicts'] == {'unproven': 1}


import json
from unittest.mock import patch

import pytest
from scripts import llm_model_reconciliation_verify as verify


@pytest.mark.parametrize('tags', [True, False])
def test_main_multiline_psql_success(tags, capsys):
    record = observe_compilation(request_override='a', session_selected='a',
                                 compiled_model='a', intent_override=None)
    payload = json.dumps([dict(model_reconciliation=record, actual_model='a')], indent=2)
    stdout = 'BEGIN\nSET\n' + payload + '\nROLLBACK' if tags else payload
    with patch.object(verify, 'psql', return_value=stdout) as query:
        assert verify.main() == 0
    sql = query.call_args.args[0]
    assert sql.startswith('BEGIN READ ONLY; SET LOCAL statement_timeout = 15000;')
    assert sql.endswith('ROLLBACK;')
    assert json.loads(capsys.readouterr().out)['rows'] == 1


@pytest.mark.parametrize('stdout', ['', 'BEGIN\nSET\nROLLBACK', '[\n{bad}\n]',
                                    '{}', 'null', '[1]', '[]\nWARNING', '[]\n[]'])
def test_main_invalid_payload_is_unverified(stdout, capsys):
    with patch.object(verify, 'psql', return_value=stdout):
        assert verify.main() == 2
    captured = capsys.readouterr()
    assert not captured.out
    assert json.loads(captured.err)['status'] == 'unverified'


@pytest.mark.parametrize('stdout', ['[]', 'BEGIN\nSET\n[]\nROLLBACK'])
def test_main_empty_rows_not_success(stdout, capsys):
    with patch.object(verify, 'psql', return_value=stdout):
        assert verify.main() == 2
    assert json.loads(capsys.readouterr().out)['rows'] == 0


def test_main_db_failure(capsys):
    with patch.object(verify, 'psql', side_effect=RuntimeError('DB unavailable')):
        assert verify.main() == 2
    assert json.loads(capsys.readouterr().err) == dict(status='unverified', error='RuntimeError')


@pytest.mark.parametrize('tamper', [False, True])
def test_main_mismatch_exit_one(tamper, capsys):
    record = observe_compilation(request_override='a', session_selected='a',
                                 compiled_model='b', intent_override=None)
    if tamper:
        record['assessment']['verdict'] = 'exact_match'
    with patch.object(verify, 'psql', return_value=json.dumps([
            dict(model_reconciliation=record, actual_model='b')])):
        assert verify.main() == 1
    result = json.loads(capsys.readouterr().out)
    assert result['verdicts'] == {'unexplained': 1}
    assert result['recorded_disagreements'] == int(tamper)


def test_main_missing_record_exit_two(capsys):
    with patch.object(verify, 'psql', return_value='[{"compiled_model":"a","actual_model":"a"}]'):
        assert verify.main() == 2
    result = json.loads(capsys.readouterr().out)
    assert result['missing_record'] == 1
    assert result['verdicts'] == {'unproven': 1}


@pytest.mark.parametrize('db_rc', [0, 1, 2])
def test_main_psql_subprocess_boundary(db_rc, capsys):
    from subprocess import CompletedProcess
    from scripts import error_book
    stdout = 'BEGIN\nSET\n[\n {"compiled_model":"a","actual_model":"a"}\n]\nROLLBACK'
    with patch.object(error_book, '_psql_argv', return_value=(['psql'], {})), \
         patch.object(error_book.subprocess, 'run', return_value=CompletedProcess(
             ['psql'], db_rc, stdout=stdout, stderr='database failure')) as run:
        assert verify.main() == 2
    assert run.call_args.kwargs['timeout'] == 60
    captured = capsys.readouterr()
    if db_rc:
        assert not captured.out
        assert json.loads(captured.err)['error'] == 'RuntimeError'
    else:
        assert json.loads(captured.out)['missing_record'] == 1
