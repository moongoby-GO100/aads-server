"""직원 서류 업로드 화면이 서버의 만료일 필수 서류 규칙을 따라가는지 고정한다.

2026-10-08: 서버는 보건증에 만료일이 없으면 400 으로 거절하는데, 직원 업로드 화면에는
만료일 칸이 없어 보건증만 계속 등록되지 않았다.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
INDEX = ROOT / "app/static/apps/obys/index.html"
SERVICE = ROOT / "app/services/yeoljeong_finance_service.py"


def _upload_form(html: str) -> str:
    match = re.search(r'<form id="onboardingUploadForm".*?</form>', html, re.S)
    assert match, "onboardingUploadForm 이 없다"
    return match.group(0)


def _server_expiry_types() -> set[str]:
    source = SERVICE.read_text(encoding="utf-8")
    match = re.search(r"ONBOARDING_EXPIRY_REQUIRED_TYPES = frozenset\(\s*\{([^}]*)\}", source)
    assert match
    return set(re.findall(r'"([a-z_]+)"', match.group(1)))


def test_upload_form_has_expiry_input() -> None:
    form = _upload_form(INDEX.read_text(encoding="utf-8"))
    assert 'name="expiresAt"' in form


def test_upload_sends_expires_at() -> None:
    html = INDEX.read_text(encoding="utf-8")
    body = html.split("async function uploadOnboardingDocument(form) {", 1)[1].split("\n    }\n", 1)[0]
    assert 'formData.append("expires_at"' in body


def test_client_expiry_required_types_match_server() -> None:
    html = INDEX.read_text(encoding="utf-8")
    match = re.search(r"const ONBOARDING_EXPIRY_REQUIRED_TYPES = new Set\(\[([^\]]*)\]\)", html)
    assert match
    assert set(re.findall(r'"([a-z_]+)"', match.group(1))) == _server_expiry_types()
