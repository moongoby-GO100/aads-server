"""오비서(yeoljeong) 전용 DB 접속 주소."""
from __future__ import annotations

import os


def obys_db_url() -> str:
    """오비서 전용 DB 주소.

    우선순위: OBYS_DATABASE_URL -> DATABASE_URL -> 기본값
    (postgresql://aads:aads@localhost:5432/aads).
    OBYS_DATABASE_URL 이 아직 배포 환경에 주입되지 않은 상태로 코드가 먼저
    배포돼도 기존 동작(aads DB 접속)이 그대로 유지되도록 폴백을 둔다.
    """
    return (
        os.getenv("OBYS_DATABASE_URL")
        or os.getenv("DATABASE_URL")
        or "postgresql://aads:aads@localhost:5432/aads"
    )
