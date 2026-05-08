from pathlib import Path
import re

SITEMAP_URL = "https://pick.newtalk.kr/reports/2026-05-06/ntv2-retail-wholesale-sitemap.html"
V20_URL = "https://v2.newtalk.kr/NT-PRODUCT-REGISTRATION-UI-PLAN-v2.0-20260506.html"

targets = [
    Path("/srv/newtalk-v2/src/public"),
    Path("/srv/newtalk-v2/frontend/public"),
    Path("/srv/newtalk-v2/docs/planning"),
]

bar = f'''
<div class="ntv2-ui-plan-links" style="border-bottom:1px solid #d9e2ec;background:#ffffff;padding:10px 16px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:14px;line-height:1.5">
  <strong style="margin-right:10px">NewTalk V2 UI 기획</strong>
  <a href="{SITEMAP_URL}" style="display:inline-block;margin-right:8px;color:#2563eb;font-weight:700">소매/도매 사이트맵</a>
  <a href="{V20_URL}" style="display:inline-block;margin-right:8px;color:#2563eb;font-weight:700">최신 v2.0</a>
</div>
'''

changed = []
skipped = []

for base in targets:
    if not base.exists():
        continue
    for path in sorted(base.glob("NT-PRODUCT-REGISTRATION-UI-PLAN-*.html")):
        text = path.read_text(encoding="utf-8", errors="ignore")
        if SITEMAP_URL in text:
            skipped.append(str(path))
            continue
        match = re.search(r"<body[^>]*>", text, flags=re.IGNORECASE)
        if match:
            insert_at = match.end()
            updated = text[:insert_at] + bar + text[insert_at:]
        else:
            updated = bar + text
        path.write_text(updated, encoding="utf-8")
        changed.append(str(path))

print("changed", len(changed))
for item in changed:
    print(item)
print("skipped", len(skipped))
