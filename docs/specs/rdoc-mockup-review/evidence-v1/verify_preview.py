from pathlib import Path
from playwright.sync_api import sync_playwright
import json, hashlib
from datetime import datetime
from zoneinfo import ZoneInfo

root = Path('/root/aads/aads-server/docs/specs/rdoc-mockup-review')
out = root / 'evidence-v1'
out.mkdir(exist_ok=True)
errors=[]
checks=[]
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True)
    page=browser.new_page(viewport={'width':1440,'height':1100},device_scale_factor=1)
    page.on('pageerror',lambda e: errors.append(str(e)))
    page.goto((root/'mockup-v1.html').as_uri())
    page.screenshot(path=str(out/'desktop.png'),full_page=True)
    assert page.locator('#approve').is_disabled()
    page.locator('#request').click()
    assert '입력해' in page.locator('#notice').inner_text()
    checks.append('empty_comment_rejected')
    page.locator('#feedback').fill('모바일에서 승인 대상 정보를 먼저 보여주세요.')
    page.locator('#request').click()
    assert page.locator('#approve').is_disabled()
    assert 'v1 · 미해결' in page.locator('#comments').inner_text()
    page.screenshot(path=str(out/'changes-requested.png'),full_page=True)
    page.locator('#resubmit').click()
    assert page.locator('#targetVersion').inner_text()=='v2'
    assert page.locator('#approve').is_disabled()
    assert '반영 보고' in page.locator('#comments').inner_text()
    checks.append('revision_resubmission_requires_new_confirmation')
    page.locator('#reviewed').check()
    page.locator('#approve').click()
    assert 'v2' in page.locator('#dialogTarget').inner_text()
    page.screenshot(path=str(out/'approval-confirmation.png'),full_page=True)
    page.locator('#confirm').click()
    assert '실제 승인·DB 저장·작업 실행은 하지 않았습니다' in page.locator('#notice').inner_text()
    checks.append('approval_confirmation_exact_version_and_demo_disclaimer')
    page.reload()
    page.locator('#kind').select_option('modify')
    assert page.locator('#approve').is_disabled()
    assert page.locator('#request').is_disabled()
    assert page.locator('#blockAlert').is_visible()
    page.screenshot(path=str(out/'before-missing.png'),full_page=True)
    checks.append('existing_screen_before_missing_blocks_submission')
    page.locator('#kind').select_option('new')
    page.locator('#feedback').fill('복구 후에도 남아야 할 의견')
    for state in ['loading','empty','error','permission','expired','offline']:
        page.locator('#viewState').select_option(state)
        assert page.locator('#approve').is_disabled()
        assert page.locator('#statebox').is_visible()
        page.screenshot(path=str(out/f'state-{state}.png'),full_page=True)
        page.locator('#recover').click()
        assert page.locator('#feedback').input_value()=='복구 후에도 남아야 할 의견'
        checks.append(f'{state}_recovery_preserves_input')
    page.reload()
    page.locator('#mobile').click()
    assert 'mobile' in page.locator('#preview').get_attribute('class')
    page.locator('#desktop').click()
    checks.append('preview_viewport_switch')
    for width in [390,360,768,1440]:
        page.set_viewport_size({'width':width,'height':844 if width<600 else 1100})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'),f'overflow at {width}'
        if width==390: page.screenshot(path=str(out/'mobile.png'),full_page=True)
        checks.append(f'no_horizontal_overflow_{width}')
    assert not errors, errors
    checks.append('no_browser_page_errors')
    browser.close()
result={'tested_at_kst':datetime.now(ZoneInfo('Asia/Seoul')).isoformat(),'source':'local Playwright Chromium; file URL; no login; no production approval','checks':checks,'console_page_errors':errors,'result':'pass'}
(out/'verification.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
print(json.dumps(result,ensure_ascii=False))
