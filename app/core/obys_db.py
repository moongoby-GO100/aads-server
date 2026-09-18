"""오비서(yeoljeong) 전용 DB 접속 주소."""
from __future__ import annotations

import os


class ObysDatabaseUrlMissing(RuntimeError):
    """OBYS_DATABASE_URL 이 주입되지 않았다."""


def obys_db_url() -> str:
    """오비서 전용 DB 주소. OBYS_DATABASE_URL 만 본다.

    2026-09-19 컷오버 완료(O10). 이관 전에는 코드가 먼저 배포돼도 기존
    동작이 유지되도록 DATABASE_URL -> aads 기본값 폴백을 뒀는데, 이관이
    끝난 지금 그 폴백은 회귀 경로일 뿐이다. 환경변수 하나가 빠지면
    오비서가 조용히 aads DB 의 yeoljeong_* 원본을 읽고 쓰게 되고, 두 DB 가
    갈라진 뒤에야 드러난다. 값이 없으면 그 자리에서 실패시킨다.
    """
    url = (os.getenv("OBYS_DATABASE_URL") or "").strip()
    if not url:
        raise ObysDatabaseUrlMissing(
            "OBYS_DATABASE_URL 이 설정되지 않았다. 오비서(yeoljeong) 데이터는 "
            "obys 전용 DB 에만 있으므로 aads DB 로 폴백하지 않는다. "
            ".env 에 OBYS_DATABASE_URL 을 주입하고 재배포하라."
        )
    return url
