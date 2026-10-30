"""직원·급여 수정 검증: 로컬 정적 서버 + API 목, 운영 접근 없음."""
import asyncio
import functools
import json
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'tests/unit/.obys-people-e2e-evidence'


async def main():
    OUT.mkdir(parents=True, exist_ok=True)
    handler = functools.partial(SimpleHTTPRequestHandler, directory=str(ROOT / 'app'))
    server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    origin = f'http://127.0.0.1:{server.server_port}'
    results = []
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True, args=['--no-sandbox'])
            for scenario, legacy, empty_registry in [("legacy", True, False), ("legacy-empty-registry", True, True), ("unlinked", False, True)]:
                context = await browser.new_context(service_workers='block')
                await context.add_init_script("localStorage.setItem('aads_token','mock-only');")
                businesses = [{'id': 'legacy-store', 'name': '기존 테스트 매장', 'branches': [{'id': 'branch-test', 'name': '테스트 지점'}]}] if legacy else []
                session = {'legacy_access': legacy, 'user': {'email': 'owner@example.test'}, 'permissions': {'role': 'owner', 'can_manage_settings': True, 'can_manage_onboarding': True, 'can_view': True}, 'permissions_verified': True}
                requests, errors, forbidden, console_errors = [], [], [], []

                async def mock(route):
                    path = urlparse(route.request.url).path
                    requests.append(path)
                    if path.endswith('/session'): payload = session
                    elif path.endswith('/auth/me'): payload = {'tenant_id': 'test', 'membership': {'tenant_id': 'test', 'role': 'owner', 'status': 'active'}}
                    elif path.endswith('/auth/tenants'): payload = {'current_tenant_id': 'test', 'tenants': [{'tenant_id': 'test', 'name': '테스트 회사'}]}
                    elif path.endswith('/businesses'): payload = {'businesses': [] if empty_registry else businesses}
                    elif path.endswith('/settings'): payload = {'settings': {'businesses': businesses, 'branches': businesses[0]['branches'] if legacy else []}}
                    else: payload = {'records': [], 'metrics': [], 'requests': [], 'invites': [], 'documents': [], 'contracts': [], 'employees': [], 'statements': [], 'source': {'live': True, 'name': 'mock'}}
                    if not legacy and path not in ['/api/v1/auth/me', '/api/v1/auth/tenants', '/api/v1/workspaces/businesses', '/api/v1/yeoljeong-finance/session', '/api/v1/yeoljeong-finance/tenant-registry/businesses']:
                        forbidden.append(path)
                        await route.fulfill(status=403, json={'detail': '멀티테넌트 격리 작업이 진행 중입니다.'})
                    else:
                        await route.fulfill(json=payload)

                await context.route('**/api/**', mock)
                page = await context.new_page()
                page.on('pageerror', lambda err: errors.append(str(err)))
                page.on('console', lambda msg: console_errors.append(msg.text) if msg.type == 'error' else None)
                screens = []
                for device, width, height in [('pc', 1920, 911), ('mobile', 390, 844)]:
                    await page.set_viewport_size({'width': width, 'height': height})
                    for view in ['employees', 'attendance', 'payroll', 'hr-docs']:
                        await page.goto(f'{origin}/static/apps/obys/admin.html#{view}')
                        await page.wait_for_timeout(400)
                        group = page.locator('[data-group="직원·급여"]')
                        assert await group.locator('button[data-route]').count() == 4
                        assert await group.locator('a[href*="legacy=1"]').count() == int(legacy)
                        if width < 500:
                            if not await page.locator('#side.show').count(): await page.locator('#menuToggle').click()
                        file = f'{scenario}-{device}-admin-{view}.png'
                        await page.screenshot(path=str(OUT / file), full_page=True)
                        screens.append(file)
                        if empty_registry:
                            assert await page.get_by_role('link', name='사업자 등록하기').count() == 1
                            text = await page.locator('body').inner_text()
                            assert not any(x in text for x in ['열정국밥', '중화점', 'biz-junghwa', '멀티테넌트 격리', 'DB 연결 확인 필요'])
                    # 관리화면에서도 403 및 콘솔 오류를 허용하지 않는다.
                    assert not forbidden, forbidden
                    assert not console_errors, console_errors
                    for view in ['employees', 'attendance', 'payroll', 'onboarding', 'contracts', 'approvals']:
                        await page.goto(f'{origin}/static/apps/obys/index.html?legacy=1&view=legacy&people_view={view}')
                        await page.wait_for_timeout(450)
                        if legacy:
                            await page.locator('#'+view+'View').wait_for(state='visible', timeout=10000)
                            await page.locator('#legacyWorkspaceName', has_text='기존 테스트 매장').wait_for(state='attached')
                        else:
                            assert await page.get_by_role('link', name='새 관리화면으로 이동').count() == 1
                            text = await page.locator('body').inner_text()
                            assert not any(x in text for x in ['열정국밥', '중화점', 'biz-junghwa', '멀티테넌트 격리'])
                        assert await page.locator('[data-development-tool]:visible').count() == 0
                        file = f'{scenario}-{device}-legacy-{view}.png'
                        await page.screenshot(path=str(OUT / file), full_page=True)
                        screens.append(file)
                assert not forbidden, forbidden
                assert not errors, errors
                assert not console_errors, console_errors
                results.append({'scenario': scenario, 'checks': 'passed', 'requests': requests, '403_requests': forbidden, 'page_errors': errors, 'console_errors': console_errors, 'screens': screens})
                await context.close()
            await browser.close()
    finally:
        server.shutdown()
        server.server_close()
    (OUT / 'result.json').write_text(json.dumps(results, ensure_ascii=False, indent=2))
    print(json.dumps([{'scenario': r['scenario'], 'screens': len(r['screens']), 'checks': r['checks'], '403': len(r['403_requests'])} for r in results]))


if __name__ == '__main__':
    asyncio.run(main())
