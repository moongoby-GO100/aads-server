"""Dedicated FastAPI app for the Yeoljeong store assistant.

This entrypoint keeps fb.newtalk.kr on a separate process/container from the
full AADS API while reusing the existing auth and Yeoljeong routers.
"""
from __future__ import annotations

import pathlib

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

import app.auth as auth_module
from app.api import auth, yeoljeong_finance


app = FastAPI(
    title="Yeoljeong Store Assistant API",
    version="0.1.0",
    description="Isolated API surface for fb.newtalk.kr",
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
    return RedirectResponse("/static/apps/yeoljeong-finance/index.html")


@app.get("/health/live", include_in_schema=False)
async def live_health_check():
    return {"status": "ok", "service": "yeoljeong-finance"}


@app.get("/api/v1/health/live", include_in_schema=False)
async def api_live_health_check():
    return {"status": "ok", "service": "yeoljeong-finance"}


app.include_router(auth.router, prefix="/api/v1", tags=["auth"])
app.include_router(yeoljeong_finance.router, prefix="/api/v1", tags=["yeoljeong-finance"])

_static_dir = pathlib.Path(__file__).resolve().parent / "static"
if _static_dir.is_dir():
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")
