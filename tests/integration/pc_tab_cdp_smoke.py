"""Real Chromium/CDP smoke; two isolated profiles. No PC desktop or production writes.
Run: PYTHONPATH=. python tests/integration/pc_tab_cdp_smoke.py
"""
import asyncio
import base64
import json
import socket
from pathlib import Path

from playwright.async_api import async_playwright
from importlib import import_module

tabs = import_module("pc_agent.commands.browser_tab")


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


async def main():
    evidence = Path('/tmp/aads-pc-tab-evidence')
    evidence.mkdir(exist_ok=True)
    async with async_playwright() as p:
        browsers = []
        bindings = []
        try:
            for label in ('A', 'B'):
                port = free_port()
                browser = await p.chromium.launch(headless=True, args=['--no-sandbox', f'--remote-debugging-port={port}'])
                browsers.append(browser)
                page = await browser.new_page(viewport={'width': 800, 'height': 600})
                await page.set_content(f'<body style="background:{"#dbeafe" if label == "A" else "#dcfce7"};font:24px sans-serif"><h1>Isolated chat {label}</h1><input id="value" style="position:absolute;left:20px;top:100px;width:300px;height:50px;font-size:24px"><button onclick="document.title=\'clicked-{label}\'" style="position:absolute;left:20px;top:180px">Click {label}</button></body>')
                key = 'chat-pc-smoke-' + label
                tabs.cdp.CDPSessionManager._sessions[key] = tabs.cdp.CDPSession(key, port, '/tmp/smoke')
                opened = await tabs.execute({'op':'open','port':port,'work_key':key})
                assert opened['status'] == 'success', opened
                bindings.append((page, {'port':port,'work_key':key, 'tab_token':opened['data']['tab_token']}, opened['data']['target_id']))
            async def operate(label, item):
                page, binding, target = item
                frame = await tabs.execute({**binding,'op':'frame'})
                assert frame['status'] == 'success', frame
                fid = frame['data']['frame_id']
                for params in ({'action':'click','x':50,'y':125}, {'action':'type','text':'chat-'+label}):
                    result = await tabs.execute({**binding,'op':'control','frame_id':fid,**params})
                    assert result['status'] == 'success', result
                assert await page.locator('#value').input_value() == 'chat-'+label
                result = await tabs.execute({**binding,'op':'control','frame_id':fid,'action':'click','x':50,'y':190})
                assert result['status'] == 'success', result
                assert await page.title() == 'clicked-'+label
                frame = await tabs.execute({**binding,'op':'frame'})
                (evidence/f'chat-{label}.jpg').write_bytes(base64.b64decode(frame['data']['frame']))
                await tabs.execute({**binding,'op':'close'})
                reopened = await tabs.execute({**binding,'op':'open','target_id':target})
                assert reopened['status'] == 'success', reopened
                binding['tab_token'] = reopened['data']['tab_token']
                assert await page.locator('#value').input_value() == 'chat-'+label
            await asyncio.gather(*(operate(label,item) for label,item in zip(('A','B'), bindings)))
            # Never pick another tab after the bound page has closed.
            page, binding, target = bindings[0]
            await page.close()
            result = await tabs.execute({**binding,'op':'frame'})
            assert result['status'] == 'error', result
            await tabs.execute({**binding,'op':'close'})
            await browsers[0].new_page()
            result = await tabs.execute({**binding,'op':'open','target_id':target})
            assert result['status'] == 'error', result
            report = {'status':'passed','profiles':2,'concurrent_input_isolated':True,'clicks_isolated':True,
                      'reconnect_preserved':True,'closed_target_rejected':True,'environment':'local headless Chromium; Windows PC deployment not tested'}
            (evidence/'result.json').write_text(json.dumps(report, indent=2))
            print(json.dumps(report))
        finally:
            for _, binding, _ in bindings:
                await tabs.execute({**binding,'op':'close'})
            for browser in browsers:
                await browser.close()

if __name__ == '__main__':
    asyncio.run(main())
