"""Dedicated FastAPI app for the Yeoljeong store assistant.

This entrypoint keeps fb.newtalk.kr on a separate process/container from the
full AADS API while reusing the existing auth and Yeoljeong routers.
"""
from __future__ import annotations

import contextlib
import logging
import pathlib

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

import app.auth as auth_module
from app.api import acct_purchase, auth, obys_finance, obys_inventory, obys_workspaces


logger = logging.getLogger(__name__)


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
    "/static",
    "/docs",
    "/openapi.json",
    "/redoc",
)


@app.middleware("http")
async def jwt_auth_middleware(request: Request, call_next):
    path = request.url.path
    if path == "/":
        return await call_next(request)
    if any(path.startswith(prefix) for prefix in _AUTH_EXEMPT_PREFIXES):
        return await call_next(request)
    if request.method == "OPTIONS":
        return await call_next(request)

    auth_header = request.headers.get("authorization", "")
    if not auth_header.startswith("Bearer "):
        cookie_token = auth_module.extract_aads_cookie_token(request)
        if cookie_token:
            auth_header = f"Bearer {cookie_token}"
    if auth_header.startswith("Bearer "):
        payload = auth_module.verify_token(auth_header[7:])
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
app.include_router(acct_purchase.router, prefix="/api/v1", tags=["acct-purchase"])
app.include_router(obys_inventory.router, prefix="/api/v1", tags=["yeoljeong-inventory"])
app.include_router(obys_workspaces.router, prefix="/api/v1", tags=["obys-workspaces"])

_static_dir = pathlib.Path(__file__).resolve().parent / "static"
if _static_dir.is_dir():
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")
