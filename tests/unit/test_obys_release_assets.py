"""scripts/obys_release_assets.py: 정적 파일 버전 부여와 Cloudflare 파일 단위 삭제 (네트워크·자격증명 없이)."""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
TOOL = ROOT / "scripts/obys_release_assets.py"
spec = importlib.util.spec_from_file_location("obys_release_assets", TOOL)
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)

SHA = "1a2b3c4d"
HTML = """<html>
<link rel="stylesheet" href="/static/apps/obys/modules/store-assistant-v2.css">
<link rel="stylesheet" href="/static/apps/obys/modules/ledger-details.css?v=20261014-r1">
<link rel="stylesheet" href='/static/apps/obys/modules/clobe-collection.css?v=20261003-r5'>
<script src="/static/apps/obys/modules/app-config.js"></script>
<script src="/static/apps/obys/modules/store-assistant-v2.js"></script>
<script src="https://cdn.example.com/static/apps/obys/modules/ext.js"></script>
<script src="//cdn.example.com/static/apps/obys/modules/ext2.js"></script>
<script>fetch("/api/v1/obys/modules/x.js?v=5"); const u = "/static/apps/obys/assets/logo.png?v=3";</script>
<code>/static/apps/obys/modules/app-config.js</code>
</html>
"""


def test_tool_source_is_ascii_because_it_is_shipped_over_ssh_as_python_c():
    TOOL.read_bytes().decode("ascii")


def test_stamp_html_puts_the_same_sha_on_unversioned_and_manually_versioned_refs():
    out, n = tool.stamp_html(HTML, SHA)
    assert n == 5
    for ref in ("store-assistant-v2.css", "ledger-details.css", "clobe-collection.css", "app-config.js", "store-assistant-v2.js"):
        assert f"/static/apps/obys/modules/{ref}?v={SHA}" in out
    assert "20260921-r3" not in out and "20261003-r5" not in out
    assert out.count(f"?v={SHA}") == 5


def test_stamp_html_leaves_external_urls_api_calls_other_assets_and_prose_alone():
    out, _ = tool.stamp_html(HTML, SHA)
    assert 'https://cdn.example.com/static/apps/obys/modules/ext.js"' in out
    assert '"//cdn.example.com/static/apps/obys/modules/ext2.js"' in out
    assert 'fetch("/api/v1/obys/modules/x.js?v=5")' in out
    assert '"/static/apps/obys/assets/logo.png?v=3"' in out
    assert "<code>/static/apps/obys/modules/app-config.js</code>" in out


def test_stamp_is_deterministic_and_idempotent():
    once, _ = tool.stamp_html(HTML, SHA)
    twice, _ = tool.stamp_html(once, SHA)
    assert once == twice
    other, _ = tool.stamp_html(once, "ffffffff")
    assert SHA not in other and other.count("?v=ffffffff") == 5


def test_stamp_sw_replaces_only_the_cache_version_value():
    sw = 'const CACHE_VERSION = "obys-clock-shell-20261008-r2";\nconst CACHE_PREFIX = "obys-clock-shell-";\n'
    out, n = tool.stamp_sw(sw, SHA)
    assert n == 1
    assert out == f'const CACHE_VERSION = "obys-clock-shell-{SHA}";\nconst CACHE_PREFIX = "obys-clock-shell-";\n'


@pytest.mark.parametrize("bad", ["", "a b", "x/../y", '1"2', "-abc", "a" * 41])
def test_invalid_version_is_rejected(bad):
    with pytest.raises(ValueError):
        tool.stamp_html(HTML, bad)
    with pytest.raises(ValueError):
        tool.stamp_sw("x", bad)


def _obys_tree(tmp_path):
    d = tmp_path / "obys"
    d.mkdir()
    (d / "index.html").write_text(HTML, encoding="utf-8")
    (d / "signup.html").write_text('<link href="/static/apps/obys/modules/store-assistant-v2.css">', encoding="utf-8")
    (d / "clock.html").write_text("<html>no modules</html>", encoding="utf-8")
    (d / "sw.js").write_text('const CACHE_VERSION = "obys-clock-shell-20261008-r2";\n', encoding="utf-8")
    return d


def test_stamp_tree_rewrites_html_and_sw_and_skips_files_without_refs(tmp_path):
    d = _obys_tree(tmp_path)
    clock_before = (d / "clock.html").read_text()
    report = tool.stamp_tree(d, SHA)
    assert report["html"] == {"index.html": 5, "signup.html": 1} and report["sw"] == 1
    assert (d / "clock.html").read_text() == clock_before
    assert f'"obys-clock-shell-{SHA}"' in (d / "sw.js").read_text()


def test_stamp_tree_refuses_a_sw_without_cache_version(tmp_path):
    d = _obys_tree(tmp_path)
    (d / "sw.js").write_text("self.x = 1;\n")
    with pytest.raises(ValueError):
        tool.stamp_tree(d, SHA)


def test_stamp_cli_prints_a_report(tmp_path):
    d = _obys_tree(tmp_path)
    out = subprocess.run([sys.executable, str(TOOL), "stamp", str(d), SHA], check=True, capture_output=True, text=True).stdout
    assert out.startswith("OBYS_ASSET_STAMP ") and json.loads(out.split(" ", 1)[1])["version"] == SHA


def test_the_real_shell_has_module_refs_and_a_stampable_service_worker(tmp_path):
    d = tmp_path / "obys"
    d.mkdir()
    for name in ("index.html", "signup.html", "clock.html", "sw.js"):
        (d / name).write_bytes((ROOT / "app/static/apps/obys" / name).read_bytes())
    report = tool.stamp_tree(d, SHA)
    # 12 = 기존 10 + 직원 포털(employee-portal.css·employee-portal.js, ACCT-OBYS-EMPLOYEE-PORTAL-MOCKUP-V1)
    assert report["sw"] == 1 and report["html"]["index.html"] == 12
    text = (d / "index.html").read_text(encoding="utf-8")
    for ref in ("app-config.js", "store-assistant-v2.js", "store-assistant-v2.css", "account-recovery.js",
                "employee-portal.js", "employee-portal.css"):
        assert f"/static/apps/obys/modules/{ref}?v={SHA}" in text


def test_purge_urls_cover_index_sw_and_every_referenced_module_once(tmp_path):
    d = _obys_tree(tmp_path)
    urls = tool.purge_urls(d, SHA, "https://fb.newtalk.kr/")
    base = "https://fb.newtalk.kr/static/apps/obys"
    assert urls[:2] == [f"{base}/index.html", f"{base}/sw.js"]
    assert f"{base}/modules/store-assistant-v2.css" in urls and f"{base}/modules/store-assistant-v2.css?v={SHA}" in urls
    assert f"{base}/modules/app-config.js" in urls and f"{base}/modules/store-assistant-v2.js" in urls
    assert len(urls) == len(set(urls))
    assert not [u for u in urls if "cdn.example.com" in u or "/api/" in u]


def test_purge_urls_include_every_top_level_html_and_webmanifest_as_bare_urls(tmp_path):
    d = _obys_tree(tmp_path)
    (d / "mockup-v4-1.html").write_text("<html>mockup</html>", encoding="utf-8")
    (d / "manifest-app.webmanifest").write_text("{}", encoding="utf-8")
    (d / "modules").mkdir()
    (d / "modules" / "inner.html").write_text("x", encoding="utf-8")
    urls = tool.purge_urls(d, SHA, "https://fb.newtalk.kr/")
    base = "https://fb.newtalk.kr/static/apps/obys"
    for name in ("mockup-v4-1.html", "clock.html", "signup.html", "manifest-app.webmanifest"):
        assert f"{base}/{name}" in urls
        assert f"{base}/{name}?v={SHA}" not in urls
    assert urls[:2] == [f"{base}/index.html", f"{base}/sw.js"]
    assert f"{base}/modules/store-assistant-v2.css" in urls and f"{base}/modules/store-assistant-v2.css?v={SHA}" in urls
    assert f"{base}/modules/app-config.js" in urls and f"{base}/modules/app-config.js?v={SHA}" in urls
    assert len(urls) == len(set(urls))
    assert f"{base}/inner.html" not in urls and f"{base}/modules/inner.html" not in urls


def test_purge_splits_a_large_top_level_url_list_into_chunks_of_at_most_30(tmp_path):
    d = _obys_tree(tmp_path)
    for i in range(40):
        (d / f"page{i}.html").write_text("<html></html>", encoding="utf-8")
    urls = tool.purge_urls(d, SHA, "https://fb.newtalk.kr")
    assert len(urls) > 30
    sent = []
    res = tool.purge(urls, {"CF_API_EMAIL": "e@x", "CF_API_KEY": "k", "CF_ZONE_ID": "z1"},
                     post=lambda url, headers, b: sent.append(b) or {"success": True})
    assert res == {"status": "ok", "urls": len(urls), "purged": len(urls)}
    assert len(sent) > 1 and all(len(b["files"]) <= 30 for b in sent)
    assert sum((b["files"] for b in sent), []) == urls


def test_purge_body_is_a_files_list_never_purge_everything(tmp_path):
    body = tool.build_purge_body(["https://fb.newtalk.kr/static/apps/obys/sw.js"])
    assert body == {"files": ["https://fb.newtalk.kr/static/apps/obys/sw.js"]}
    assert "purge_everything" not in json.dumps(body)
    sent = []
    urls = [f"https://fb.newtalk.kr/static/apps/obys/modules/f{i}.js" for i in range(65)]
    res = tool.purge(urls, {"CF_API_EMAIL": "e@x", "CF_API_KEY": "k", "CF_ZONE_ID": "z1"},
                     post=lambda url, headers, b: sent.append((url, headers, b)) or {"success": True})
    assert res == {"status": "ok", "urls": 65, "purged": 65}
    assert [len(b["files"]) for _, _, b in sent] == [30, 30, 5]
    assert all(set(b) == {"files"} for _, _, b in sent)
    assert all(u.endswith("/zones/z1/purge_cache") for u, _, _ in sent)
    assert sum((b["files"] for _, _, b in sent), []) == urls


def test_purge_failures_are_reported_without_secrets():
    creds = {"CF_API_EMAIL": "e@secret.example", "CF_API_KEY": "SECRET-KEY-VALUE", "CF_ZONE_ID": "z1"}

    def boom(url, headers, body):
        raise RuntimeError("request failed X-Auth-Key: SECRET-KEY-VALUE")

    res = tool.purge(["https://fb.newtalk.kr/a.js"], creds, post=boom)
    assert res["status"] == "failed" and res["reason"] == "RuntimeError"
    api = tool.purge(["https://fb.newtalk.kr/a.js"], creds, post=lambda *a: {"success": False, "errors": [{"code": 9109, "message": "x"}]})
    assert api["status"] == "failed" and api["codes"] == [9109]
    for r in (res, api):
        assert "SECRET" not in json.dumps(r) and "secret.example" not in json.dumps(r)


def test_missing_credentials_skip_the_purge(tmp_path):
    assert tool.load_cf_env(tmp_path / "nope") is None
    partial = tmp_path / "env"
    partial.write_text("CF_API_EMAIL=a@b\nCF_API_KEY=\nCF_ZONE_ID=z\n")
    assert tool.load_cf_env(partial) is None
    assert tool.purge(["u"], None) == {"status": "skipped", "reason": "credentials_missing", "urls": 1}
    out = subprocess.run([sys.executable, str(TOOL), "purge", "--env-file", str(tmp_path / "nope"), "https://fb.newtalk.kr/x"],
                         capture_output=True, text=True)
    assert out.returncode == 0
    assert json.loads(out.stdout.split(" ", 1)[1])["status"] == "skipped"


def test_env_file_parsing_handles_export_quotes_and_comments(tmp_path):
    f = tmp_path / "env"
    f.write_text('# c\nexport CF_API_EMAIL="a@b.c"\nCF_API_KEY=\'k1\'\nCF_ZONE_ID=z9\nOTHER=1\n')
    assert tool.load_cf_env(f) == {"CF_API_EMAIL": "a@b.c", "CF_API_KEY": "k1", "CF_ZONE_ID": "z9"}


def test_tool_has_no_hardcoded_credentials_or_zone_wide_purge():
    code = "\n".join(ln for ln in TOOL.read_text().splitlines() if not ln.lstrip().startswith("#"))
    assert "purge_everything" not in TOOL.read_text()
    assert "https://api.cloudflare.com/client/v4" in code


REL_HTML = """<html>
<link rel="stylesheet" href="modules/contract-editor-v41.css?v=20261006-r5">
<link rel="stylesheet" href='modules/data-overview.css'>
<script src="modules/contract-core.js?v=20261015-r1"></script>
<script src="/static/apps/obys/modules/app-config.js?v=old"></script>
<script src="../modules/up.js"></script>
<script src="./modules/dot.js"></script>
<script src="../x.js?v=1"></script>
<script src="https://cdn.example.com/modules/ext.js"></script>
<script src="//cdn.example.com/modules/ext2.js"></script>
<code>modules/prose.js</code>
</html>
"""


def test_stamp_html_stamps_relative_module_refs_and_keeps_them_relative():
    out, n = tool.stamp_html(REL_HTML, SHA)
    assert n == 4
    for ref in ('"modules/contract-editor-v41.css?v=%s"' % SHA, "'modules/data-overview.css?v=%s'" % SHA,
                '"modules/contract-core.js?v=%s"' % SHA):
        assert ref in out
    assert '"/static/apps/obys/modules/app-config.js?v=%s"' % SHA in out
    assert "20261006-r5" not in out and "20261015-r1" not in out and "v=old" not in out
    assert 'href="/static/apps/obys/modules/contract' not in out


def test_stamp_html_leaves_parent_dir_external_and_prose_refs_unchanged():
    out, _ = tool.stamp_html(REL_HTML, SHA)
    for keep in ('"../modules/up.js"', '"./modules/dot.js"', '"../x.js?v=1"', '"https://cdn.example.com/modules/ext.js"',
                 '"//cdn.example.com/modules/ext2.js"', "<code>modules/prose.js</code>"):
        assert keep in out


def test_relative_stamp_is_idempotent():
    once, _ = tool.stamp_html(REL_HTML, SHA)
    assert tool.stamp_html(once, SHA)[0] == once


def test_purge_urls_normalize_relative_refs_to_absolute_bare_and_versioned_urls(tmp_path):
    d = _obys_tree(tmp_path)
    (d / "mockup-v4-1.html").write_text(REL_HTML, encoding="utf-8")
    urls = tool.purge_urls(d, SHA, "https://fb.newtalk.kr/")
    base = "https://fb.newtalk.kr/static/apps/obys"
    for name in ("contract-editor-v41.css", "data-overview.css", "contract-core.js"):
        assert f"{base}/modules/{name}" in urls and f"{base}/modules/{name}?v={SHA}" in urls
    assert len(urls) == len(set(urls))
    assert not [u for u in urls if "up.js" in u or "dot.js" in u or "x.js" in u or "cdn.example.com" in u or "prose" in u]


def test_module_refs_deduplicate_absolute_and_relative_forms_in_order():
    text = '<a href="modules/a.js"></a><a href="/static/apps/obys/modules/a.js"></a><a href="modules/b.css"></a>'
    assert tool.module_refs(text) == ["/static/apps/obys/modules/a.js", "/static/apps/obys/modules/b.css"]


def test_the_real_mockup_v41_stamps_all_five_relative_modules_and_purges_them(tmp_path):
    d = tmp_path / "obys"
    d.mkdir()
    src = (ROOT / "app/static/apps/obys/mockup-v4-1.html").read_text(encoding="utf-8")
    (d / "mockup-v4-1.html").write_text(src, encoding="utf-8")
    report = tool.stamp_tree(d, SHA)
    assert report["html"]["mockup-v4-1.html"] >= 5
    text = (d / "mockup-v4-1.html").read_text(encoding="utf-8")
    names = ("contract-core.js", "contract-schedule.js", "contract-editor-v41.js", "contract-editor-v41.css", "data-overview.css")
    for name in names:
        assert f'"modules/{name}?v={SHA}"' in text
    urls = tool.purge_urls(d, SHA, "https://fb.newtalk.kr")
    for name in names:
        base = f"https://fb.newtalk.kr/static/apps/obys/modules/{name}"
        assert base in urls and f"{base}?v={SHA}" in urls
