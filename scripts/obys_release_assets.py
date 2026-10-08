#!/usr/bin/env python3
"""OBYS static-asset release helper (called by deploy_acct_app_cafe24.sh). ASCII only on purpose:
the source is shipped to cafe24 over ssh and run with `python3 -c`.

  stamp <obys dir> <version>            ?v=<version> on every /static/apps/obys/modules/*.js|css ref in the
                                        top-level HTML files, and <version> into sw.js CACHE_VERSION
  urls  <obys dir> <version> <host url> file URLs to purge from Cloudflare after the switch (one per line)
  purge --env-file <path> <url...>      purge_cache(files); credentials are read from the file at run time

The repository originals are never touched (stamp runs on the build-context copy). purge only ever sends a
files list (never a whole-zone purge); missing credentials or API errors are reported in PURGE_RESULT and the exit code stays 0.
"""
import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path

MODULE_REF = re.compile(
    r"""(?P<lead>(?<=["'(]))(?P<path>/static/apps/obys/modules/[A-Za-z0-9_.-]+\.(?:js|css))(?:\?v=[^"'\s&>)#]*)?"""
)
CACHE_VERSION_LINE = re.compile(r'(const CACHE_VERSION = "obys-clock-shell-)[^"]*(";)')
VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._-]{0,39}$")
CF_ENV_KEYS = ("CF_API_EMAIL", "CF_API_KEY", "CF_ZONE_ID")
CF_API_BASE = "https://api.cloudflare.com/client/v4"
PURGE_CHUNK = 30


def _check_version(version):
    if not VERSION_RE.match(version or ""):
        raise ValueError("invalid version %r" % (version,))
    return version


def stamp_html(text, version):
    """Set ?v=<version> on every module ref (replacing any old ?v=). Returns (new text, count)."""
    _check_version(version)
    return MODULE_REF.subn(lambda m: "%s?v=%s" % (m.group("path"), version), text)


def stamp_sw(text, version):
    _check_version(version)
    return CACHE_VERSION_LINE.subn(lambda m: m.group(1) + version + m.group(2), text)


def module_refs(text):
    seen = []
    for m in MODULE_REF.finditer(text):
        if m.group("path") not in seen:
            seen.append(m.group("path"))
    return seen


def stamp_tree(obys_dir, version):
    """Rewrite top-level *.html and sw.js in place. Only ever call this on a build-context copy."""
    root = Path(obys_dir)
    report = {"version": version, "html": {}, "sw": 0}
    for page in sorted(root.glob("*.html")):
        text = page.read_text(encoding="utf-8")
        new, n = stamp_html(text, version)
        if n:
            page.write_text(new, encoding="utf-8")
            report["html"][page.name] = n
    sw = root / "sw.js"
    if sw.is_file():
        text = sw.read_text(encoding="utf-8")
        new, n = stamp_sw(text, version)
        if n != 1:
            raise ValueError("sw.js: CACHE_VERSION line not found exactly once (%d)" % n)
        sw.write_text(new, encoding="utf-8")
        report["sw"] = n
    return report


def purge_urls(obys_dir, version, host_url):
    """index.html, sw.js and every module the shells load: bare URL and ?v=<version> URL, deduplicated."""
    _check_version(version)
    base = host_url.rstrip("/")
    urls = [base + "/static/apps/obys/index.html", base + "/static/apps/obys/sw.js"]
    for page in sorted(Path(obys_dir).glob("*.html")):
        for path in module_refs(page.read_text(encoding="utf-8")):
            urls += [base + path, "%s%s?v=%s" % (base, path, version)]
    return list(dict.fromkeys(urls))


def build_purge_body(urls):
    if not urls:
        raise ValueError("no urls")
    return {"files": list(urls)}


def load_cf_env(path):
    """Read the three CF_ keys from a KEY=VALUE file. None if missing or empty. Values are never printed."""
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError:
        return None
    found = {}
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[7:].strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        if key in CF_ENV_KEYS:
            found[key] = val
    if not all(found.get(k) for k in CF_ENV_KEYS):
        return None
    return found


def _http_post(url, headers, body, timeout=15):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode() or "{}")


def purge(urls, creds, post=_http_post):
    """Send only a files list (30 per request). Result status: ok|failed|skipped, never any secret."""
    if creds is None:
        return {"status": "skipped", "reason": "credentials_missing", "urls": len(urls)}
    if not urls:
        return {"status": "skipped", "reason": "no_urls", "urls": 0}
    endpoint = "%s/zones/%s/purge_cache" % (CF_API_BASE, creds["CF_ZONE_ID"])
    headers = {
        "X-Auth-Email": creds["CF_API_EMAIL"],
        "X-Auth-Key": creds["CF_API_KEY"],
        "Content-Type": "application/json",
    }
    done = 0
    for i in range(0, len(urls), PURGE_CHUNK):
        chunk = urls[i:i + PURGE_CHUNK]
        try:
            resp = post(endpoint, headers, build_purge_body(chunk))
        except Exception as exc:  # must not fail the deploy; keep only the type - the message may echo request headers
            return {"status": "failed", "reason": type(exc).__name__, "urls": len(urls), "purged": done}
        if not isinstance(resp, dict) or resp.get("success") is not True:
            codes = [e.get("code") for e in (resp.get("errors") or []) if isinstance(e, dict)] if isinstance(resp, dict) else []
            return {"status": "failed", "reason": "api_error", "codes": codes, "urls": len(urls), "purged": done}
        done += len(chunk)
    return {"status": "ok", "urls": len(urls), "purged": done}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("stamp")
    p.add_argument("obys_dir")
    p.add_argument("version")
    p = sub.add_parser("urls")
    p.add_argument("obys_dir")
    p.add_argument("version")
    p.add_argument("host_url")
    p = sub.add_parser("purge")
    p.add_argument("--env-file", required=True)
    p.add_argument("urls", nargs="*")
    args = ap.parse_args(argv)

    if args.cmd == "stamp":
        print("OBYS_ASSET_STAMP " + json.dumps(stamp_tree(args.obys_dir, args.version), ensure_ascii=False, sort_keys=True))
    elif args.cmd == "urls":
        print("\n".join(purge_urls(args.obys_dir, args.version, args.host_url)))
    else:
        print("PURGE_RESULT " + json.dumps(purge(args.urls, load_cf_env(args.env_file)), sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
