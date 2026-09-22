from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.browser_bridge import live_api as live

TENANT = '11111111-1111-4111-8111-111111111111'
CHAT = '22222222-2222-4222-8222-222222222222'
OTHER_CHAT = '33333333-3333-4333-8333-333333333333'


@pytest.fixture
def harness(monkeypatch):
    state = SimpleNamespace(principal={'sub':'admin','tenant_id':TENANT,'is_admin':True},
        chat_exists=True, locks=set(), commands=[], session_calls=[], online=True,
        agent_tenant=TENANT, types=['browser_tab'], next_id=0, pending={}, forgotten=[], lose_db=False)
    class Connection:
        async def fetchval(self, sql, *args):
            if 'chat_sessions' in sql: return state.chat_exists
            if 'pg_try_advisory_lock' in sql:
                if args[0] in state.locks: return False
                state.locks.add(args[0]); return True
            if state.lose_db: raise RuntimeError('db_connection_lost')
            return 1
        async def execute(self, sql, *args): state.locks.discard(args[0])
    class Pool:
        @asynccontextmanager
        async def acquire(self): yield Connection()
    async def ensure(**kwargs):
        state.session_calls.append(kwargs)
        return SimpleNamespace(endpoint=SimpleNamespace(metadata={'port':9333}),session_id='bridge-session')
    service = SimpleNamespace(ensure_work_session=ensure,sessions=SimpleNamespace(touch=lambda _:None))
    class Manager:
        def get_agent(self, agent_id):
            return SimpleNamespace(tenant_id=state.agent_tenant,command_types=state.types) if state.online else None
        async def send_command(self, agent_id, command_type, params):
            state.next_id += 1
            state.commands.append(dict(params));state.pending[state.next_id]=params
            return state.next_id
        async def get_result(self, command_id, timeout):
            params=state.pending[command_id]; op=params['op']
            data={'tab_token':'token-'+params['work_key'],'target_id':'target-a'} if op=='open' else {'type':'frame','frame':'ZmFrZQ==','frame_id':'f1','width':800,'height':600} if op=='frame' else {'action':params.get('action',op)}
            return SimpleNamespace(status='success',result=data)
        def forget_result(self, command_id): state.forgotten.append(command_id)
    monkeypatch.setattr(live,'verify_token',lambda _:state.principal)
    monkeypatch.setattr(live,'get_pool',lambda:Pool())
    monkeypatch.setattr(live,'get_browser_bridge_service',lambda:service)
    monkeypatch.setattr(live,'pc_agent_manager',Manager())
    app=FastAPI(); app.websocket('/chat/{chat_id}/pc-live')(live.chat_pc_live)
    with TestClient(app) as client: yield client,state


def connect(client,chat=CHAT):
    return client.websocket_connect(f'/chat/{chat}/pc-live?access_token=test')


@pytest.mark.parametrize('principal,code', [(None,4401),({'sub':'a','tenant_id':TENANT,'is_admin':False},4403)])
def test_authentication_before_pc_access(harness,principal,code):
    from starlette.websockets import WebSocketDisconnect
    client,state=harness;state.principal=principal
    with pytest.raises(WebSocketDisconnect) as e:
        with connect(client): pass
    assert e.value.code==code
    assert state.commands==[]


def test_other_tenant_chat_rejected(harness):
    from starlette.websockets import WebSocketDisconnect
    client,state=harness;state.chat_exists=False
    with pytest.raises(WebSocketDisconnect) as e:
        with connect(client): pass
    assert e.value.code==4404
    assert state.commands==[]


@pytest.mark.parametrize('field,value,error',[('online',False,'pc_offline'),('agent_tenant','other','pc_tenant_mismatch'),('types',[],'pc_update_required')])
def test_pc_access_fails_closed(harness,field,value,error):
    client,state=harness;setattr(state,field,value)
    with connect(client) as ws:
        ws.send_json({'agent_id':'pc-a'})
        assert ws.receive_json()['error']==error
    assert state.commands==[]


def test_duplicate_chat_lock_and_independent_chats(harness):
    client,state=harness
    with connect(client) as a:
        a.send_json({'agent_id':'pc-a'});assert a.receive_json()['type']=='ready'
        with connect(client) as duplicate:
            duplicate.send_json({'agent_id':'pc-a'})
            assert duplicate.receive_json()['error']=='chat_browser_already_in_use'
        with connect(client,OTHER_CHAT) as b:
            b.send_json({'agent_id':'pc-a'});assert b.receive_json()['type']=='ready'
            assert len(state.locks)==2
            assert a.receive_json()['type']=='frame'
            assert b.receive_json()['type']=='frame'
    assert state.locks==set()
    assert len({c['work_key'] for c in state.commands})==2


def test_client_cannot_override_scope_or_target(harness):
    client,state=harness
    with connect(client) as ws:
        ws.send_json({'agent_id':'pc-a'});ws.receive_json();ws.receive_json()
        ws.send_json({'type':'control','request_id':'r1','action':'click','x':10,'y':20,'frame_id':'f1',
                      'port':9222,'work_key':'attacker','tab_token':'attacker','target_id':'other'})
        while True:
            result=ws.receive_json()
            if result['type']=='control_ack': break
        assert result['request_id']=='r1'
    control=next(c for c in state.commands if c['op']=='control')
    assert control['port']==9333
    assert control['work_key']==live.chat_work_key(TENANT,CHAT,'pc-a')
    assert control['tab_token']!='attacker'
    assert 'target_id' not in control
    assert len(state.forgotten)==len(state.commands)


@pytest.mark.parametrize('failure', ['token', 'db'])
def test_expired_auth_or_lost_db_lock_stops_input(harness, failure):
    client,state=harness
    with connect(client) as ws:
        ws.send_json({'agent_id':'pc-a'});ws.receive_json();ws.receive_json()
        if failure=='token': state.principal=None
        else: state.lose_db=True
        ws.send_json({'type':'control','action':'click','x':10,'y':20,'frame_id':'f1'})
        while True:
            result=ws.receive_json()
            if result['type'] in ('error','control_error'): break
    assert not any(c['op']=='control' for c in state.commands)
