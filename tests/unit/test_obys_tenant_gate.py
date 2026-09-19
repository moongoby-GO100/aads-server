"""OBYS-SEC-TENANT-GATE-20260919-R2: 오비서 레거시 API 92개 테넌트 게이트.

obys_finance/obys_accounting/obys_ops/obys_dashboard 는 WHERE 절에 테넌트
조건이 없어 신규 가입 계정도 타 사업자 데이터를 그대로 받았다(설정 4곳,
sales 3,812행 등 재현). 데이터 모델 스코프 작업이 끝나기 전까지 라우터
단위로 허용 테넌트만 통과시키는 게이트를 검증한다. obys_inventory 는
이미 `_company_id(user)` 로 스코프가 있으므로 중복 차단하지 않는다.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core.obys_tenant import (
    _DEFAULT_LEGACY_TENANT_IDS,
    require_legacy_obys_access,
)

ALLOWED_TENANT = _DEFAULT_LEGACY_TENANT_IDS[0]
AADS_INTERNAL_TENANT = "2d701a8c-9596-4757-8588-faa4f7837112"


def _request(path: str = "/api/v1/yeoljeong-finance/settings") -> SimpleNamespace:
    return SimpleNamespace(url=SimpleNamespace(path=path))


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("OBYS_LEGACY_TENANT_IDS", raising=False)


class TestDefaultAllowList:
    async def test_allowed_tenant_passes(self):
        user = {"tenant_id": ALLOWED_TENANT}
        result = await require_legacy_obys_access(_request(), user)
        assert result is user

    async def test_aads_internal_only_passes_tenant_scoped_routes(self):
        user = {"tenant_id": AADS_INTERNAL_TENANT, "is_admin": True}
        result = await require_legacy_obys_access(
            _request("/api/v1/yeoljeong-finance/tenant-registry/businesses"), user
        )
        assert result is user

        with pytest.raises(HTTPException) as exc_info:
            await require_legacy_obys_access(_request(), user)
        assert exc_info.value.status_code == 403

    @pytest.mark.parametrize(
        "path",
        [
            "/api/v1/yeoljeong-finance/uploads",
            "/api/v1/yeoljeong-finance/uploads/00000000-0000-0000-0000-000000000001/download",
            "/api/v1/yeoljeong-finance/uploaded-ledger",
        ],
    )
    async def test_new_tenant_can_enter_only_scoped_upload_routes(self, path):
        user = {"tenant_id": "00000000-0000-0000-0000-000000000123", "is_admin": True}
        assert await require_legacy_obys_access(_request(path), user) is user

    async def test_non_allowed_tenant_is_403(self):
        user = {"tenant_id": "some-other-tenant"}
        with pytest.raises(HTTPException) as exc_info:
            await require_legacy_obys_access(_request(), user)
        assert exc_info.value.status_code == 403

    async def test_missing_tenant_id_is_403(self):
        user = {"tenant_id": None}
        with pytest.raises(HTTPException) as exc_info:
            await require_legacy_obys_access(_request(), user)
        assert exc_info.value.status_code == 403

    async def test_empty_tenant_id_is_403(self):
        user = {"tenant_id": ""}
        with pytest.raises(HTTPException) as exc_info:
            await require_legacy_obys_access(_request(), user)
        assert exc_info.value.status_code == 403


class TestEnvOverride:
    async def test_env_overrides_allow_list(self, monkeypatch):
        monkeypatch.setenv("OBYS_LEGACY_TENANT_IDS", "tenant-a, tenant-b")
        user_allowed = {"tenant_id": "tenant-b"}
        result = await require_legacy_obys_access(_request(), user_allowed)
        assert result is user_allowed

        user_blocked = {"tenant_id": ALLOWED_TENANT}
        with pytest.raises(HTTPException) as exc_info:
            await require_legacy_obys_access(_request(), user_blocked)
        assert exc_info.value.status_code == 403


class TestRouterWiring:
    def test_obys_inventory_router_has_no_gate_dependency(self):
        from app.api import obys_inventory

        assert not any(
            getattr(dep.dependency, "__name__", "") == "require_legacy_obys_access"
            for dep in obys_inventory.router.dependencies
        )

    @pytest.mark.parametrize(
        "module_name",
        ["obys_finance", "obys_accounting", "obys_ops", "obys_dashboard"],
    )
    def test_legacy_router_has_gate_dependency(self, module_name):
        module = __import__(f"app.api.{module_name}", fromlist=["router"])
        names = [
            getattr(dep.dependency, "__name__", "") for dep in module.router.dependencies
        ]
        assert "require_legacy_obys_access" in names
