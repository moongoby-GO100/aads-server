import os
import re
from playwright.sync_api import sync_playwright
TOK=open(os.environ['M5_TOKEN_FILE']).read().strip()
OUT='/tmp/m5shots'; BASE='https://aads.newtalk.kr'
def san(s): return (s or '').replace(TOK,'<TOKEN>')
log=[]
def newpage(b,vw,tok):
    ctx=b.new_context(viewport=vw, **({'is_mobile':True,'has_touch':True} if vw['width']<500 else {}))
    ctx.add_cookies([{'name':'aads_token','value':tok,'url':BASE}])
    pg=ctx.new_page()
    pg.on('response',lambda r: log.append((vw['width'],r.status,re.sub(r'\?.*','',r.url)[-70:])) if ('/projects/' in r.url or 'project-docs' in r.url or '/auth/' in r.url) else None)
    pg.add_init_script("localStorage.setItem('aads_token','%s')"%tok)
    return ctx,pg
with sync_playwright() as p:
    b=p.chromium.launch(headless=True)
    # A) content search desktop
    ctx,pg=newpage(b,{'width':1440,'height':1000},TOK)
    pg.goto(BASE+'/docs',wait_until='domcontentloaded',timeout=40000); pg.wait_for_timeout(3000)
    pg.get_by_text('내용(뜻으로)',exact=True).first.click()
    pg.get_by_placeholder(re.compile('무엇이 궁금')).fill('OHVIS 문서 정본관리 게이트 운영 기획서')
    pg.wait_for_timeout(9000)
    pg.screenshot(path=OUT+'/prod_10_content_search_desktop.png')
    print('SEARCH:',san(pg.inner_text('body')).replace('\n',' | ')[-1600:])
    ctx.close()
    # B) 401 recovery: invalid token
    ctx,pg=newpage(b,{'width':1440,'height':900},'invalid.token.value')
    pg.goto(BASE+'/docs',wait_until='domcontentloaded',timeout=40000); pg.wait_for_timeout(7000)
    print('401 url:',pg.url); pg.screenshot(path=OUT+'/prod_11_401_invalid_token.png')
    print('401 text:',pg.inner_text('body').replace('\n',' | ')[:200])
    ctx.close()
    # C) mobile canonical
    ctx,pg=newpage(b,{'width':390,'height':844},TOK)
    pg.goto(BASE+'/docs',wait_until='domcontentloaded',timeout=40000); pg.wait_for_timeout(4000)
    pg.get_by_text('문서 정본',exact=True).first.click(); pg.get_by_text('문서 정본 목록').wait_for(timeout=20000); pg.wait_for_timeout(4000)
    pg.screenshot(path=OUT+'/prod_12_mobile_list.png')
    pg.get_by_text('OHVIS 문서 정본관리 게이트 — 운영 기획서',exact=True).first.click(); 
    pg.wait_for_timeout(6000)
    pg.get_by_text('현재 문서와 개정 이력',exact=True).first.scroll_into_view_if_needed(); pg.wait_for_timeout(800)
    pg.screenshot(path=OUT+'/prod_13_mobile_detail.png')
    print('mobile overflow:',pg.evaluate('document.documentElement.scrollWidth')," vs ",pg.evaluate('window.innerWidth'))
    ctx.close(); b.close()
for l in log: print(l)
