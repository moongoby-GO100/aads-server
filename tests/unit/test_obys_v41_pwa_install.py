"""로그인 후 화면(mockup-v4-1.html, V4.1) 웹앱 설치: manifest 연결·'앱 설치' 버튼·서비스워커 등록."""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).parents[2]
STATIC = ROOT / "app/static/apps/obys"
HTML = (STATIC / "mockup-v4-1.html").read_text(encoding="utf-8")
JS = (STATIC / "modules/pwa-install.js").read_text(encoding="utf-8")
HEAD = HTML.split("</head>", 1)[0]

sys.path.insert(0, str(ROOT / "scripts"))
import obys_release_assets as tool  # noqa: E402


def test_head_links_app_manifest_once_with_ios_meta():
    links = re.findall(r'<link\b[^>]*rel="manifest"[^>]*>', HEAD)
    assert len(links) == 1
    assert 'href="/static/apps/obys/manifest-app.webmanifest"' in links[0]
    assert 'name="theme-color"' in HEAD
    assert 'name="apple-mobile-web-app-capable"' in HEAD
    assert 'name="apple-mobile-web-app-title"' in HEAD
    assert 'rel="apple-touch-icon"' in HEAD


def test_install_button_in_top_actions_before_logout():
    actions = HTML.split('class="top-actions"', 1)[1].split("</header>", 1)[0]
    assert 'id="pwaInstallBtn"' in actions and ">앱 설치<" in actions
    assert actions.index('id="pwaInstallBtn"') < actions.index('id="logoutBtn"')
    btn = re.search(r'<button[^>]*id="pwaInstallBtn"[^>]*>', actions).group(0)
    assert 'class="btn"' in btn
    assert 'id="pwaInstallHint"' in HTML


def test_install_button_visible_on_mobile_and_touch_size():
    assert "min-height:40px" in HTML.split("#pwaInstallBtn{", 1)[1].split("}", 1)[0]
    media = HTML.split("@media(max-width:820px){", 1)[1].split("</style>", 1)[0]
    assert "#pwaInstallBtn.btn:not(.primary){display:inline-block}" in media
    assert ".pwa-hint-close{" in HTML and "min-height:40px" in HTML.split(".pwa-hint-close{", 1)[1].split("}", 1)[0]


def test_page_loads_shared_module_by_stampable_absolute_path():
    assert '<script src="/static/apps/obys/modules/pwa-install.js"></script>' in HTML
    assert "/static/apps/obys/modules/pwa-install.js" in tool.module_refs(HTML)
    out, n = tool.stamp_html(HTML, "abc1234")
    assert "/static/apps/obys/modules/pwa-install.js?v=abc1234" in out and n >= 1


def test_module_has_install_flow_fallbacks_and_sw_registration():
    assert "beforeinstallprompt" in JS and "deferredPrompt.prompt()" in JS
    assert "(display-mode: standalone)" in JS
    assert re.search(
        r'serviceWorker\.register\("/static/apps/obys/sw\.js",\s*\{\s*scope:\s*"/static/apps/obys/"\s*\}\)', JS
    )
    assert 'addEventListener("appinstalled"' in JS
    assert "if (!deferredPrompt) {" in JS and "fallbackHint()" in JS
    assert JS.index("if (!standalone)") < JS.index("installBtn.hidden = false;")
    for token in ("KAKAOTALK", "NAVER", "Instagram", "FBAN", "FBAV", "Line", "DaumApps", "SamsungBrowser"):
        assert token in JS
    for text in ("다른 브라우저로 열기", "홈 화면에 추가", "현재 페이지 추가", "오비서 설치",
                 "scheme=https;package=com.android.chrome;end"):
        assert text in JS


def test_module_file_listed_by_purge_urls(tmp_path):
    (tmp_path / "mockup-v4-1.html").write_text(HTML, encoding="utf-8")
    urls = tool.purge_urls(tmp_path, "abc1234", "https://fb.newtalk.kr")
    assert "https://fb.newtalk.kr/static/apps/obys/modules/pwa-install.js?v=abc1234" in urls
