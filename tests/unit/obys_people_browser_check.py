"""로컬 파일과 API 목만으로 사람 메뉴를 점검한다. python3 tests/unit/obys_people_browser_check.py"""
import asyncio
import json
import mimetypes
from pathlib import Path
from urllib.parse import urlparse
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'docs/reports/evidence/acct-people-menu-button-audit-20261030'
SESSION = {'user': {'email': 'owner@example.test', 'name': '점검 관리자', 'is_admin': True}, 'permissions_verified': True,
           'permissions': {'role': 'owner', 'can_manage_onboarding': True, 'can_manage_settings': True, 'can_view': True, 'can_edit_local_data': True}}

async def main():
    OUT.mkdir(parents=True, exist_ok=True)
    result = {'mode': 'all requests intercepted; local assets + API mocks only', 'checks': [], 'screens': [], 'page_errors': [], 'requests': []}
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=['--no-sandbox'])
        context = await browser.new_context(viewport={'width': 1920, 'height': 911}, service_workers='block')
        page = await context.new_page()
        page.on('pageerror', lambda error: result['page_errors'].append(str(error)))
        failure = {'status': 0}
        # 화면이 실제 GET /payroll 로 목록을 다시 불러오므로, 행은 목 응답으로 넣는다.
        mock_rows = {'payroll': []}
        async def route(request_route):
            req = request_route.request
            path = urlparse(req.url).path
            if path.startswith('/api/'):
                result['requests'].append({'method': req.method, 'path': path})
                # 실패 주입은 급여 API 에만 한다. 0 이면 정상 목 응답.
                status = failure['status'] if '/payroll' in path else 0
                payload = {'detail': '점검용 실패'} if status else {}
                if not status:
                    if path.endswith('/session'): payload = SESSION
                    elif path.endswith('/payroll'): payload = {'statements': mock_rows['payroll']}
                    elif path.endswith('/businesses'): payload = {'businesses': []}
                    elif path.endswith('/versions'): payload = {'versions': [{'version': 1, 'status': 'draft', 'net_pay': 100}]}
                    elif path.endswith('/confirm'): payload = {'statement': {'id': 'p1', 'status': 'confirmed'}}
                    elif path.endswith('/deliver'): payload = {'status': 'failed', 'delivery': {'error_detail': '점검용 전달 실패'}}
                    elif path.endswith('/draft-from-attendance'): payload = {'statement': {'id': 'p1', 'status': 'draft'}}
                    else: payload = {'records': [], 'requests': [], 'documents': [], 'contracts': [], 'employees': [], 'invites': []}
                await request_route.fulfill(status=status or 200, json=payload)
            elif path.startswith('/static/'):
                file = ROOT / 'app' / path.lstrip('/')
                if file.is_file():
                    await request_route.fulfill(body=file.read_bytes(), content_type=mimetypes.guess_type(str(file))[0] or 'application/octet-stream')
                else: await request_route.fulfill(status=404, body='missing local asset')
            else:
                await request_route.fulfill(status=200, body='')
        await context.route('**/*', route)
        await context.add_init_script("localStorage.setItem('aads_token','local-mock-token');localStorage.setItem('yeoljeong-finance-auth-user'," + json.dumps(json.dumps(SESSION)) + ");")
        await page.goto('http://obys.local/static/apps/obys/index.html?legacy=1&view=legacy&people_view=employees')
        await page.wait_for_function('typeof setView === "function"')
        await page.wait_for_timeout(600)
        menus = await page.evaluate('''() => ['.side-nav','#subTabs'].map(selector => [...document.querySelectorAll(`${selector} [data-group="people"]`)].map(el=>el.textContent.trim()).sort())''')
        assert menus[0] == menus[1] and len(menus[0]) == 8, menus
        result['checks'].append('PC·모바일 메뉴 여덟 항목 일치')
        inventory = {}
        for device, width, height in [('pc', 1920, 911), ('mobile', 390, 844)]:
            await page.set_viewport_size({'width': width, 'height': height})
            for view in ['employees', 'attendance', 'payroll', 'onboarding', 'contracts', 'approvals']:
                await page.evaluate('(view)=>setView(view)', view)
                await page.wait_for_timeout(150)
                assert await page.locator('#'+view+'View').is_visible(), view
                inventory[view] = await page.locator('#'+view+'View').locator('button,a,[role=button],select').evaluate_all('els=>els.map(el=>({label:el.textContent.trim(),id:el.id,attrs:[...el.attributes].filter(a=>a.name.startsWith("data-")).map(a=>[a.name,a.value])}))')
                file = f'{device}-{view}.png'
                await page.screenshot(path=str(OUT / file), full_page=True)
                result['screens'].append(file)
            mock_rows['payroll'] = [{'id': 'p1', 'status': 'draft', 'period': '2026-10', 'employee_name': '점검 직원', 'gross_pay': 100, 'net_pay': 100},
                                    {'id': 'p2', 'status': 'confirmed', 'period': '2026-10', 'employee_name': '확정 직원', 'gross_pay': 200, 'net_pay': 200}]
            await page.evaluate("setView('payroll')")
            await page.evaluate('loadPayrollStatements()')
            await page.wait_for_timeout(150)
            assert await page.locator('[data-payroll-action="confirm"]').count() == 1
            assert await page.locator('[data-payroll-action="pay"]').count() == 1
            assert await page.locator('[data-delete-payroll="p2"]').count() == 0
            await page.screenshot(path=str(OUT / f'{device}-payroll-actions.png'), full_page=True)
            result['screens'].append(f'{device}-payroll-actions.png')
            await page.evaluate("startEmployeePreview({email:'employee@example.test', name:'점검 직원'})")
            await page.wait_for_timeout(100)
            await page.screenshot(path=str(OUT / f'{device}-employee-preview.png'), full_page=True)
            result['screens'].append(f'{device}-employee-preview.png')
            await page.evaluate('stopEmployeePreview()')
            await page.evaluate("setView('payroll')")
            clock_page = await context.new_page()
            await clock_page.goto('http://obys.local/static/apps/obys/clock.html')
            await clock_page.wait_for_timeout(100)
            await clock_page.screenshot(path=str(OUT / f'{device}-clock.png'), full_page=True)
            result['screens'].append(f'{device}-clock.png')
            await clock_page.close()
        mock_rows['payroll'] = [{'id': 'p1', 'status': 'draft'}]
        await page.evaluate('loadPayrollStatements()')
        await page.wait_for_timeout(150)
        # Delegated real click: double click cannot send two confirms.
        page.on('dialog', lambda dialog: dialog.accept())
        before = len([x for x in result['requests'] if x['path'].endswith('/confirm')])
        await page.locator('[data-payroll-action="confirm"]').evaluate('(el)=>{el.click();el.click()}')
        await page.wait_for_timeout(200)
        assert len([x for x in result['requests'] if x['path'].endswith('/confirm')]) - before == 1
        result['checks'].append('급여 확정 실제 행 버튼 중복 클릭: POST 1회, 목록 GET 갱신')
        mock_rows['payroll'] = [{'id': 'p2', 'status': 'confirmed', 'period': '2026-10'}]
        await page.evaluate('loadPayrollStatements()')
        await page.wait_for_timeout(150)
        await page.locator('[data-payroll-action="deliver"]').click()
        await page.wait_for_timeout(100)
        assert '점검용 전달 실패' in await page.locator('#toast').inner_text()
        result['checks'].append('명세 전달 HTTP 200 / status=failed를 성공으로 표시하지 않음')
        failure['status'] = 403
        await page.evaluate('loadPayrollStatements().catch(()=>{})')
        assert await page.evaluate('hasServerAuth()')
        result['checks'].append('403은 인증을 제거하지 않음')
        failure['status'] = 401
        await page.evaluate('loadPayrollStatements().catch(()=>{})')
        assert not await page.evaluate('hasServerAuth()')
        assert await page.locator('#authGate').is_visible()
        result['checks'].append('401 로그인 화면 즉시 노출')
        result['inventory'] = inventory
        await browser.close()
    (OUT / '로컬-브라우저-검증.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps({'checks': result['checks'], 'screens': len(result['screens']), 'page_errors': result['page_errors']}, ensure_ascii=False))
    assert not result['page_errors'], result['page_errors']

if __name__ == '__main__':
    asyncio.run(main())
