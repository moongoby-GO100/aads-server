from pathlib import Path

path = Path("/etc/nginx/sites-enabled/v2.newtalk.kr")
text = path.read_text()
marker = "    location /_next/static/ {"
block = """    # UI 기획서 HTML - Next.js 미들웨어 우회
    location ~ ^/NT-PRODUCT-REGISTRATION-UI-PLAN-.*\\.html$ {
        root /srv/newtalk-v2/frontend/public;
        add_header Cache-Control "no-cache";
    }

"""

if block.strip() not in text:
    if marker not in text:
        raise SystemExit("marker not found")
    backup = path.with_suffix(path.suffix + ".bak_ui_plan_static")
    backup.write_text(text)
    text = text.replace(marker, block + marker, 1)
    path.write_text(text)
    print("patched")
else:
    print("already patched")
