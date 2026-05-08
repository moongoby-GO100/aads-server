from pathlib import Path

path = Path("/etc/apache2/sites-enabled/00-v2.newtalk.kr.conf")
text = path.read_text()
old = """    Alias /NT-PRODUCT-REGISTRATION-UI-PLAN-v1.9-20260505.html /srv/newtalk-v2/frontend/public/NT-PRODUCT-REGISTRATION-UI-PLAN-v1.9-20260505.html
    <Location /NT-PRODUCT-REGISTRATION-UI-PLAN-v1.9-20260505.html>
        ProxyPass !
    </Location>

    ProxyPass        / http://127.0.0.1:3000/
"""
new = """    Alias /NT-PRODUCT-REGISTRATION-UI-PLAN-v1.9-20260505.html /srv/newtalk-v2/frontend/public/NT-PRODUCT-REGISTRATION-UI-PLAN-v1.9-20260505.html
    <Location /NT-PRODUCT-REGISTRATION-UI-PLAN-v1.9-20260505.html>
        ProxyPass !
    </Location>
    Alias /NT-PRODUCT-REGISTRATION-UI-PLAN-v2.0-20260506.html /srv/newtalk-v2/frontend/public/NT-PRODUCT-REGISTRATION-UI-PLAN-v2.0-20260506.html
    <Location /NT-PRODUCT-REGISTRATION-UI-PLAN-v2.0-20260506.html>
        ProxyPass !
    </Location>

    ProxyPass        / http://127.0.0.1:3000/
"""

if "NT-PRODUCT-REGISTRATION-UI-PLAN-v2.0-20260506.html" in text:
    print("already patched")
else:
    count = text.count(old)
    if count != 2:
        raise SystemExit(f"expected 2 insertion points, found {count}")
    backup = path.with_suffix(path.suffix + ".bak_ui_plan_v20")
    backup.write_text(text)
    path.write_text(text.replace(old, new))
    print("patched", count)
