"""카페24 fb vhost 업스트림 판독이 주석 줄을 무시하는지 — 2026-10-06 run 5590 exit 12 재현.

설치본 vhost 9행 주석에 'http://172.28.50.2:8111' 예시가 남아 있어, 실제 운영 포트(8112) 하나만
있는데도 '두 포트'로 판정하고 릴리스를 막았다. 판독 스크립트(R_APACHE_UPSTREAM)를 그대로 꺼내 실행한다.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = (Path(__file__).resolve().parents[2] / "scripts" / "deploy_acct_app_cafe24.sh").read_text(encoding="utf-8")

VHOST = """<VirtualHost *:443>
# http://172.28.50.2:8111 is replaced at install time with http://<acct-pg bridge IP>:8111.
    ProxyPass "/api/v1/" "http://172.28.50.2:8112/api/v1/" connectiontimeout=3 timeout=600 retry=0 keepalive=On
    ProxyPassReverse "/api/v1/" "http://172.28.50.2:8112/api/v1/"
    ProxyPass "/" "http://172.28.50.2:8112/" connectiontimeout=3 timeout=300 retry=0
</VirtualHost>
"""


def _heredoc(name: str) -> str:
    match = re.search(r"read -r -d '' " + name + r" <<'EOS' \|\| true\n(.*?)\nEOS\n", SCRIPT, re.S)
    assert match, name
    return match.group(1)


def test_upstream_reader_ignores_commented_example(tmp_path):
    if not shutil.which("bash"):
        pytest.skip("bash 없음")
    site = tmp_path / "00-zz-fb.newtalk.kr.conf"
    site.write_text(VHOST, encoding="utf-8")
    out = subprocess.run(["bash", "-c", _heredoc("R_APACHE_UPSTREAM"), "x", str(site)], capture_output=True, text=True, timeout=10)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "172.28.50.2 8112"


def test_upstream_reader_still_rejects_two_live_ports(tmp_path):
    site = tmp_path / "00-zz-fb.newtalk.kr.conf"
    site.write_text(VHOST.replace('"/" "http://172.28.50.2:8112/"', '"/" "http://172.28.50.2:8113/"'), encoding="utf-8")
    out = subprocess.run(["bash", "-c", _heredoc("R_APACHE_UPSTREAM"), "x", str(site)], capture_output=True, text=True, timeout=10)
    assert out.returncode == 1


def test_switch_precheck_also_ignores_comments():
    body = _heredoc("R_APACHE_SWITCH")
    assert "grep -vE '^[[:space:]]*#' \"$SITE\"" in body
