"""Isolated AGY OAuth login. Keep the current credential until new login succeeds."""
import asyncio
import json
import os
import pty
import re
import secrets
import shutil
import signal
import tempfile
import time
from pathlib import Path

from scripts.account_login import ANTIGRAVITY_BIN, ANTIGRAVITY_HOME_ROOT

_sessions = {}
_lock = asyncio.Lock()
_TERMINAL = {'success', 'failed', 'expired', 'cancelled'}


def public(s):
    return {k: s.get(k) for k in ('login_id', 'kind', 'account', 'needs_code', 'state', 'message', 'url')} | {
        'expires_in': max(0, int(s['deadline'] - time.monotonic())), 'user_code': None,
    }


def validate_code(code):
    code = (code or '').strip()
    if not re.fullmatch(r'[A-Za-z0-9_\-#/=+.]{4,4096}', code):
        raise ValueError('Google 인증 후 받은 authorization code를 붙여넣으세요')
    return code


async def pump(s):
    output = ''
    selected = False
    credential = s['home'] / '.gemini/antigravity-cli/antigravity-oauth-token'
    try:
        while s['state'] not in _TERMINAL:
            if time.monotonic() >= s['deadline']:
                s.update(state='expired', message='인증 시간이 만료됐습니다. 다시 시도하세요.'); break
            try:
                chunk = os.read(s['master'], 65536)
            except BlockingIOError:
                await asyncio.sleep(.1); continue
            except OSError:
                chunk = b''
            if not chunk:
                s.update(state='failed', message='CLI 인증이 종료됐습니다. 다시 시도하세요.'); break
            output = (output + chunk.decode(errors='replace'))[-131072:]
            if not selected and 'Select login method' in output:
                os.write(s['master'], b'\r'); selected = True
            match = re.search(r'https://accounts\.google\.com/[^\s\x1b\x07]+', output)
            if match and not s['url']:
                s.update(url=match[0], state='awaiting_code', message='Google 인증 후 표시된 authorization code를 붙여넣으세요. 현재 계정은 인증 완료 전까지 유지됩니다.')
            if credential.is_file() and '? for shortcuts' in output:
                data = json.loads(credential.read_text())
                token = data.get('token') or {}
                if not token.get('access_token') or not token.get('refresh_token'):
                    continue
                # The CLI completed authentication in its isolated HOME. Only promote
                # its own credential after the ready screen, without logging any token.
                destination = ANTIGRAVITY_HOME_ROOT / 'antigravity-cli/antigravity-oauth-token'
                destination.parent.mkdir(parents=True, exist_ok=True)
                fd, temp = tempfile.mkstemp(prefix='.agy-login-', dir=destination.parent)
                try:
                    with os.fdopen(fd, 'wb') as out:
                        out.write(credential.read_bytes()); out.flush(); os.fsync(out.fileno())
                    os.replace(temp, destination)
                finally:
                    if os.path.exists(temp): os.unlink(temp)
                s.update(state='success', message='Google OAuth 로그인 완료. 사용량을 다시 확인합니다.'); break
    except Exception:
        s.update(state='failed', message='AGY 인증을 완료하지 못했습니다. 기존 계정은 유지됩니다.')
    finally:
        try: os.killpg(s['proc'].pid, signal.SIGKILL)
        except ProcessLookupError: pass
        await s['proc'].wait()
        os.close(s['master'])
        # Group termination is asynchronous; the CLI has logging children.
        # Wait before removing their isolated HOME so no child can recreate it.
        for _ in range(60):
            try: os.killpg(s['proc'].pid, 0)
            except ProcessLookupError: break
            await asyncio.sleep(.05)
        for attempt in range(20):
            try:
                shutil.rmtree(s['home']); break
            except FileNotFoundError: break
            except OSError:
                if attempt == 19:
                    s.update(state='failed', message='인증 프로세스 정리를 확인하지 못했습니다. 다시 시도하세요.')
                else: await asyncio.sleep(.05)
        s['finished_at'] = time.monotonic()


async def start():
    async with _lock:
        for key, value in list(_sessions.items()):
            if value.get('finished_at', float('inf')) + 600 < time.monotonic(): _sessions.pop(key)
        active = next((s for s in _sessions.values() if s['state'] not in _TERMINAL), None)
        if active: return public(active)
        home = Path(tempfile.mkdtemp(prefix='aads-agy-login-'))
        master, slave = pty.openpty()
        env = {k: v for k, v in os.environ.items() if k in ('PATH', 'LANG', 'LC_ALL', 'TZ')}
        env.update(HOME=str(home), TERM='xterm-256color')
        try:
            proc = await asyncio.create_subprocess_exec(str(ANTIGRAVITY_BIN), stdin=slave, stdout=slave,
                stderr=slave, env=env, cwd=str(home), start_new_session=True)
        except Exception:
            os.close(master); shutil.rmtree(home); raise
        finally: os.close(slave)
        os.set_blocking(master, False)
        s = dict(login_id='agy-'+secrets.token_urlsafe(12), kind='antigravity', account='AGY Google OAuth',
                 needs_code=True, state='starting', message='', url=None, deadline=time.monotonic()+900,
                 home=home, master=master, proc=proc)
        _sessions[s['login_id']] = s
        s['task'] = asyncio.create_task(pump(s))
    for _ in range(50):
        if s['url'] or s['state'] in _TERMINAL: break
        await asyncio.sleep(.1)
    return public(s)


def get(login_id):
    s = _sessions.get(login_id)
    if s is None: raise ValueError('로그인 세션이 없습니다. 다시 시도하세요.')
    return s


async def submit(login_id, code):
    s = get(login_id)
    if s['state'] != 'awaiting_code': raise ValueError('지금은 인증 코드를 받을 수 없습니다')
    code = validate_code(code)
    os.write(s['master'], (code + '\r').encode())
    s.update(state='verifying', message='Google OAuth 로그인 완료를 확인합니다.')
    return public(s)


async def cancel(login_id):
    s = get(login_id)
    if s['state'] not in _TERMINAL:
        s.update(state='cancelled', message='취소했습니다. 기존 계정은 유지됩니다.')
        await s['task']
    return public(s)
