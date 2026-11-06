"""fb.newtalk.kr 화면 HTML 은 캐시하지 않는다.

2026-11-06: 배포(r86, 2fa8de1f) 뒤에도 관리자 화면(admin.html)이 1시간 캐시돼
옛 ?v= 스크립트를 불렀고, 이미 고친 '3.3% 용역계약 시급 차단'이 대표 화면에 그대로 떴다.
index.html 만 no-store 였고 admin.html 은 /static/ 의 max-age=3600 을 물려받았다.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONF = (ROOT / "config" / "apache" / "fb-cafe24.conf").read_text(encoding="utf-8")

BLOCK = re.compile(r'<LocationMatch "([^"]+)">\s*Header always set Cache-Control "([^"]+)"')


def _no_store_patterns():
    return [re.compile(p) for p, value in BLOCK.findall(CONF) if "no-store" in value]


def test_both_vhosts_mark_app_html_no_store():
    pats = _no_store_patterns()
    for path in ("/static/apps/obys/admin.html", "/static/apps/obys/index.html"):
        hits = [p for p in pats if p.search(path)]
        assert len(hits) == 2, f"{path}: no-store 규칙이 :80·:443 두 vhost 에 있어야 한다 ({len(hits)})"


def test_versioned_assets_keep_cache():
    pats = _no_store_patterns()
    for path in ("/static/apps/obys/modules/contract-editor-v41.js", "/static/apps/obys/modules/contract-v41.css"):
        assert not any(p.search(path) for p in pats), f"{path} 는 ?v= 로 갱신되므로 캐시를 유지한다"
