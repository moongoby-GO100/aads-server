import os
import re
from playwright.sync_api import sync_playwright
TOK=open(os.environ['M5_TOKEN_FILE']).read().strip()
OUT='/tmp/m5shots'; BASE='https://aads.newtalk.kr'
def san(s): return (s or '').replace(TOK,'<TOKEN>')
log=[]
def newpage(b,vw,tok=TOK):
    ctx=b.new_context(viewport=vw)
    if tok:
        ctx.add_cookies([{'name':'aads_token','value':tok,'url':BASE}])
    pg=ctx.new_page()
    pg.on('response',lambda r: log.append((r.status,re.sub(r'\?.*','',r.url)[-95:])) if ('/projects/' in r.url or 'project-docs' in r.url) else None)
    if tok: pg.add_init_script("localStorage.setItem('aads_token','%s')"%tok)
    return ctx,pg
def maintext(pg): 
    return san(pg.inner_text('body')).replace('\n',' | ')
with sync_playwright() as p:
    b=p.chromium.launch(headless=True)
    ctx,pg=newpage(b,{'width':1440,'height':900})
    pg.goto(BASE+'/docs',wait_until='domcontentloaded',timeout=40000); pg.wait_for_timeout(3000)
    pg.get_by_text('문서 정본',exact=True).first.click(); pg.wait_for_timeout(5000)
    # pilot 109 (approved v1.0.0, latest draft v1.2.0)
    pg.get_by_text('OHVIS 문서 정본관리 게이트 — 운영 기획서',exact=True).first.click(); pg.wait_for_timeout(5000)
    pg.screenshot(path=OUT+'/prod_04_pilot109_detail.png')
    t=maintext(pg); i=t.find('상태'); print('P109 detail:',t[-1800:])
    # pilot 74 (approved only)
    pg.get_by_text('W-14F 평가기·실행기 인터페이스 계약 v1',exact=True).first.click(); pg.wait_for_timeout(4000)
    pg.screenshot(path=OUT+'/prod_05_pilot74_detail.png')
    print('P74 detail:',maintext(pg)[-900:])
    # switch ACCT
    pg.get_by_role('button',name='ACCT',exact=True).click(); pg.wait_for_timeout(4000)
    pg.screenshot(path=OUT+'/prod_06_project_switch_acct.png')
    print('ACCT:',maintext(pg)[-700:])
    ctx.close()
    b.close()
for l in log: print(l)
