import os
import re
from playwright.sync_api import sync_playwright
TOK=open(os.environ['M5_TOKEN_FILE']).read().strip()
OUT='/tmp/m5shots'; BASE='https://aads.newtalk.kr'
def san(s): return (s or '').replace(TOK,'<TOKEN>')
log=[]
with sync_playwright() as p:
    b=p.chromium.launch(headless=True)
    ctx=b.new_context(viewport={'width':1440,'height':900})
    ctx.add_cookies([{'name':'aads_token','value':TOK,'url':BASE}])
    pg=ctx.new_page()
    pg.on('response',lambda r: log.append((r.status,re.sub(r'\?.*','',r.url)[-90:])) if ('/projects/' in r.url or 'project-docs' in r.url) else None)
    pg.add_init_script("localStorage.setItem('aads_token','%s')"%TOK)
    pg.goto(BASE+'/docs',wait_until='domcontentloaded',timeout=40000)
    pg.wait_for_timeout(4000)
    pg.get_by_text('문서 정본',exact=True).first.click()
    pg.wait_for_timeout(6000)
    pg.screenshot(path=OUT+'/prod_03_canonical_tab.png')
    print(san(pg.inner_text('main') if pg.locator('main').count() else pg.inner_text('body'))[-2500:].replace('\n',' | '))
    b.close()
for l in log: print(l)
