"""V4.1 관리자 화면 정식 주소 admin.html, 옛 mockup-v4-1.html 은 새 주소로 넘김."""
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OBYS = ROOT / "app" / "static" / "apps" / "obys"
sys.path.insert(0, str(ROOT / "scripts"))
import obys_release_assets as tool  # noqa: E402

ADMIN_PATH = "/static/apps/obys/admin.html"
INDEX = (OBYS / "index.html").read_text(encoding="utf-8")
ADMIN = (OBYS / "admin.html").read_text(encoding="utf-8")
REDIRECT = (OBYS / "mockup-v4-1.html").read_text(encoding="utf-8")
OLD = "mockup-" + "v4-1"
# 배포 제외 목록에 이 별칭을 넣지 말라는 경고 주석·검증이 의도적으로 옛 이름을 적는 파일.
RELEASE_KEEP_REFERENCES = {"scripts/deploy_acct_app_cafe24.sh", "tests/unit/test_acct_app_cafe24_release.py"}


def test_index_points_to_admin_html():
    assert f'const V41_APP_PATH = "{ADMIN_PATH}";' in INDEX
    assert OLD not in INDEX


def test_admin_html_is_the_v41_screen():
    assert 'rel="manifest"' in ADMIN and "manifest-app.webmanifest" in ADMIN
    assert "contract-editor-v41.js" in ADMIN


def test_old_address_is_a_redirect_only_page():
    assert 'location.replace("/static/apps/obys/admin.html" + location.search + location.hash)' in REDIRECT
    assert '<meta name="robots" content="noindex">' in REDIRECT
    noscript = re.findall(r"<noscript>(.*?)</noscript>", REDIRECT, re.S)
    assert any('http-equiv="refresh"' in n and "0;url=/static/apps/obys/admin.html" in n for n in noscript)
    assert any('href="/static/apps/obys/admin.html"' in n for n in noscript)
    for forbidden in ("contract-editor-v41", "manifest", "serviceWorker", "sw.js", "<link"):
        assert forbidden not in REDIRECT
    assert len(REDIRECT) < 1500


def test_redirect_script_keeps_query_and_hash():
    script = re.search(r"<script>(.*?)</script>", REDIRECT, re.S).group(1)
    expected = f"{ADMIN_PATH}?cv41_request=req-1#employees"
    node = shutil.which("node")
    if node:
        js = (
            "const calls=[];globalThis.location={search:'?cv41_request=req-1',hash:'#employees',"
            "replace:(u)=>calls.push(u)};" + script + ";console.log(JSON.stringify(calls));"
        )
        out = subprocess.run([node, "-e", js], capture_output=True, text=True, timeout=30, check=True).stdout
        assert json.loads(out) == [expected]
    else:
        print("node 없음: 정규식 검증으로 대체")
        m = re.fullmatch(r'\s*location\.replace\("([^"]+)" \+ location\.search \+ location\.hash\);\s*', script)
        assert m and m.group(1) + "?cv41_request=req-1" + "#employees" == expected


def test_no_other_references_to_the_old_name():
    self_path = Path(__file__).resolve()
    redirect_path = (OBYS / "mockup-v4-1.html").resolve()
    allowed_release_test = (ROOT / "tests/unit/test_obys_release_assets.py").resolve()
    hits = []
    for base in ("app", "scripts", "tests"):
        for path in (ROOT / base).rglob("*"):
            if not path.is_file() or "__pycache__" in path.parts or "node_modules" in path.parts:
                continue
            rel = path.relative_to(ROOT).as_posix()
            if rel.startswith("app/static/reports/") or path.suffix == ".md" or path.suffix == ".pyc":
                continue
            if path.resolve() in (self_path, redirect_path, allowed_release_test):
                continue
            if rel in RELEASE_KEEP_REFERENCES:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            if OLD in text:
                hits.append(rel)
    assert hits == []


def test_purge_urls_include_admin_and_old_redirect(tmp_path):
    d = tmp_path / "obys"
    shutil.copytree(OBYS, d, ignore=shutil.ignore_patterns("*.pre-*"))
    urls = tool.purge_urls(d, "abc1234", "https://fb.newtalk.kr/")
    base = "https://fb.newtalk.kr/static/apps/obys"
    assert f"{base}/admin.html" in urls and f"{base}/mockup-v4-1.html" in urls
