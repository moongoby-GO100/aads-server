import inspect
import os

os.environ.setdefault("JWT_SECRET_KEY", "unit-test-secret")
os.environ.setdefault("E2B_API_KEY", "unit-test-e2b-key")

from app.auth import TenantRole, tenant_role_allows
from app.routers import chat as chat_router
from app.services import chat_service


def test_tenant_role_policy_order():
    assert tenant_role_allows("owner", TenantRole.ADMIN)
    assert tenant_role_allows("admin", TenantRole.MEMBER)
    assert tenant_role_allows("member", TenantRole.VIEWER)
    assert tenant_role_allows("viewer", TenantRole.VIEWER)

    assert not tenant_role_allows("viewer", TenantRole.MEMBER)
    assert not tenant_role_allows("member", TenantRole.ADMIN)
    assert not tenant_role_allows("admin", TenantRole.OWNER)
    assert not tenant_role_allows("unknown", TenantRole.VIEWER)


def test_chat_router_uses_role_dependencies_for_workspace_and_session_access():
    assert chat_router.require_tenant_viewer is not None
    assert chat_router.require_tenant_member is not None
    assert chat_router.require_tenant_admin is not None

    read_sources = [
        inspect.getsource(chat_router.get_workspaces),
        inspect.getsource(chat_router.get_sessions),
        inspect.getsource(chat_router.get_session),
        inspect.getsource(chat_router.get_session_execution),
        inspect.getsource(chat_router.get_execution),
    ]
    write_sources = [
        inspect.getsource(chat_router.create_session),
        inspect.getsource(chat_router.update_session),
    ]
    admin_sources = [
        inspect.getsource(chat_router.create_workspace),
        inspect.getsource(chat_router.update_workspace),
        inspect.getsource(chat_router.delete_workspace),
        inspect.getsource(chat_router.delete_session),
    ]

    assert all("require_tenant_viewer" in source for source in read_sources)
    assert all("tenant_id=_tenant_id(context)" in source for source in read_sources)
    assert all("require_tenant_member" in source for source in write_sources)
    assert all("tenant_id=_tenant_id(context)" in source for source in write_sources)
    assert all("require_tenant_admin" in source for source in admin_sources)
    assert all("tenant_id=_tenant_id(context)" in source for source in admin_sources)


def test_chat_service_crud_accepts_tenant_scope_and_filters_queries():
    scoped_functions = [
        chat_service.list_workspaces,
        chat_service.create_workspace,
        chat_service.update_workspace,
        chat_service.delete_workspace,
        chat_service.list_workspace_roles,
        chat_service.get_execution,
        chat_service.get_current_execution,
        chat_service.get_session,
        chat_service.list_sessions,
        chat_service.create_session,
        chat_service.update_session,
        chat_service.delete_session,
    ]

    for fn in scoped_functions:
        assert "tenant_id" in inspect.signature(fn).parameters

    service_source = "\n".join(inspect.getsource(fn) for fn in scoped_functions)
    assert "WHERE tenant_id" in service_source or "AND tenant_id" in service_source
    assert "workspace_not_found_for_tenant" in service_source
    assert "INSERT INTO chat_sessions (tenant_id, workspace_id" in service_source
    assert "INSERT INTO chat_workspaces (tenant_id, name" in service_source
