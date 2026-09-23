"""Jinah entrypoint: uvicorn app.obys_standalone:create_app --factory.

Kept separate from the existing production entrypoint so M1 staging does not
change the current AADS-hosted service. Full migration still requires M2-M6.
"""
from contextlib import asynccontextmanager

from fastapi.responses import JSONResponse

from app.core.obys_runtime import RuntimeConfigurationError, RuntimeSettings, check_readiness


def create_app():
    settings = RuntimeSettings.from_env()
    settings.apply()

    from app.yeoljeong_main import app
    import app.auth as auth_module

    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        checks = await check_readiness(settings)
        if not all(checks.values()):
            failed = ",".join(name for name, ok in checks.items() if not ok)
            raise RuntimeConfigurationError(f"standalone database readiness failed: {failed}")
        try:
            async with original_lifespan(application):
                yield
        finally:
            if auth_module._pool is not None:
                await auth_module._pool.close()
                auth_module._pool = None
                auth_module._saas_schema_ready = False

    app.router.lifespan_context = lifespan

    @app.get("/health/ready", include_in_schema=False)
    async def readiness():
        checks = await check_readiness(settings)
        ready = all(checks.values())
        return JSONResponse(
            status_code=200 if ready else 503,
            content={"status": "ok" if ready else "unavailable", "checks": checks},
        )

    return app
