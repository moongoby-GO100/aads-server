"""로그인 시 시작 조직 선택 규칙.

2026-09-22 실측 회귀: role='ceo' 계정이 default_tenant_id 로 "열정국밥 운영관리"
를 지정해 두었는데도 로그인마다 internal 조직으로 되돌아가, 오비서 화면에
사업자가 하나도 없는 것처럼 보였다. 사용자가 고른 조직이 먼저다.
"""
from __future__ import annotations

import pytest

from app import auth as auth_module


INTERNAL = "2d701a8c-9596-4757-8588-faa4f7837112"
YEOLJEONG = "15055cac-71b0-45ec-b714-7093dde189ff"

MEMBERSHIPS = [
    {"tenant_id": INTERNAL, "kind": "internal", "slug": "internal"},
    {"tenant_id": YEOLJEONG, "kind": "customer", "slug": "tenant-32"},
]


@pytest.fixture
def patched(monkeypatch):
    async def list_tenants(user_id):
        return MEMBERSHIPS

    async def ensure_customer(**kwargs):  # pragma: no cover - 호출되면 실패로 본다
        raise AssertionError("소속 조직이 있는 사용자에게 새 조직을 만들면 안 된다")

    monkeypatch.setattr(auth_module, "list_user_tenants", list_tenants)
    monkeypatch.setattr(auth_module, "ensure_customer_tenant_for_user", ensure_customer)
    monkeypatch.setattr(
        auth_module, "_is_internal_tenant_principal", lambda email, role: True
    )


@pytest.mark.asyncio
async def test_explicit_default_tenant_wins_over_internal(patched):
    tenant_id = await auth_module.resolve_login_tenant_for_user(
        {"id": "u-1", "email": "moongoby@naver.com", "role": "ceo",
         "default_tenant_id": YEOLJEONG}
    )
    assert tenant_id == YEOLJEONG


@pytest.mark.asyncio
async def test_internal_principal_without_default_starts_internal(patched):
    tenant_id = await auth_module.resolve_login_tenant_for_user(
        {"id": "u-1", "email": "moongoby@naver.com", "role": "ceo"}
    )
    assert tenant_id == INTERNAL


@pytest.mark.asyncio
async def test_stale_default_tenant_is_ignored(patched):
    """소속이 끊긴 조직이 default 로 남아 있으면 그쪽으로 보내지 않는다."""
    tenant_id = await auth_module.resolve_login_tenant_for_user(
        {"id": "u-1", "email": "moongoby@naver.com", "role": "ceo",
         "default_tenant_id": "00000000-0000-0000-0000-000000000000"}
    )
    assert tenant_id == INTERNAL
