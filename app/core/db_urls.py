"""DATABASE_URL 변환 헬퍼 (동기 SQLAlchemy 용)."""
from __future__ import annotations

import re

_SCHEME_RE = re.compile(r"^postgres(?:ql)?(?:\+[A-Za-z0-9_]+)?://", re.IGNORECASE)


def to_sync_psycopg_url(url: str) -> str:
    """DATABASE_URL 을 ``postgresql+psycopg://`` (psycopg v3) 로 강제 변환한다.

    런타임 이미지에는 psycopg2 가 없어 드라이버 미지정 URL 은 SQLAlchemy 기본값
    psycopg2 를 찾다가 ImportError 가 난다. 오류 메시지에 URL(비밀번호 포함)을
    싣지 않는다.
    """
    raw = (url or "").strip()
    match = _SCHEME_RE.match(raw)
    if not match:
        raise ValueError("DATABASE_URL 이 비어 있거나 postgres 스킴이 아닙니다")
    return "postgresql+psycopg://" + raw[match.end():]
