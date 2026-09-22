"""Chat-scoped PC tab stream. Cross-slot ownership is fenced by PostgreSQL."""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json

import anyio
from uuid import UUID

from fastapi import WebSocket, WebSocketDisconnect

from app.auth import verify_token
from app.core.db_pool import get_pool
from app.browser_bridge.service import get_browser_bridge_service
from app.services.pc_agent_manager import pc_agent_manager

def chat_work_key(tenant: str, chat: str, agent: str) -> str:
    return 'chat-pc-' + hashlib.sha256(f'{tenant}:{chat}:{agent}'.encode()).hexdigest()[:40]


async def tab_command(service, agent_id: str, work_key: str, params: dict) -> dict:
    # A tab has its own DB lock and PC token. Do not enqueue frames behind the
    # desktop-wide execution lease, or independent chats would serialize.
    command_id = await pc_agent_manager.send_command(agent_id, 'browser_tab', params)
    try:
        result = await pc_agent_manager.get_result(command_id, timeout=15)
        data = dict(result.result or {})
        if result.status != 'success' or data.get('error'):
            allowed = {'stale_frame_retry', 'focus_input_first', 'explicit_tab_required', 'tab_already_in_use',
                       'tab_binding_expired', 'page_changed_retry', 'tab_connection_lost', 'invalid_coordinate',
                       'invalid_text_length', 'unsupported_key', 'unsupported_control_action'}
            error = str(data.get('error') or '')
            raise ValueError(error if error in allowed else 'pc_tab_unavailable_or_update_required')
    finally:
        pc_agent_manager.forget_result(command_id)
    return data


async def chat_pc_live(websocket: WebSocket, chat_id: str) -> None:
    token = websocket.query_params.get('access_token') or websocket.cookies.get('aads_token') or ''
    principal = verify_token(token)
    if not principal or not principal.get('sub') or not principal.get('tenant_id'):
        await websocket.close(code=4401, reason='authentication_required')
        return
    # PC control grants privileged access to a physical computer. Keep the first
    # version admin-only, never inherit access from the global active session.
    if not principal.get('is_admin'):
        await websocket.close(code=4403, reason='pc_admin_required')
        return
    try:
        chat_uuid, tenant_uuid = UUID(chat_id), UUID(str(principal['tenant_id']))
    except ValueError:
        await websocket.close(code=4404, reason='chat_not_found')
        return
    async with get_pool().acquire() as conn:
        owned = await conn.fetchval('SELECT 1 FROM chat_sessions WHERE id=$1 AND tenant_id=$2', chat_uuid, tenant_uuid)
    if not owned:
        await websocket.close(code=4404, reason='chat_not_found')
        return
    await websocket.accept()
    service = get_browser_bridge_service()
    tab_token = ''
    binding: dict = {}
    jobs: set[asyncio.Task] = set()
    try:
        raw_hello = await asyncio.wait_for(websocket.receive_text(), timeout=10)
        if len(raw_hello) > 4096:
            raise ValueError('invalid_pc_request')
        hello = json.loads(raw_hello)
        if not isinstance(hello, dict):
            raise ValueError('invalid_pc_request')
        agent_id = str(hello.get('agent_id') or '')
        url = str(hello.get('url') or '')
        if not agent_id or len(agent_id) > 120 or len(url) > 2048:
            raise ValueError('invalid_pc_request')
        if url:
            from urllib.parse import urlparse
            parsed = urlparse(url)
            if parsed.scheme not in {'http', 'https'} or not parsed.netloc:
                raise ValueError('unsupported_target_url')
        agent = pc_agent_manager.get_agent(agent_id)
        if not agent:
            raise ValueError('pc_offline')
        if str(agent.tenant_id or '') != str(tenant_uuid):
            raise ValueError('pc_tenant_mismatch')
        if 'browser_tab' not in agent.command_types:
            raise ValueError('pc_update_required')
        work_key = chat_work_key(str(tenant_uuid), str(chat_uuid), agent_id)
        lock_key = int.from_bytes(hashlib.sha256(work_key.encode()).digest()[:8], 'big', signed=True)
        # Holding this dedicated pool connection preserves an advisory lock across
        # API slots. Loss of the connection fails the next frame before input.
        async with get_pool().acquire() as lock_conn:
            if not await lock_conn.fetchval('SELECT pg_try_advisory_lock($1)', lock_key):
                raise ValueError('chat_browser_already_in_use')
            try:
                session = await service.ensure_work_session(work_key=work_key, agent_id=agent_id,
                    label='채팅 전용 PC 브라우저', url='about:blank', command_timeout_seconds=45, queue_wait_timeout_seconds=10)
                meta = session.endpoint.metadata
                binding = {'port': int(meta['port']), 'work_key': work_key}
                await lock_conn.fetchval('SELECT 1')
                opened = await tab_command(service, agent_id, work_key, {
                    **binding, 'op': 'open', 'url': url, 'target_id': meta.get('live_target_id', '')})
                tab_token = opened['tab_token']
                binding['tab_token'] = tab_token
                meta['live_target_id'] = opened['target_id']
                service.sessions.touch(session)
                await websocket.send_json({'type': 'ready', 'browser_session_id': session.session_id,
                    'target_id': opened['target_id'], 'runtime': 'pc_tab_cdp'})
                send_lock = asyncio.Lock()
                command_lock = asyncio.Lock()

                async def send(payload: dict) -> None:
                    async with send_lock:
                        await websocket.send_json(payload)

                async def execute(op: str, payload: dict | None = None) -> dict:
                    async with command_lock:
                        if not verify_token(token):
                            raise ValueError('authentication_required')
                        await lock_conn.fetchval('SELECT 1')
                        return await tab_command(service, agent_id, work_key, {**(payload or {}), **binding, 'op': op})

                async def frames() -> None:
                    while True:
                        try:
                            frame = await execute('frame')
                        except ValueError as exc:
                            if str(exc) == 'page_changed_retry':
                                await asyncio.sleep(.33)
                                continue
                            raise
                        await send(frame)
                        await asyncio.sleep(.33)

                async def controls() -> None:
                    while True:
                        raw = await websocket.receive_text()
                        if len(raw) > 16000:
                            raise ValueError('control_too_large')
                        message = json.loads(raw)
                        if not isinstance(message, dict):
                            raise ValueError('invalid_control')
                        if message.get('type') == 'ping':
                            await send({'type': 'pong'})
                            continue
                        if message.get('type') != 'control':
                            raise ValueError('unsupported_message')
                        try:
                            result = await execute('control', {k: message[k] for k in
                                ('action', 'x', 'y', 'deltaY', 'text', 'key', 'replace', 'frame_id') if k in message})
                            await send({'type': 'control_ack', 'request_id': message.get('request_id'), 'result': result})
                        except ValueError as exc:
                            await send({'type': 'control_error', 'request_id': message.get('request_id'), 'error': str(exc)})

                jobs = {asyncio.create_task(frames()), asyncio.create_task(controls())}
                done, _ = await asyncio.wait(jobs, return_when=asyncio.FIRST_COMPLETED)
                for job in done:
                    job.result()
            finally:
                # ASGI cancellation must not interrupt tab detach or DB unlock.
                with anyio.CancelScope(shield=True):
                    for job in jobs:
                        job.cancel()
                    if jobs:
                        await asyncio.gather(*jobs, return_exceptions=True)
                    if tab_token:
                        with contextlib.suppress(Exception):
                            await asyncio.wait_for(tab_command(service, agent_id, work_key, {**binding, 'op': 'close'}), timeout=5)
                    with contextlib.suppress(Exception):
                        await lock_conn.execute('SELECT pg_advisory_unlock($1)', lock_key)
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        with contextlib.suppress(Exception):
            await websocket.send_json({'type': 'error', 'error': str(exc) if isinstance(exc, ValueError) else 'pc_tab_connection_failed'})
    finally:
        with contextlib.suppress(Exception):
            await websocket.close()
