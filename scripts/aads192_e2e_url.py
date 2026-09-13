#!/usr/bin/env python3
"""AADS-192: 배포 탭 화면 검증용 E2E 로그인 URL 생성.

실행: docker exec aads-server-green python3 /app/scripts/aads192_e2e_url.py [redirect]
deploy.sh Step 7(QA)과 동일한 내부 관리자 신원으로 토큰을 발급한다.
"""
import sys
from urllib.parse import quote

from app.auth import create_token

USER_ID = "79ee004e-1e2e-490f-aa05-b096814f180d"
EMAIL = "moongoby@naver.com"
TENANT_ID = "2d701a8c-9596-4757-8588-faa4f7837112"


def main() -> int:
    redirect = sys.argv[1] if len(sys.argv) > 1 else "/chat"
    token = create_token(USER_ID, EMAIL, is_admin=True, tenant_id=TENANT_ID)
    print(
        "https://aads.newtalk.kr/e2e-auth.html"
        f"?token={quote(token, safe='')}&redirect={quote(redirect, safe='/')}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
