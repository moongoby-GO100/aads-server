"""Dedicated FastAPI app for the Yeoljeong store assistant.

This entrypoint keeps fb.newtalk.kr on a separate process/container from the
full AADS API while reusing the existing auth and Yeoljeong routers.
"""
from __future__ import annotations

import contextlib
import logging
import os
import pathlib

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

import app.auth as auth_module
from app.api import (
    acct_purchase,
    auth,
    clobe_integration,
    obys_collections,
    obys_finance,
    obys_inventory,
    obys_workspaces,
    unni_naengmyeon,
)


logger = logging.getLogger(__name__)

# 카페24 컨테이너에는 PC Agent 가 없다. 통장사본 판독이 없는 에이전트를 기다리지 않게 서버 tesseract 를 기본으로 한다.
# 운영자가 OCR_BACKEND 를 명시하면 그 값이 우선하며, AADS 본 앱(app/main.py)의 기본값은 바꾸지 않는다.
OCR_BACKEND_DEFAULT = "local"


def apply_ocr_backend_default() -> str:
    return os.environ.setdefault("OCR_BACKEND", OCR_BACKEND_DEFAULT)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    """공유 asyncpg 풀을 이 프로세스에서도 연다.

    이 앱은 `app/main.py` 와 별도 프로세스로 뜨는데 기동 훅이 아예 없어
    `app.core.db_pool.init_pool()` 이 한 번도 호출되지 않았다. 그래서 풀을
    쓰는 경로(자격증명 Vault 의 `/auth/login/e2e-inject` 등)가 실행 시점에
    "DB pool이 초기화되지 않았습니다" 로 500 을 냈다 — 라우터는 등록돼 있으니
    경로 존재만 확인해서는 드러나지 않는다(2026-09-23 실측).

    대부분의 업무 API 는 자체 커넥션으로 동작하므로 초기화 실패가 기동을
    막지는 않게 하고 원인만 남긴다.
    """
    apply_ocr_backend_default()
    try:
        from app.core.db_pool import init_pool

        await init_pool()
        logger.info("yeoljeong_main: db_pool initialised")
    except Exception as exc:  # noqa: BLE001 - 기동은 계속하고 원인만 남긴다
        logger.error("yeoljeong_main: db_pool init failed | %s", exc)
    try:
        yield
    finally:
        try:
            from app.services.clobe_mcp_client import close_store_pool

            await close_store_pool()
        except Exception as exc:  # noqa: BLE001
            logger.warning("yeoljeong_main: clobe store pool close failed | %s", exc)
        try:
            from app.core.db_pool import close_pool

            await close_pool()
        except Exception as exc:  # noqa: BLE001
            logger.warning("yeoljeong_main: db_pool close failed | %s", exc)


app = FastAPI(
    title="Yeoljeong Store Assistant API",
    version="0.1.0",
    description="Isolated API surface for fb.newtalk.kr",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://fb.newtalk.kr",
        "http://localhost:8110",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_AUTH_EXEMPT_PREFIXES = (
    "/health",
    "/health/live",
    "/api/v1/health/live",
    "/api/v1/auth/login",
    "/api/v1/auth/register",
    "/api/v1/auth/me",
    # 언니냉면 공개 문의 폼(비로그인 고객). 라우터가 IP 당 빈도 제한·허니팟을 건다.
    "/api/v1/unni-naengmyeon/inquiries",
    # 조리법 화면은 미인증이면 401 JSON 이 아니라 로그인 화면으로 보내야 하므로
    # 미들웨어를 통과시키고 라우트가 직접 판정한다(unni_recipes).
    "/unni-naengmyeon",
    "/static",
    "/docs",
    "/openapi.json",
    "/redoc",
)


# 클로브AI 가 사용자 브라우저를 돌려보내는 콜백은 우리 JWT 가 없다. state(단회·10분·해시 저장)가 인증을 대신한다.
# 접두 일치가 아니라 정확 경로 하나만 면제한다. 같은 라우터의 나머지(start/status/verify/...)는 require_internal_admin.
_AUTH_EXEMPT_EXACT_PATHS = frozenset(
    {
        "/api/v1/integrations/clobe/oauth/callback",
        # 직원 가입 화면(로그인 전)의 사업자·근무지 목록. 이름만 내보내는 읽기 전용 경로 하나만 면제한다.
        "/api/v1/yeoljeong-finance/public/join-workplaces",
    }
)


def _request_token_payload(request: Request) -> dict | None:
    """Bearer 헤더, 없으면 aads_token 쿠키(오비서 로그인 시 함께 심는다)의 JWT 를 검증한다."""
    auth_header = request.headers.get("authorization", "")
    if not auth_header.startswith("Bearer "):
        cookie_token = auth_module.extract_aads_cookie_token(request)
        if cookie_token:
            auth_header = f"Bearer {cookie_token}"
    if auth_header.startswith("Bearer "):
        return auth_module.verify_token(auth_header[7:]) or None
    return None


@app.middleware("http")
async def jwt_auth_middleware(request: Request, call_next):
    path = request.url.path
    if path == "/":
        return await call_next(request)
    if path in _AUTH_EXEMPT_EXACT_PATHS or any(path.startswith(prefix) for prefix in _AUTH_EXEMPT_PREFIXES):
        return await call_next(request)
    if request.method == "OPTIONS":
        return await call_next(request)

    payload = _request_token_payload(request)
    if payload:
        request.state.user = payload
        return await call_next(request)

    return JSONResponse(status_code=401, content={"detail": "인증이 필요합니다. Bearer 토큰을 제공하세요."})


@app.get("/", include_in_schema=False)
async def root_redirect():
    return RedirectResponse("/static/apps/obys/index.html")


@app.get("/health/live", include_in_schema=False)
async def live_health_check():
    return {"status": "ok", "service": "yeoljeong-finance"}


@app.get("/api/v1/health/live", include_in_schema=False)
async def api_live_health_check():
    return {"status": "ok", "service": "yeoljeong-finance"}


app.include_router(auth.router, prefix="/api/v1", tags=["auth"])
app.include_router(obys_finance.router, prefix="/api/v1", tags=["yeoljeong-finance"])
app.include_router(obys_finance.public_router, prefix="/api/v1", tags=["yeoljeong-finance-public"])
app.include_router(acct_purchase.router, prefix="/api/v1", tags=["acct-purchase"])
app.include_router(obys_inventory.router, prefix="/api/v1", tags=["yeoljeong-inventory"])
app.include_router(obys_workspaces.router, prefix="/api/v1", tags=["obys-workspaces"])
app.include_router(obys_collections.router, prefix="/api/v1", tags=["obys-collections"])
app.include_router(clobe_integration.router, prefix="/api/v1", tags=["clobe-integration"])
app.include_router(unni_naengmyeon.router, prefix="/api/v1", tags=["unni-naengmyeon"])

# 언니냉면 조리법(직원용). fb.newtalk.kr 이 카페24로 옮겨 오면서 contabo116 대시보드(Next)의
# 서버 렌더링 대신 이 앱이 오비서 로그인 여부만 확인해 정적 HTML 을 돌려준다. 공개 화면
# (/unni-naengmyeon/, brand/*) 과 이미지는 카페24 apache 가 정적 스냅샷에서 직접 낸다
# (scripts/deploy_unni_naengmyeon_cafe24.sh, config/apache/fb-cafe24.conf BEGIN-UNNI).
_UNNI_RECIPES_HTML = pathlib.Path(__file__).resolve().parent / "sites" / "unni_naengmyeon" / "recipes.html"
_UNNI_RECIPES_LOGIN = "/static/apps/obys/index.html?redirect=/unni-naengmyeon/recipes"


@app.api_route("/unni-naengmyeon/recipes", methods=["GET", "HEAD"], include_in_schema=False)
@app.api_route("/unni-naengmyeon/recipes/", methods=["GET", "HEAD"], include_in_schema=False)
async def unni_recipes(request: Request):
    no_store = {"Cache-Control": "private, no-store"}
    if not _request_token_payload(request):
        return RedirectResponse(_UNNI_RECIPES_LOGIN, status_code=302, headers=no_store)
    if not _UNNI_RECIPES_HTML.is_file():
        return JSONResponse(status_code=404, content={"detail": "Not Found"}, headers=no_store)
    return FileResponse(_UNNI_RECIPES_HTML, media_type="text/html", headers=no_store)

_static_dir = pathlib.Path(__file__).resolve().parent / "static"
_obys_index = _static_dir / "apps" / "obys" / "index.html"


# StaticFiles(html=False) 는 디렉터리 URL 을 404 로 돌린다. 오비서 정식 URL 만 정확히 열고
# 다른 static 디렉터리는 그대로 404 로 둔다. mount 보다 먼저 등록해야 이 라우트가 잡힌다.
@app.api_route("/static/apps/obys/", methods=["GET", "HEAD"], include_in_schema=False)
async def obys_directory_index():
    if not _obys_index.is_file():
        return JSONResponse(status_code=404, content={"detail": "Not Found"})
    return FileResponse(_obys_index, media_type="text/html")


@app.api_route("/static/apps/obys", methods=["GET", "HEAD"], include_in_schema=False)
async def obys_directory_redirect(request: Request):
    target = "/static/apps/obys/"
    if request.url.query:
        target = f"{target}?{request.url.query}"
    return RedirectResponse(target, status_code=307)


if _static_dir.is_dir():
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")
