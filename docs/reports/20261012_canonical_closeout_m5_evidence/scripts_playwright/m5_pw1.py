import os, re, sys, json
from playwright.sync_api import sync_playwright
TOK=open(os.environ['M5_TOKEN_FILE']).read().strip()
OUT='/tmp/m5shots'
BASE='https://aads.newtalk.kr'
def san(s): return (s or '').replace(TOK,'<TOKEN>')
log=[]
with sync_playwright() as p:
    b=p.chromium.launch(headless=True)
    # 1) unauthenticated
    ctx=b.new_context(viewport={'width':1440,'height':900})
    pg=ctx.new_page()
    pg.on('response',lambda r: log.append(('anon',r.status,re.sub(r'\?.*','',r.url)[:100])) if '/api/' in r.url else None)
    try:
        resp=pg.goto(BASE+'/docs',wait_until='networkidle',timeout=30000)
        print('anon status',resp.status,'final url',pg.url)
        pg.wait_for_timeout(1500)
        pg.screenshot(path=OUT+'/prod_01_anon_desktop.png')
        print('anon text:',san(pg.inner_text('body'))[:300].replace('\n',' | '))
    except Exception as e: print('anon err',san(str(e))[:200])
    ctx.close()
    # 2) authed desktop
    ctx=b.new_context(viewport={'width':1440,'height':900})
    ctx.add_cookies([{'name':'aads_token','value':TOK,'url':BASE}])
    pg=ctx.new_page()
    pg.on('response',lambda r: log.append(('auth',r.status,re.sub(r'\?.*','',r.url)[:100])) if '/api/' in r.url else None)
    pg.add_init_script("localStorage.setItem('aads_token','%s')"%TOK)
    try:
        resp=pg.goto(BASE+'/docs',wait_until='domcontentloaded',timeout=40000)
        pg.wait_for_timeout(8000)
        print('auth status',resp.status,'url',pg.url)
        pg.screenshot(path=OUT+'/prod_02_docs_desktop.png')
        print('text:',san(pg.inner_text('body'))[:1500].replace('\n',' | '))
    except Exception as e: print('auth err',san(str(e))[:300])
    b.close()
for l in log: print(l)
