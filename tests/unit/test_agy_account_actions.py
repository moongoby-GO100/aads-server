import asyncio
import json
import textwrap
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from scripts import agy_login


@pytest.mark.parametrize('code', ['', 'x\ny', 'https://evil.example/', 'a'*4097])
def test_login_rejects_invalid_code(code):
    with pytest.raises(ValueError): agy_login.validate_code(code)


def test_login_accepts_google_code():
    assert agy_login.validate_code(' 4/0Abc-DEF_123.xyz ') == '4/0Abc-DEF_123.xyz'


def test_isolated_login_cancel_and_success(tmp_path, monkeypatch):
    cli = tmp_path/'fake-agy'
    cli.write_text('#!/usr/bin/env python3\n'+textwrap.dedent('''
        import os,sys,json,tty
        from pathlib import Path
        tty.setraw(0)
        print('Select login method:', flush=True)
        sys.stdin.read(1)
        print('https://accounts.google.com/o/oauth2/auth?state=qa',flush=True)
        code=''
        while True:
            ch=sys.stdin.read(1)
            if ch in ('\\r','\\n'):break
            code+=ch
        if code != '4/valid-code':sys.exit(1)
        p=Path.home()/'.gemini/antigravity-cli/antigravity-oauth-token'
        p.parent.mkdir(parents=True)
        p.write_text(json.dumps({'token':{'access_token':'test-only','refresh_token':'test-only'}}))
        print('? for shortcuts',flush=True)
        sys.stdin.read()
    '''))
    cli.chmod(0o700)
    destination=tmp_path/'live/antigravity-cli/antigravity-oauth-token'
    destination.parent.mkdir(parents=True);destination.write_text('existing credential sentinel')
    monkeypatch.setattr(agy_login,'ANTIGRAVITY_BIN',cli)
    monkeypatch.setattr(agy_login,'ANTIGRAVITY_HOME_ROOT',tmp_path/'live')
    monkeypatch.setattr(agy_login,'_sessions',{})
    async def run():
        s=await agy_login.start();assert s['state']=='awaiting_code'
        first=agy_login.get(s['login_id'])
        again=await agy_login.start();assert again['login_id']==s['login_id']
        await agy_login.cancel(s['login_id'])
        assert first['proc'].returncode is not None and not first['home'].exists()
        assert destination.read_text()=='existing credential sentinel'
        s=await agy_login.start();active=agy_login.get(s['login_id'])
        await agy_login.submit(s['login_id'],'4/valid-code')
        await asyncio.wait_for(active['task'],3)
        assert active['state']=='success'
        assert json.loads(destination.read_text())['token']['refresh_token']=='test-only'
        assert destination.stat().st_mode & 0o777 == 0o600
        assert not active['home'].exists() and active['proc'].returncode is not None
    asyncio.run(run())


@pytest.mark.parametrize('row,expected', [(None,True),({'is_active':True,'manual_disabled':False},True),({'is_active':False,'manual_disabled':False},False),({'is_active':True,'manual_disabled':True},False)])
def test_account_execution_guard(row,expected,monkeypatch):
    from app.core import antigravity_account
    conn=MagicMock();conn.fetchrow=AsyncMock(return_value=row)
    acquire=MagicMock();acquire.__aenter__=AsyncMock(return_value=conn);acquire.__aexit__=AsyncMock(return_value=False)
    pool=MagicMock();pool.acquire.return_value=acquire
    monkeypatch.setattr(antigravity_account,'get_pool',lambda:pool)
    assert asyncio.run(antigravity_account.antigravity_enabled()) is expected
