"""오비서 본 화면(index.html) 웹앱 설치: 별도 manifest·설치 버튼·서비스워커 등록, 기존 출퇴근 앱 불변."""
import json
import re
from pathlib import Path

STATIC = Path(__file__).parents[2] / "app/static/apps/obys"
HTML = (STATIC / "index.html").read_text(encoding="utf-8")
SW = (STATIC / "sw.js").read_text(encoding="utf-8")


def test_index_links_app_manifest_once():
    links = re.findall(r'<link\b[^>]*rel="manifest"[^>]*>', HTML)
    assert len(links) == 1
    assert 'href="/static/apps/obys/manifest-app.webmanifest"' in links[0]
    head = HTML.split("</head>", 1)[0]
    assert 'name="theme-color"' in head
    assert 'rel="apple-touch-icon"' in head
    assert 'name="apple-mobile-web-app-capable"' in head


def test_app_manifest_is_valid_and_starts_at_index():
    data = json.loads((STATIC / "manifest-app.webmanifest").read_text(encoding="utf-8"))
    assert data["id"] == "/static/apps/obys/index.html"
    assert data["start_url"] == "/static/apps/obys/index.html"
    assert data["scope"] == "/static/apps/obys/"
    assert data["name"] == "오비서" and data["short_name"] == "오비서"
    assert data["display"] == "standalone" and data["lang"] == "ko"
    sizes = {icon["sizes"] for icon in data["icons"]}
    assert {"192x192", "512x512"} <= sizes
    for icon in data["icons"]:
        assert (STATIC / icon["src"].removeprefix("/static/apps/obys/")).is_file()


def test_clock_manifest_unchanged():
    data = json.loads((STATIC / "manifest.webmanifest").read_text(encoding="utf-8"))
    assert data["id"] == "/static/apps/obys/clock.html"
    assert data["start_url"] == "/static/apps/obys/clock.html"
    assert data["name"] == "오비서 출퇴근"


def test_index_has_install_prompt_and_service_worker_registration():
    assert "beforeinstallprompt" in HTML
    assert "deferredPrompt.prompt()" in HTML
    assert re.search(
        r'serviceWorker\.register\("/static/apps/obys/sw\.js",\s*\{\s*scope:\s*"/static/apps/obys/"\s*\}\)', HTML
    )
    assert "(display-mode: standalone)" in HTML
    assert "홈 화면에 추가" in HTML


def test_install_bar_in_topbar_not_inside_hidden_top_actions():
    header = HTML.split('<header class="topbar">', 1)[1].split("</header>", 1)[0]
    assert 'id="pwaInstallBar"' in header
    assert header.index('class="top-actions"') < header.index('id="pwaInstallBar"')
    assert re.search(r"body\.signed-out \.top-actions\s*\{\s*display:\s*none;", HTML)
    assert "body.signed-out .pwa-install-bar" not in HTML


def test_sw_app_shell_does_not_cache_index():
    shell = SW.split("const APP_SHELL = [", 1)[1].split("];", 1)[0]
    assert "index.html" not in shell
    assert "clock.html" in shell


def test_install_button_always_shown_unless_standalone_with_fallback_hints():
    script = HTML.rsplit("<script>", 1)[1]
    assert "bar.hidden = false;" in script and "installBtn.hidden = false;" in script
    assert script.index("if (!standalone)") < script.index("bar.hidden = false;")
    bar_show = script.index("bar.hidden = false;")
    assert bar_show < script.index('addEventListener("beforeinstallprompt"')
    assert "if (!deferredPrompt) {" in script and "fallbackHint()" in script
    assert 'addEventListener("appinstalled"' in script
    assert "min-height: 40px" in HTML.split(".pwa-install-bar button {", 1)[1].split("}", 1)[0]


def test_install_fallback_hints_per_browser():
    for token in ("KAKAOTALK", "NAVER", "Instagram", "FBAN", "FBAV", "Line", "DaumApps"):
        assert token in HTML
    for text in (
        "다른 브라우저로 열기",
        "홈 화면에 추가",
        "현재 페이지 추가",
        "오비서 설치",
        "scheme=https;package=com.android.chrome;end",
    ):
        assert text in HTML
    assert "SamsungBrowser" in HTML
