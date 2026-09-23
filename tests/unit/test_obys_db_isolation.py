"""오비서 업무 DB 격리 계약.

오비서가 AADS `DATABASE_URL` 로 폴백하면 환경변수 하나가 빠졌을 때 조용히
aads DB 의 `yeoljeong_*` 를 읽고 쓰게 되고, 두 DB 가 갈라진 뒤에야 드러난다.
진아서버 이전에서는 그대로 교차 쓰기가 되므로 폴백이 없어야 한다.
"""
from __future__ import annotations

import pytest

from app.core import obys_db
from app.services import yeoljeong_finance_service as svc


OBYS = "postgresql://obys:x@obys-host:5432/obys"
AADS = "postgresql://aads:x@aads-host:5432/aads"


def test_finance_service_never_falls_back_to_aads_database_url(monkeypatch):
    monkeypatch.delenv("YEOLJEONG_FINANCE_DATABASE_URL", raising=False)
    monkeypatch.delenv("OBYS_DATABASE_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", AADS)

    assert svc._db_url() == ""
    assert svc._db_available() is False


def test_finance_service_uses_obys_dsn(monkeypatch):
    monkeypatch.delenv("YEOLJEONG_FINANCE_DATABASE_URL", raising=False)
    monkeypatch.setenv("OBYS_DATABASE_URL", OBYS)
    monkeypatch.setenv("DATABASE_URL", AADS)

    assert svc._db_url() == OBYS.replace("postgresql://", "postgres://")
    assert svc._db_available() is True


def test_finance_service_honours_explicit_override(monkeypatch):
    override = "postgresql://yf:x@yf-host:5432/yf"
    monkeypatch.setenv("YEOLJEONG_FINANCE_DATABASE_URL", override)
    monkeypatch.setenv("OBYS_DATABASE_URL", OBYS)

    assert svc._db_url() == override.replace("postgresql://", "postgres://")


def test_obys_db_url_still_fails_loudly(monkeypatch):
    monkeypatch.delenv("OBYS_DATABASE_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", AADS)

    with pytest.raises(obys_db.ObysDatabaseUrlMissing):
        obys_db.obys_db_url()
