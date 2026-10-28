"""Execute the production stream error branch with a fake relay and DB boundary."""
import ast
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

SOURCE = Path('app/services/chat_service.py')


def stream_harness():
    tree = ast.parse(SOURCE.read_text())
    fn = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'send_message_stream')
    branch = next(n for n in ast.walk(fn) if isinstance(n, ast.If) and "etype" in ast.unparse(n.test) and "error" in ast.unparse(n.test) and any(isinstance(c, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "_terminal_err_event" for t in c.targets) for c in n.body))
    # Keep the real branch, including its retry break, persistence and return.
    body = ast.unparse(branch)
    code = '''async def run(events, stats, full_response='부분 응답'):
    session_id = 'fake-session'
    _execution_id_str = 'fake-execution'
    model_used = 'claude-sonnet'
    intent_result = SimpleNamespace(model=model_used)
    cost_usd = 0
    input_tokens = output_tokens = 0
    _turn_contract = SimpleNamespace(user_pinned=False)
    for _stream_attempt in range(3):
        stats.append('model_call')
        for event in events:
            etype = event['type']
'''
    code += '\n'.join('            ' + line for line in body.splitlines()) + '\n'
    class Pool:
        def acquire(self):
            return self
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
    mark = AsyncMock()
    env = {'json': json, 'logger': logging.getLogger(__name__), 'SimpleNamespace': SimpleNamespace,
               '_safe_fallback_reason': lambda s: s, '_heartbeat_asyncio': SimpleNamespace(sleep=AsyncMock()),
               '_save_interrupted_partial_message': AsyncMock(return_value={'content': '부분 응답'}),
               '_has_meaningful_partial_content': bool, 'get_pool': Pool,
               '_mark_execution_interrupted': mark}
    exec(code, env)  # noqa: S102 - execute only the local production AST
    return env['run'], mark


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['error', 'interrupted'])
async def test_busy_terminal_calls_once_and_preserves_partial(kind):
    run, mark = stream_harness()
    stats = []
    event = {'type': kind, 'content': 'oauth_slot_busy', 'terminal': True, 'reason': 'oauth_slot_busy', 'error_type': 'oauth_slot_busy'}
    out = [json.loads(s.removeprefix('data: ')) async for s in run([event], stats)]
    assert len(stats) == 1
    assert out[-1]['terminal'] is True
    assert out[-1]['stream_status'] == 'interrupted'
    assert out[-1]['reason'] == 'oauth_slot_busy'
    assert [e['type'] for e in out] == (['interrupted', 'error'] if kind == 'interrupted' else ['error'])
    assert not any(e['type'] == 'stream_reset' for e in out)
    assert mark.await_args.args[3] == 'oauth_slot_busy'
    assert mark.await_args.kwargs['partial_content'] == '부분 응답'


@pytest.mark.asyncio
@pytest.mark.parametrize('content', ['timeout', '429 rate limit', '529 overloaded'])
async def test_transient_keeps_three_attempt_limit(content):
    run, mark = stream_harness()
    stats = []
    out = [s async for s in run([{'type': 'error', 'content': content}], stats)]
    assert len(stats) == 3
    assert mark.await_count == 1
    assert out


@pytest.mark.asyncio
async def test_busy_db_interruption_preserves_reason_partial_and_blocks_resume(monkeypatch):
    from uuid import uuid4

    from app.services import chat_service as svc

    sid, eid, pid = uuid4(), uuid4(), uuid4()
    conn = AsyncMock()
    conn.fetchrow.return_value = {
        'status': 'running', 'owner_instance': svc._EXECUTION_OWNER_INSTANCE,
        'owner_epoch': 2, 'lease_valid': True, 'completed_at': None,
    }
    conn.fetchval.side_effect = lambda query, *args: eid if 'RETURNING id' in query else None
    monkeypatch.setattr(svc, '_is_local_active_api_slot', lambda: True)
    assert svc._classify_interruption_reason('oauth_slot_busy') == 'llm_provider_error'
    assert svc._classify_interruption_reason('terminal_provider:active_stream_hard_timeout') == 'llm_provider_error'
    assert not svc._should_auto_resume_interrupted_reason('oauth_slot_busy')
    assert not svc._should_auto_resume_interrupted_reason('terminal_provider:timeout')
    assert not await svc._schedule_interrupted_auto_resume(conn, str(sid), str(eid), pid, '부분 응답', 'oauth_slot_busy')
    assert conn.mock_calls == []
    await svc._mark_execution_interrupted(conn, str(sid), str(eid), 'oauth_slot_busy',
                                         partial_content='부분 응답', placeholder_id=str(pid), expected_owner_epoch=2)
    status_write = next(c for c in conn.fetchval.await_args_list if 'RETURNING id' in c.args[0])
    assert "status = 'interrupted'" in status_write.args[0]
    assert status_write.args[2] == 'oauth_slot_busy'
    message_write = next(c for c in conn.execute.await_args_list if 'intent = $3' in c.args[0])
    assert '부분 응답' in message_write.args[1]
    assert 'is_hidden = FALSE' in message_write.args[0]
    assert not any('DELETE FROM chat_messages' in c.args[0] for c in conn.execute.await_args_list)
