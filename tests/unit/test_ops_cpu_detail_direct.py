import asyncio

import httpx
import pytest
from fastapi import FastAPI

from app import auth
from app.api import ops
from app.auth import require_internal_admin
from app.services import server_cpu_detail as cpu


def test_auth_dependency_and_access_contract(monkeypatch):
    # Exercise missing-credential rejection even where optional PyJWT is absent.
    monkeypatch.setattr(auth, 'JWT_AVAILABLE', True)
    app = FastAPI()
    app.include_router(ops.router, prefix='/api/v1')
    route = next(r for r in app.routes if getattr(r, 'path', '') == '/api/v1/ops/servers/{server_id}/cpu-detail')
    assert any(d.call is require_internal_admin for d in route.dependant.dependencies)

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            response = await client.get('/api/v1/ops/servers/contabo116/cpu-detail')
            assert response.status_code in (401, 403)
            async def denied():
                return await require_internal_admin({'is_internal_admin': False})
            app.dependency_overrides[require_internal_admin] = denied
            assert (await client.get('/api/v1/ops/servers/contabo116/cpu-detail')).status_code == 403
            async def allowed():
                return {'is_internal_admin': True}
            app.dependency_overrides[require_internal_admin] = allowed
            monkeypatch.setattr(cpu, 'transport', lambda *_: {'error': 'transport_unavailable'})
            cpu._cache.clear()
            good = await client.get('/api/v1/ops/servers/contabo116/cpu-detail')
            assert good.status_code == 200
            assert good.json()['status'] == 'transport_unavailable'
            assert (await client.get('/api/v1/ops/servers/nope/cpu-detail')).status_code == 404
            assert (await client.get('/api/v1/ops/servers/contabo116/cpu-detail?minutes=61')).status_code == 422
            assert (await client.get('/api/v1/ops/servers/contabo116/cpu-detail?top_n=21')).status_code == 422
            cpu._cache.clear()

    asyncio.run(run())
