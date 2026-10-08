"""언니냉면 화면의 카페24 이전(ACCT-CAFE24-MOVE-YEOLJEONG-UNNI-20261008) 회귀.

fb.newtalk.kr 이 카페24로 넘어간 뒤 /unni-naengmyeon/ 은 ACCT 앱의 JWT 미들웨어에 걸려
401 JSON 을 냈다(2026-10-08 실측). 공개 화면은 apache 가 정적 스냅샷으로, 문의 POST 와
직원용 조리법만 ACCT 앱(app.yeoljeong_main)이 받는다.
"""
from __future__ import annotations

import importlib.util
import re
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.auth as auth_module
from app import yeoljeong_main
from app.api import unni_naengmyeon as unni_api

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "config" / "apache" / "fb-cafe24.conf"
SCRIPT = ROOT / "scripts" / "deploy_unni_naengmyeon_cafe24.sh"
LOGIN = "/static/apps/obys/index.html?redirect=/unni-naengmyeon/recipes"

_spec = importlib.util.spec_from_file_location("unni_site", ROOT / "scripts" / "unni_naengmyeon_site.py")
site = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(site)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(auth_module, "SECRET_KEY", "test-secret-key-for-unni-cafe24-0123456789abcdef")
    return TestClient(yeoljeong_main.app, follow_redirects=False)


def _token():
    return auth_module.create_token("7", "staff@example.test")


# ── 조리법: 로그인 확인 후 HTML, 미인증은 401 JSON 이 아니라 오비서 로그인으로 ──────────────

def test_recipes_anonymous_redirects_to_obys_login(client):
    r = client.get("/unni-naengmyeon/recipes")
    assert r.status_code == 302
    assert r.headers["location"] == LOGIN
    assert "no-store" in r.headers["cache-control"]


def test_recipes_invalid_token_redirects(client):
    client.cookies.set("aads_token", "not-a-jwt")
    assert client.get("/unni-naengmyeon/recipes").status_code == 302


def test_recipes_with_login_cookie_serves_html(client):
    client.cookies.set("aads_token", _token())
    r = client.get("/unni-naengmyeon/recipes")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "private, no-store" == r.headers["cache-control"]
    assert "조리법 가이드" in r.text


def test_recipes_with_bearer_and_trailing_slash_and_head(client):
    headers = {"Authorization": f"Bearer {_token()}"}
    assert client.get("/unni-naengmyeon/recipes/", headers=headers).status_code == 200
    assert client.head("/unni-naengmyeon/recipes", headers=headers).status_code == 200


def test_unni_public_paths_are_not_401_on_the_app(client):
    # 공개 화면은 apache 가 정적으로 낸다. 앱까지 오면 401 JSON 이 아니라 404 여야 한다.
    assert client.get("/unni-naengmyeon/").status_code == 404


def test_other_api_still_requires_auth(client):
    assert client.get("/api/v1/obys/workspaces").status_code == 401


# ── 공개 문의 폼 ─────────────────────────────────────────────────────────────

class _FakeConnection:
    def __init__(self):
        self.calls = []

    async def execute(self, query, *args):
        self.calls.append((query, args))
        return "INSERT 0 1"


class _FakePool:
    def __init__(self):
        self.connection = _FakeConnection()

    @asynccontextmanager
    async def acquire(self):
        yield self.connection


def test_inquiry_is_public_on_the_acct_app(client, monkeypatch):
    pool = _FakePool()
    monkeypatch.setattr(unni_api, "get_pool", lambda: pool)
    unni_api._request_times.clear()
    r = client.post(
        "/api/v1/unni-naengmyeon/inquiries",
        json={
            "name": "홍길동",
            "contact": "010-1234-5678",
            "subject": "단체 주문 문의",
            "message": "내일 점심 단체 주문이 가능한지 문의드립니다.",
            "privacy_consent": True,
            "website": "",
        },
    )
    assert r.status_code == 201, r.text
    assert len(pool.connection.calls) == 1
    assert client.get("/api/v1/unni-naengmyeon/inquiries").status_code == 405


# ── 이식한 조리법 HTML ────────────────────────────────────────────────────────

def test_recipes_html_carries_every_menu_and_only_snapshot_assets():
    text = yeoljeong_main._UNNI_RECIPES_HTML.read_text(encoding="utf-8")
    for title in ("물냉면", "비빔냉면", "언니냉면", "불냉면", "명태회냉면", "묵사발",
                  "등심 돈까스", "전 종류", "함박스테이크", "군만두", "찐만두", "새우"):
        assert f"<h3>{title}</h3>" in text
    assert "<script" not in text
    srcs = re.findall(r'src="([^"]+)"', text)
    assert srcs and all(s.startswith("/brands/unni-naengmyeon/") for s in srcs)
    assert "/_next/" not in text


# ── 스냅샷·vhost 보조 도구 ───────────────────────────────────────────────────

def test_rewrite_html_replaces_next_image_in_attributes_srcset_and_rsc_payload():
    html = (
        '<img srcSet="/_next/image?url=%2Fbrands%2Funni-naengmyeon%2Fmenu%2Fa.jpg&amp;w=640&amp;q=75 1x, '
        '/_next/image?url=%2Fbrands%2Funni-naengmyeon%2Fmenu%2Fa.jpg&amp;w=1080&amp;q=75 2x" '
        'src="/_next/image?url=%2Fbrands%2Funni-naengmyeon%2Fmenu%2Fa.jpg&amp;w=1080&amp;q=75">'
        '<script>self.__next_f.push([1,"\\"src\\":\\"/_next/image?url=%2Fbrands%2Fx.png\\u0026w=96\\u0026q=75\\""])</script>'
    )
    out = site.rewrite_html(html)
    assert "/_next/image" not in out
    assert 'src="/brands/unni-naengmyeon/menu/a.jpg"' in out
    assert "/brands/unni-naengmyeon/menu/a.jpg 1x, /brands/unni-naengmyeon/menu/a.jpg 2x" in out
    assert '\\"src\\":\\"/brands/x.png\\"' in out


def test_rewrite_html_fails_when_an_unparsed_next_image_remains():
    with pytest.raises(ValueError):
        site.rewrite_html('<img src="/_next/image">')


def test_asset_refs_lists_snapshot_assets_only():
    html = ('<link href="/_next/static/css/a.css"><script src="/_next/static/chunks/b.js?dpl=1"></script>'
            '<img src="/brands/unni-naengmyeon/menu/c%20d.jpg"><a href="/static/apps/obys/">x</a>')
    assert site.asset_refs(html) == [
        "/_next/static/chunks/b.js",
        "/_next/static/css/a.css",
        "/brands/unni-naengmyeon/menu/c d.jpg",
    ]


def _live(upstream="http://172.28.50.2:8118"):
    """BEGIN-UNNI 이전의 설치본 = template 에서 UNNI 블록을 뺀 렌더."""
    return site.strip_unni(TEMPLATE.read_text(encoding="utf-8").replace("__FB_UPSTREAM__", upstream))


def test_render_vhost_adds_unni_blocks_with_the_live_upstream():
    out = site.render_vhost(TEMPLATE.read_text(encoding="utf-8"), _live("http://10.9.8.7:8123"))
    assert out.count("# BEGIN-UNNI") == 2
    assert '"http://10.9.8.7:8123/unni-naengmyeon/recipes"' in out
    assert "__FB_UPSTREAM__" not in out
    # 설치 후에도 upstream 은 하나 — deploy_acct_app_cafe24.sh 의 포트 교체가 그대로 동작해야 한다
    ups = {m for line in out.splitlines() if not line.lstrip().startswith("#")
           for m in re.findall(r"http://[0-9.]+:[0-9]+", line)}
    assert ups == {"http://10.9.8.7:8123"}
    assert site._comparable(site.strip_unni(out)) == site._comparable(_live("http://10.9.8.7:8123"))


def test_render_vhost_is_idempotent_on_an_installed_vhost():
    tpl = TEMPLATE.read_text(encoding="utf-8")
    once = site.render_vhost(tpl, _live())
    assert site.render_vhost(tpl, once) == once


def test_render_vhost_refuses_a_hand_edited_live_vhost():
    live = _live().replace("ProxyRequests Off", "ProxyRequests Off\n    ProxyTimeout 5", 1)
    with pytest.raises(LookupError):
        site.render_vhost(TEMPLATE.read_text(encoding="utf-8"), live)


def test_render_vhost_refuses_two_upstreams():
    live = _live() + "\nProxyPass /x http://10.0.0.1:9999/x\n"
    with pytest.raises(ValueError):
        site.render_vhost(TEMPLATE.read_text(encoding="utf-8"), live)


def test_template_unni_rules_precede_the_catch_all_proxy():
    tpl = TEMPLATE.read_text(encoding="utf-8")
    for block in re.findall(r"# BEGIN-ROUTES\n(.*?)# END-ROUTES", tpl, re.DOTALL):
        lines = [line.strip() for line in block.splitlines()]
        catch_all = lines.index('ProxyPass "/" "__FB_UPSTREAM__/" connectiontimeout=3 timeout=300 retry=0')
        for needle in ('ProxyPass "/unni-naengmyeon/recipes"', 'ProxyPassMatch "^/unni-naengmyeon(/.*)?$" !',
                       'ProxyPassMatch "^/_next/static/" !', 'ProxyPassMatch "^/brands/unni-naengmyeon/" !'):
            idx = next(i for i, line in enumerate(lines) if line.startswith(needle))
            assert idx < catch_all, needle
        recipes = next(i for i, line in enumerate(lines) if line.startswith('ProxyPass "/unni-naengmyeon/recipes"'))
        exclude = lines.index('ProxyPassMatch "^/unni-naengmyeon(/.*)?$" !')
        assert recipes < exclude  # 첫 일치가 이긴다 — 조리법이 제외 규칙보다 먼저


# ── 설치 스크립트 ────────────────────────────────────────────────────────────

def test_script_syntax():
    assert subprocess.run(["bash", "-n", str(SCRIPT)], check=False).returncode == 0


def test_apply_refuses_without_approval():
    proc = subprocess.run(["bash", str(SCRIPT), "apply"], capture_output=True, text=True, check=False,
                          env={"PATH": "/usr/bin:/bin", "UNNI_LOCK_FILE": "/tmp/unni-test.lock"})
    assert proc.returncode == 4
    assert "UNNI_CAFE24_APPROVED=1" in proc.stderr
