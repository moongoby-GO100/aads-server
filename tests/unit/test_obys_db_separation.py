"""오비서(yeoljeong) DB 접속 분리 — OBYS_DATABASE_URL fail-fast 계약 검증.

AADS-OBYS-DB-SEPARATION-20260918: yeoljeong 데이터는 obys DB 로 이관됐고
(O10, 2026-09-19 판정 완료) 애플리케이션 배선도 끝났다. 이관 중에는
OBYS_DATABASE_URL 이 없으면 DATABASE_URL -> aads 기본값으로 떨어지는
폴백을 뒀지만, 컷오버가 끝난 뒤 그 폴백은 회귀 경로일 뿐이라 제거했다.
여기서는 DB 접속 없이 도는 정적/단위 검증만 한다 — obys_db_url() 이
미주입 시 실패하는지와, 5개 서비스가 더 이상
os.getenv("DATABASE_URL" 을 직접 부르지 않는지.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.core.obys_db import ObysDatabaseUrlMissing, obys_db_url

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


def test_obys_database_url_is_used(monkeypatch):
    monkeypatch.setenv("OBYS_DATABASE_URL", "postgresql://obys:obys@obys-host:5432/obys")
    monkeypatch.setenv("DATABASE_URL", "postgresql://aads:aads@aads-host:5432/aads")
    assert obys_db_url() == "postgresql://obys:obys@obys-host:5432/obys"


def test_no_fallback_to_database_url_when_obys_unset(monkeypatch):
    """DATABASE_URL 이 있어도 aads DB 로 떨어지지 않는다 — 무성 회귀 차단."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://aads:aads@aads-host:5432/aads")
    with pytest.raises(ObysDatabaseUrlMissing):
        obys_db_url()


def test_raises_when_both_unset():
    with pytest.raises(ObysDatabaseUrlMissing):
        obys_db_url()


def test_empty_string_obys_database_url_is_treated_as_unset(monkeypatch):
    monkeypatch.setenv("OBYS_DATABASE_URL", "   ")
    monkeypatch.setenv("DATABASE_URL", "postgresql://aads:aads@aads-host:5432/aads")
    with pytest.raises(ObysDatabaseUrlMissing):
        obys_db_url()


def test_missing_url_error_is_a_runtime_error(monkeypatch):
    """호출부가 RuntimeError 로 잡고 있어도 그대로 걸린다."""
    assert issubclass(ObysDatabaseUrlMissing, RuntimeError)
    with pytest.raises(RuntimeError):
        obys_db_url()


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
    있었다. obys_db_url() 로 그대로 위임하면 이 세 가지가 전부 깨진다
    (지금은 미주입 시 예외까지 던진다) — 그래서 위임하는 대신
    OBYS_DATABASE_URL 을 기존 우선순위 사슬 중간에 끼워 넣기만 했다.
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
