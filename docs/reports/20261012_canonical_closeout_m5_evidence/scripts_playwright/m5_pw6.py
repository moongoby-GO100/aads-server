import os,re,json
from playwright.sync_api import sync_playwright
TOK=open(os.environ['M5_TOKEN_FILE']).read().strip()
OUT='/tmp/m5shots'; BASE='https://aads.newtalk.kr'
Q=[('119','obys-v4/approve — spec'),('119b','obys-v4/approve spec 급여 직원 메뉴 승인')]
res={}
with sync_playwright() as p:
    b=p.chromium.launch(headless=True)
    ctx=b.new_context(viewport={'width':1440,'height':1000})
    ctx.add_cookies([{'name':'aads_token','value':TOK,'url':BASE}])
    pg=ctx.new_page()
    pg.add_init_script("localStorage.setItem('aads_token','%s')"%TOK)
    pg.goto(BASE+'/docs',wait_until='domcontentloaded',timeout=40000); pg.wait_for_timeout(2500)
    pg.get_by_text('내용(뜻으로)',exact=True).first.click()
    box=pg.get_by_placeholder(re.compile('무엇이 궁금'))
    for gid,q in Q:
        with pg.expect_response(lambda r:'project-docs/search' in r.url,timeout=40000) as ri:
            box.fill(''); box.fill(q)
        st=ri.value.status
        pg.wait_for_timeout(2500)
        titles=pg.evaluate("""()=>[...document.querySelectorAll('button')].filter(b=>b.className.includes('w-full text-left')&&b.querySelector('span')).map(b=>b.querySelector('span').innerText)""")
        canon=[(i+1,t) for i,t in enumerate(titles) if t.startswith('[승인') or t.startswith('[초안')]
        res[gid]={'status':st,'n':len(titles),'canonical_hits':canon}
        pg.screenshot(path=f'{OUT}/prod_15_search_pilot{gid}.png')
    b.close()
print(json.dumps(res,ensure_ascii=False,indent=1).replace(TOK,'<TOKEN>'))
