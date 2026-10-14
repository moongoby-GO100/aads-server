import sys
from playwright.sync_api import sync_playwright
URL="https://fb.newtalk.kr/login"
with sync_playwright() as p:
    b=p.chromium.launch()
    for name,vp,mobile in (("desktop",{"width":1366,"height":850},False),("mobile",{"width":390,"height":844},True)):
        ctx=b.new_context(viewport=vp,is_mobile=mobile,has_touch=mobile,user_agent="aads-probe-m5-screen-"+name)
        pg=ctx.new_page()
        errs=[]
        pg.on("console",lambda m,e=errs: e.append(m.text) if m.type=="error" else None)
        pg.on("pageerror",lambda e,l=errs: l.append("pageerror:"+str(e)))
        r=pg.goto(URL,wait_until="networkidle",timeout=45000)
        pg.wait_for_timeout(1500)
        pg.screenshot(path=f"login_{name}.png",full_page=False)
        print(name,"status",r.status,"final",pg.url,"title",repr(pg.title()),
              "pw_inputs",pg.locator("input[type=password]").count(),"inputs",pg.locator("input").count(),"buttons",pg.locator("button").count(),"console_errors",len(errs),errs[:3])
        ctx.close()
    b.close()
