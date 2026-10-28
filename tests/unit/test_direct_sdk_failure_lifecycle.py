"""A failed SDK resume must never become a successful empty response."""
import ast
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from app.services import agent_sdk_service as sdk


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['missing_session', 'error_result', 'empty_stream', 'cancelled'])
async def test_failed_sdk_never_emits_completion(monkeypatch, failure):
    import asyncio
    class Result:
        is_error = True
        result = 'provider failed'
    class System:
        pass
    class Client:
        connect = AsyncMock()
        disconnect = AsyncMock()
        async def query(self, prompt):
            if failure == 'missing_session':
                raise RuntimeError('No conversation found with session ID: stale')
            if failure == 'cancelled':
                raise asyncio.CancelledError()
        async def receive_response(self):
            if failure == 'error_result':
                yield Result()
    client = Client()
    monkeypatch.setattr(sdk, 'ClaudeSDKClient', lambda **kw: client)
    monkeypatch.setattr(sdk, 'ResultMessage', Result)
    monkeypatch.setattr(sdk, 'SystemMessage', System)
    events = []
    expected = asyncio.CancelledError if failure == 'cancelled' else sdk.AgentSDKExecutionError
    with pytest.raises(expected) as caught:
        async for line in sdk.AgentSDKService()._execute_stream_bidirectional('fix', None, 'sid', set(), 'key'):
            events.append(json.loads(line.removeprefix('data: ')))
    assert not any(e['type'] == 'sdk_complete' for e in events)
    assert client.disconnect.await_count == 1
    if failure == 'missing_session':
        assert caught.value.reason == 'sdk_session_not_found'


@pytest.mark.asyncio
async def test_successful_sdk_has_one_completion_after_cleanup(monkeypatch):
    class Result:
        is_error = False
    class System:
        pass
    client = SimpleNamespace(connect=AsyncMock(), query=AsyncMock(), disconnect=AsyncMock())
    async def receive():
        yield Result()
    async def render(message):
        yield 'data: {"type":"delta","content":"verified"}\n\n'
    client.receive_response = receive
    monkeypatch.setattr(sdk, 'ClaudeSDKClient', lambda **kw: client)
    monkeypatch.setattr(sdk, 'ResultMessage', Result)
    monkeypatch.setattr(sdk, 'SystemMessage', System)
    monkeypatch.setattr('app.core.interrupt_queue.has_interrupt', lambda sid: False)
    service = sdk.AgentSDKService()
    monkeypatch.setattr(service, '_render_sdk_message', render)
    events = [json.loads(line.removeprefix('data: ')) async for line in service._execute_stream_bidirectional('fix', None, 'sid', set(), 'key')]
    assert [e['type'] for e in events] == ['delta', 'sdk_complete']
    client.disconnect.assert_awaited_once()


@pytest.mark.parametrize('model', ['codex:gpt-6-astra', 'codex:gpt-6.1-sol', 'claude-opus-5-5', 'gemini-pro'])
def test_explicit_model_cannot_enter_claude_sdk(model):
    tree = ast.parse(Path('app/services/chat_service.py').read_text())
    fn = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'send_message_stream')
    branch = next(n for n in ast.walk(fn) if isinstance(n, ast.If) and '_DIRECT_EXECUTION_TRIGGERS' in ast.unparse(n.test))
    expr = compile(ast.Expression(branch.test), '<production route>', 'eval')
    env = {'response_mode':'quality', '_RESPONSE_MODE_PLAN':'plan', 'intent':'code_modify', '_RUNNER_DELEGATION_INTENTS':{'code_modify'}, 'content':'직접 수정하고 보고해', '_DIRECT_EXECUTION_TRIGGERS':('직접 수정',), '_turn_contract':SimpleNamespace(user_pinned=True, requested_model=model)}
    assert eval(expr, env) is False
