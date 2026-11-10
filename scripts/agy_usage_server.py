#!/usr/bin/env python3
"""Read-only host quota service; independent of in-flight model relay calls."""
import hmac
import os
import sys
from pathlib import Path

from aiohttp import web

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.account_login import _antigravity_binding, ANTIGRAVITY_BIN
from scripts.agy_usage import UsageCollector

collector = UsageCollector(timeout=25)
SECRET_PATH = Path('/root/aads/aads-server/scripts/claude_relay_secret.txt')


async def usage(request):
    secret = SECRET_PATH.read_text().strip()
    if not secret or not hmac.compare_digest(request.headers.get('X-Claude-Relay-Secret', ''), secret):
        raise web.HTTPForbidden()
    binding = _antigravity_binding()
    env = {k: v for k, v in os.environ.items() if k in ('HOME', 'PATH', 'LANG', 'LC_ALL', 'TZ')}
    env['TERM'] = 'xterm-256color'
    observation = await collector.get(binding, str(ANTIGRAVITY_BIN), env, [], wait_timeout=.2)
    latest = _antigravity_binding()
    if any(binding.get(k) != latest.get(k) for k in ('account', 'bound', 'needs_login')):
        collector.observe_binding(latest, env)
        return web.json_response(dict(latest, usage_status='unknown', groups=[], quota_account=None))
    binding.update(observation)
    if observation.get('runtime_auth_status') == 'login_required':
        binding.update(bound=False, needs_login=True, account_verified=False)
    else:
        binding['account_verified'] = observation.get('runtime_auth_status') == 'verified'
        if not observation.get('quota_account'):
            binding['bound'] = False
    return web.json_response(binding)


def main():
    app = web.Application()
    app.router.add_get('/usage', usage)
    web.run_app(app, host=os.getenv('AGY_USAGE_BIND', '127.0.0.1'), port=8206, access_log=None)


if __name__ == '__main__':
    main()
