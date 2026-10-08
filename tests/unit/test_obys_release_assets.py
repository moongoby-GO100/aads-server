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
<link rel="stylesheet" href="/static/apps/obys/modules/ledger-details.css?v=20260921-r3">
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
    assert report["sw"] == 1 and report["html"]["index.html"] == 7
    text = (d / "index.html").read_text(encoding="utf-8")
    for ref in ("app-config.js", "store-assistant-v2.js", "store-assistant-v2.css"):
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
