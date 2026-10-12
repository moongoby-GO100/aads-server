import os
import re
from playwright.sync_api import sync_playwright
TOK=open(os.environ['M5_TOKEN_FILE']).read().strip()
OUT='/tmp/m5shots'; BASE='https://aads.newtalk.kr'
def san(s): return (s or '').replace(TOK,'<TOKEN>')
log=[]
def newpage(b,vw):
    ctx=b.new_context(viewport=vw)
    ctx.add_cookies([{'name':'aads_token','value':TOK,'url':BASE}])
    pg=ctx.new_page()
    pg.on('response',lambda r: log.append((r.status,re.sub(r'\?.*','',r.url)[-95:])) if ('/projects/' in r.url or 'project-docs' in r.url) else None)
    pg.add_init_script("localStorage.setItem('aads_token','%s')"%TOK)
    return ctx,pg
def detail_text(pg):
    h=pg.get_by_text('현재 문서와 개정 이력',exact=True).first
    box=h.locator('xpath=ancestor::*[self::section or self::div][1]')
    return san(box.inner_text())
def settle(pg,ms=7000):
    try: pg.get_by_text('상세를 불러오는 중').first.wait_for(state='detached',timeout=20000)
    except Exception: pass
    pg.wait_for_timeout(1500); pg.evaluate('window.scrollTo(0,0); document.querySelectorAll("*").forEach(e=>{if(e.scrollTop>0)e.scrollTop=0})')
    pg.wait_for_timeout(500)
def open_doc(pg,title):
    pg.get_by_text(title,exact=True).first.click(); settle(pg)
with sync_playwright() as p:
    b=p.chromium.launch(headless=True)
    ctx,pg=newpage(b,{'width':1440,'height':1000})
    pg.goto(BASE+'/docs',wait_until='domcontentloaded',timeout=40000); pg.wait_for_timeout(3000)
    pg.get_by_text('문서 정본',exact=True).first.click(); pg.get_by_text('문서 정본 목록').wait_for(timeout=20000); pg.wait_for_timeout(4000)
    for tag,title in [('04_pilot109','OHVIS 문서 정본관리 게이트 — 운영 기획서'),('05_pilot110','OHVIS 문서 정본관리 게이트 — 운영 PRD'),('06_pilot74','W-14F 평가기·실행기 인터페이스 계약 v1'),('07_pilot92','AADS Blue/Green 병목 최적화 PRD v1.3.0')]:
        open_doc(pg,title)
        pg.screenshot(path=f'{OUT}/prod_{tag}_detail.png')
        t=detail_text(pg).replace('\n',' | ')
        print('==',tag,t[:900])
    # ACCT switch
    pg.get_by_role('button',name='ACCT',exact=True).click()
    pg.wait_for_timeout(2000)
    try: pg.get_by_text('목록을 불러오는 중').first.wait_for(state='detached',timeout=20000)
    except Exception: pass
    pg.wait_for_timeout(1500)
    pg.screenshot(path=OUT+'/prod_08_project_switch_acct.png')
    print('== ACCT list:',san(pg.inner_text('body')).replace('\n',' | ')[-800:])
    try:
        open_doc(pg,'obys-v4/approve — spec')
        pg.screenshot(path=OUT+'/prod_09_pilot119_detail.png')
        print('== p119',detail_text(pg).replace('\n',' | ')[:700])
    except Exception as e: print('p119 err',str(e)[:200])
    b.close()
for l in log: print(l)
