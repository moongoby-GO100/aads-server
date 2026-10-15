"""ACCT-OBYS-APPICON-A-MONOGRAM-20261015 — 오비서 앱 아이콘 A안(오 모노그램) 적용 검증."""
from __future__ import annotations

import hashlib
import json
import re
import struct
from pathlib import Path

OBYS = Path(__file__).resolve().parents[2] / "app" / "static" / "apps" / "obys"
ICONS = OBYS / "icons"
URL = "/static/apps/obys/icons/"
PNG_SIZES = {"obys-app-512.png": 512, "obys-app-192.png": 192, "obys-app-180.png": 180, "obys-app-32.png": 32}
SVG_SHA256 = "da7031ae50abe6d3d0cc94be90408ff48ff1779cc0854607ca6a2eff29442748"
HTMLS = ("index.html", "mockup-v4-1.html", "clock.html")


def _png_size(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", path.name
    assert data[12:16] == b"IHDR", path.name
    return struct.unpack(">II", data[16:24])


def test_new_icon_files_exist_with_expected_png_sizes():
    for name, size in PNG_SIZES.items():
        assert _png_size(ICONS / name) == (size, size), name
    assert (ICONS / "obys-app.svg").is_file()


def test_manifests_point_to_app_icons_that_exist():
    for manifest in ("manifest-app.webmanifest", "manifest.webmanifest"):
        icons = json.loads((OBYS / manifest).read_text(encoding="utf-8"))["icons"]
        assert {i["sizes"]: i["src"] for i in icons} == {
            "192x192": URL + "obys-app-192.png",
            "512x512": URL + "obys-app-512.png",
        }, manifest
        for icon in icons:
            assert icon["purpose"] == "any maskable", manifest
            assert (OBYS / icon["src"].removeprefix("/static/apps/obys/")).is_file(), icon["src"]


def test_html_head_icon_links():
    for name in HTMLS:
        head = (OBYS / name).read_text(encoding="utf-8").split("</head>", 1)[0]
        assert f'<link rel="apple-touch-icon" href="{URL}obys-app-180.png">' in head, name
        assert f'<link rel="icon" type="image/png" sizes="32x32" href="{URL}obys-app-32.png">' in head, name
        assert f'<link rel="icon" type="image/svg+xml" href="{URL}obys-app.svg">' in head, name
        for tag in re.findall(r"<link[^>]*>", head):
            if "icon" in tag and "rel=" in tag:
                assert "obys-clock-" not in tag, (name, tag)


def test_service_worker_shell_uses_app_icons():
    sw = (OBYS / "sw.js").read_text(encoding="utf-8")
    shell = sw.split("const APP_SHELL = [", 1)[1].split("];", 1)[0]
    assert URL + "obys-app-192.png" in shell
    assert URL + "obys-app-512.png" in shell
    assert "obys-clock-1" not in shell and "obys-clock-5" not in shell
    assert 'const CACHE_PREFIX = "obys-clock-shell-";' in sw
    assert 'CACHE_VERSION = "obys-clock-shell-20261015-r1"' in sw


def test_legacy_clock_icons_kept_for_installed_pwa():
    assert (ICONS / "obys-clock-192.png").is_file()
    assert (ICONS / "obys-clock-512.png").is_file()


def test_svg_source_sha256():
    assert hashlib.sha256((ICONS / "obys-app.svg").read_bytes()).hexdigest() == SVG_SHA256
