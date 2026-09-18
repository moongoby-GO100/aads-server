"""오비서(yeoljeong) DB 접속 분리 — OBYS_DATABASE_URL 폴백 계약 검증.

AADS-OBYS-DB-SEPARATION-20260918: yeoljeong 데이터는 이미 obys DB 로
이관됐고, 애플리케이션이 그쪽을 보게 하는 배선만 남았다. 여기서는
DB 접속 없이 도는 정적/단위 검증만 한다 — obys_db_url() 의 우선순위와,
5개 서비스가 더 이상 os.getenv("DATABASE_URL" 을 직접 부르지 않는지.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.core.obys_db import obys_db_url

ROOT = Path(__file__).resolve().parents[2]

_UNIFORM_SERVICES = [
    "app/services/yeoljeong_accounting_service.py",
    "app/services/yeoljeong_dashboard_service.py",
    "app/services/yeoljeong_inventory_service.py",
    "app/services/yeoljeong_ops_service.py",
]

_ALL_FIVE_SERVICES = _UNIFORM_SERVICES + ["app/services/yeoljeong_finance_service.py"]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("OBYS_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)


def test_obys_database_url_takes_priority(monkeypatch):
    monkeypatch.setenv("OBYS_DATABASE_URL", "postgresql://obys:obys@obys-host:5432/obys")
    monkeypatch.setenv("DATABASE_URL", "postgresql://aads:aads@aads-host:5432/aads")
    assert obys_db_url() == "postgresql://obys:obys@obys-host:5432/obys"


def test_falls_back_to_database_url_when_obys_unset(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://aads:aads@aads-host:5432/aads")
    assert obys_db_url() == "postgresql://aads:aads@aads-host:5432/aads"


def test_falls_back_to_default_when_both_unset():
    assert obys_db_url() == "postgresql://aads:aads@localhost:5432/aads"


def test_empty_string_obys_database_url_is_treated_as_unset(monkeypatch):
    monkeypatch.setenv("OBYS_DATABASE_URL", "")
    monkeypatch.setenv("DATABASE_URL", "postgresql://aads:aads@aads-host:5432/aads")
    assert obys_db_url() == "postgresql://aads:aads@aads-host:5432/aads"


def test_empty_string_database_url_is_treated_as_unset(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "")
    assert obys_db_url() == "postgresql://aads:aads@localhost:5432/aads"


@pytest.mark.parametrize("relpath", _UNIFORM_SERVICES)
def test_uniform_services_delegate_to_obys_db_url(relpath):
    source = (ROOT / relpath).read_text(encoding="utf-8")
    assert "from app.core.obys_db import obys_db_url" in source
    assert "return obys_db_url()" in source


@pytest.mark.parametrize("relpath", _UNIFORM_SERVICES)
def test_uniform_services_no_longer_call_database_url_directly(relpath):
    """os.getenv("DATABASE_URL" 직접 호출이 남아 있지 않은지 정적 검사."""
    source = (ROOT / relpath).read_text(encoding="utf-8")
    assert 'os.getenv("DATABASE_URL"' not in source


def test_finance_service_keeps_its_own_priority_chain_and_feature_flag():
    """finance_service 는 지시서가 가정한 4개 서비스와 동일 패턴이 아니었다.

    실제 코드는 YEOLJEONG_FINANCE_DATABASE_URL 우선순위 오버라이드,
    postgres:// 프로토콜 정규화, "아무것도 설정 안 됐으면 빈 문자열을
    돌려줘 DB 기능 자체를 끈다"(_db_available()) 는 세 가지를 이미 갖고
    있었다. obys_db_url() 은 항상 기본값(aads DB)을 돌려주므로 그대로
    위임하면 이 세 가지가 전부 깨진다 — 그래서 obys_db_url() 로 위임하는
    대신 OBYS_DATABASE_URL 을 기존 우선순위 사슬 중간에 끼워 넣기만 했다.
    이 테스트는 그 세 가지가 살아있는지를 소스 텍스트로 확인한다
    (모듈 자체는 무거운 의존성 때문에 여기서 임포트하지 않는다).
    """
    source = (ROOT / "app/services/yeoljeong_finance_service.py").read_text(encoding="utf-8")
    db_url_fn = source.split("def _db_url() -> str:", 1)[1].split("\n\n\n", 1)[0]
    assert 'os.getenv("YEOLJEONG_FINANCE_DATABASE_URL")' in db_url_fn
    assert 'os.getenv("OBYS_DATABASE_URL")' in db_url_fn
    assert db_url_fn.index('os.getenv("YEOLJEONG_FINANCE_DATABASE_URL")') < db_url_fn.index(
        'os.getenv("OBYS_DATABASE_URL")'
    )
    assert db_url_fn.index('os.getenv("OBYS_DATABASE_URL")') < db_url_fn.index('os.getenv("DATABASE_URL"')
    assert 'return url.replace("postgresql://", "postgre' in db_url_fn
    assert 'if url else ""' in db_url_fn
