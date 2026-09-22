import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from importlib import import_module

tabs = import_module("pc_agent.commands.browser_tab")


@pytest.fixture(autouse=True)
def clean_tabs():
    tabs._SESSIONS.clear()
    tabs._OPEN_LOCK = asyncio.Lock()
    yield
    tabs._SESSIONS.clear()


def make_tab(port=9333, key='chat-pc-a', target='target-a'):
    tab = tabs.Tab(port, key, target, SimpleNamespace(close=AsyncMock()))
    tab.send = AsyncMock(return_value={})
    tab.state = AsyncMock(return_value={'url': 'https://example.com', 'width': 800, 'height': 600, 'epoch': 1})
    tab.frames['frame-a'] = (time.monotonic(), {'url': 'https://example.com', 'width': 800, 'height': 600, 'epoch': 1})
    return tab


@pytest.mark.asyncio
async def test_parallel_inputs_are_bound_to_distinct_tabs():
    a, b = make_tab(), make_tab(9444, 'chat-pc-b', 'target-b')
    tabs._SESSIONS.update(token_a=a, token_b=b)
    calls = []
    async def request(tab, x):
        result = await tabs.execute({'op': 'control', 'tab_token': tab, 'work_key': tabs._SESSIONS[tab].work_key,
            'port': tabs._SESSIONS[tab].port, 'action': 'click', 'frame_id': 'frame-a', 'x': x, 'y': 20})
        calls.append(result)
    await asyncio.gather(request('token_a', 10), request('token_b', 100))
    assert all(r['status'] == 'success' for r in calls)
    assert a.send.call_args_list[0].args[1]['x'] == 10
    assert b.send.call_args_list[0].args[1]['x'] == 100
    assert all(call.args[0].startswith('Input.') for tab in (a,b) for call in tab.send.call_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', ['old', 'navigation', 'missing', 'nan', 'outside'])
async def test_stale_or_invalid_input_never_dispatched(mutation):
    tab = make_tab()
    params = {'action': 'click', 'frame_id': 'frame-a', 'x': 10, 'y': 20}
    if mutation == 'old': tab.frames['frame-a'] = (time.monotonic() - 6, tab.frames['frame-a'][1])
    if mutation == 'navigation': tab.state.return_value = {**tab.state.return_value, 'epoch': 2}
    if mutation == 'missing': params['frame_id'] = 'absent'
    if mutation == 'nan': params['x'] = float('nan')
    if mutation == 'outside': params['x'] = 800
    with pytest.raises(ValueError): await tab.control(params)
    tab.send.assert_not_called()


@pytest.mark.asyncio
async def test_wrong_work_scope_and_expired_token_rejected():
    tab = make_tab()
    tabs._SESSIONS['token-a'] = tab
    for key, age in [('chat-pc-b', 0), ('chat-pc-a', 61)]:
        tab.touched = time.monotonic() - age
        result = await tabs.execute({'op': 'control', 'tab_token': 'token-a', 'port': 9333, 'work_key': key})
        assert result['data']['error'] == 'tab_binding_expired'
    tab.send.assert_not_called()


@pytest.mark.asyncio
async def test_closed_explicit_target_never_falls_back(monkeypatch):
    monkeypatch.setattr(tabs.cdp.CDPSessionManager, 'get_session', lambda key: SimpleNamespace(port=9333))
    monkeypatch.setattr(tabs.cdp, '_list_cdp_targets', AsyncMock(return_value=[{'id':'other','type':'page'}]))
    connect = AsyncMock()
    monkeypatch.setattr(tabs.cdp, '_connect_cdp_ws', connect)
    result = await tabs.execute({'op':'open','port':9333,'work_key':'chat-pc-a','target_id':'closed'})
    assert result['data']['error'] == 'explicit_tab_required'
    connect.assert_not_called()


@pytest.mark.asyncio
async def test_duplicate_owner_rejected_and_close_only_detaches(monkeypatch):
    monkeypatch.setattr(tabs.cdp.CDPSessionManager, 'get_session', lambda key: SimpleNamespace(port=9333))
    tab = make_tab()
    tabs._SESSIONS['token-a'] = tab
    result = await tabs.execute({'op':'open','port':9333,'work_key':'chat-pc-a'})
    assert result['data']['error'] == 'tab_already_in_use'
    result = await tabs.execute({'op':'close','port':9333,'work_key':'chat-pc-a','tab_token':'token-a'})
    assert result['status'] == 'success'
    tab.ws.close.assert_awaited_once()
    tab.send.assert_not_called()


@pytest.mark.asyncio
async def test_password_text_not_returned_and_focus_required():
    tab = make_tab()
    tab.send.return_value = {'result': {'value': True}}
    result = await tab.control({'action':'type','text':'secret-not-in-result','frame_id':'frame-a'})
    assert 'secret-not-in-result' not in str(result)
    tab.send.return_value = {'result': {'value': False}}
    with pytest.raises(ValueError, match='focus_input_first'):
        await tab.control({'action':'type','text':'secret','frame_id':'frame-a'})


@pytest.mark.asyncio
async def test_profile_port_mismatch_rejected(monkeypatch):
    monkeypatch.setattr(tabs.cdp.CDPSessionManager, 'get_session', lambda key: SimpleNamespace(port=9444))
    result = await tabs.execute({'op':'open','port':9333,'work_key':'chat-pc-a'})
    assert result['data']['error'] == 'tab_profile_mismatch'
