import inspect
import os

os.environ.setdefault("JWT_SECRET_KEY", "unit-test-secret")
os.environ.setdefault("E2B_API_KEY", "unit-test-e2b-key")

import app.auth as auth_module
from app.auth import TenantRole, tenant_role_allows
from app.api import auth as auth_router
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


def test_auth_request_paths_do_not_run_saas_schema_ddl():
    schema_check_source = inspect.getsource(auth_module.require_saas_schema_ready)
    assert "CREATE TABLE" not in schema_check_source
    assert "ALTER TABLE" not in schema_check_source
    assert "CREATE OR REPLACE FUNCTION" not in schema_check_source

    request_path_sources = [
        inspect.getsource(auth_module._load_tenant_context),
        inspect.getsource(auth_router.register),
        inspect.getsource(auth_router.login),
        inspect.getsource(auth_router.e2e_inject),
    ]
    assert all("require_saas_schema_ready" in source for source in request_path_sources)
    assert all("ensure_saas_users_table" not in source for source in request_path_sources)
    assert "attach_internal_tenant=False" in inspect.getsource(auth_router.register)
    assert "attach_internal_tenant: bool = False" in inspect.getsource(auth_module.create_saas_user)
    assert "create_tenant_for_user" in inspect.getsource(auth_router.register)
    assert "resolve_login_tenant_for_user" in inspect.getsource(auth_router.login)


def test_saas_onboarding_api_contract_exists_and_is_role_guarded():
    public_sources = [
        inspect.getsource(auth_router.accept_tenant_invite),
    ]
    viewer_sources = [
        inspect.getsource(auth_router.list_my_tenants),
        inspect.getsource(auth_router.create_tenant),
        inspect.getsource(auth_router.complete_onboarding),
        inspect.getsource(auth_router.switch_tenant),
        inspect.getsource(auth_router.get_tenant_usage),
    ]
    admin_sources = [
        inspect.getsource(auth_router.create_tenant_invite),
        inspect.getsource(auth_router.update_tenant_plan),
    ]

    assert all("Depends(require_tenant_role(TenantRole.VIEWER))" in source for source in viewer_sources)
    assert all("Depends(require_tenant_role(TenantRole.ADMIN))" in source for source in admin_sources)
    assert all("accept_tenant_invite" in source for source in public_sources)
    assert "_assert_path_tenant(context, tenant_id)" in inspect.getsource(auth_router.create_tenant_invite)
    assert "_assert_path_tenant(context, tenant_id)" in inspect.getsource(auth_router.update_tenant_plan)
    assert "get_tenant_usage_summary" in inspect.getsource(auth_router.get_tenant_usage)
    assert "team_invites" in inspect.getsource(auth_router.RegisterRequest)
    assert "TenantOnboardingRequest" in inspect.getsource(auth_router.complete_onboarding)


def test_saas_onboarding_service_uses_invite_tokens_and_memberships():
    service_sources = [
        inspect.getsource(auth_module.create_tenant_for_user),
        inspect.getsource(auth_module.create_tenant_invite),
        inspect.getsource(auth_module.accept_tenant_invite),
        inspect.getsource(auth_module.switch_user_tenant),
        inspect.getsource(auth_module.update_tenant_plan),
        inspect.getsource(auth_module.resolve_login_tenant_for_user),
    ]
    source = "\n".join(service_sources)

    assert "token_urlsafe" in source
    assert "sha256" in inspect.getsource(auth_module._hash_invite_token)
    assert "tenant_invites" in source
    assert "tenant_memberships" in source
    assert "default_tenant_id" in source
    assert "jsonb_set" in inspect.getsource(auth_module.update_tenant_plan)
    assert "Internal tenant invites are restricted" in inspect.getsource(auth_module.create_tenant_invite)


def test_internal_tenant_is_admin_only_for_saas_users():
    list_source = inspect.getsource(auth_module.list_user_tenants)
    context_source = inspect.getsource(auth_module._load_tenant_context)
    bootstrap_source = inspect.getsource(auth_module.ensure_saas_users_table)

    assert "_is_internal_tenant_principal" in list_source
    assert "Internal tenant requires admin role" in context_source
    assert "Internal tenant requires CEO/admin/system allowlist" in context_source
    assert "INTERNAL_TENANT_ALLOWED_ROLES = {'ceo', 'admin', 'system'}" in inspect.getsource(auth_module)
    assert "ensure_customer_tenant_for_user" in inspect.getsource(auth_module.resolve_login_tenant_for_user)
    assert "t.kind = 'customer'" in inspect.getsource(auth_module.ensure_customer_tenant_for_user)
    assert "create_tenant_for_user" in inspect.getsource(auth_module.ensure_customer_tenant_for_user)
    assert "ALTER COLUMN default_tenant_id DROP DEFAULT" in bootstrap_source
    assert "ALTER COLUMN default_tenant_id DROP NOT NULL" in bootstrap_source
    assert "WHERE role IN ('ceo', 'admin', 'owner')" in bootstrap_source


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


def test_high_risk_chat_paths_require_tenant_scope_and_filter_by_tenant():
    high_risk_functions = [
        chat_service.list_messages,
        chat_service.list_messages_cursor,
        chat_service.get_message,
        chat_service.toggle_bookmark,
        chat_service.update_message,
        chat_service.delete_message_and_response,
        chat_service.search_messages,
        chat_service.list_artifacts,
        chat_service.get_artifact,
        chat_service.update_artifact,
        chat_service.delete_artifact,
        chat_service.export_artifact,
    ]

    for fn in high_risk_functions:
        assert "tenant_id" in inspect.signature(fn).parameters

    source = "\n".join(inspect.getsource(fn) for fn in high_risk_functions)
    assert "tenant_scope_required:" in inspect.getsource(chat_service._require_tenant_uuid)
    assert "chat_messages WHERE session_id = $1 AND tenant_id" in source
    assert "WHERE id = $1 AND tenant_id = $2" in source
    assert "DELETE FROM chat_messages WHERE id = ANY($1::uuid[]) AND tenant_id" in source
    assert "chat_artifacts WHERE id = $1 AND tenant_id = $2" in source
    assert "DELETE FROM chat_artifacts WHERE id = $1 AND tenant_id = $2" in source


def test_chat_router_message_and_artifact_routes_use_tenant_dependencies():
    message_sources = [
        inspect.getsource(chat_router.get_messages),
        inspect.getsource(chat_router.get_workspace_session_messages),
        inspect.getsource(chat_router.send_message),
        inspect.getsource(chat_router.toggle_bookmark),
        inspect.getsource(chat_router.update_message),
        inspect.getsource(chat_router.delete_message),
        inspect.getsource(chat_router.search_messages),
        inspect.getsource(chat_router.get_message_detail),
    ]
    artifact_sources = [
        inspect.getsource(chat_router.get_artifacts),
        inspect.getsource(chat_router.get_artifact),
        inspect.getsource(chat_router.update_artifact),
        inspect.getsource(chat_router.delete_artifact),
        inspect.getsource(chat_router.export_artifact),
    ]

    assert all("Depends(require_tenant_" in source for source in message_sources)
    assert all("tenant_id=_tenant_id(context)" in source for source in message_sources)
    assert all("Depends(require_tenant_" in source for source in artifact_sources)
    assert all("tenant_id=_tenant_id(context)" in source for source in artifact_sources)


def test_tenant_isolation_migration_scopes_high_risk_tables_without_rls():
    from pathlib import Path

    sql = Path("migrations/101_saas_tenant_isolation_guards.sql").read_text(encoding="utf-8")

    for table in ("chat_artifacts", "e2e_credentials", "project_artifacts", "pipeline_jobs", "directive_lifecycle"):
        assert f"ALTER TABLE {table}" in sql
        assert "ADD COLUMN IF NOT EXISTS tenant_id UUID" in sql
        assert f"idx_{table.split('_')[0]}" in sql or table in {"e2e_credentials", "directive_lifecycle"}

    assert "ENABLE ROW LEVEL SECURITY" not in sql
    assert "idx_e2e_cred_tenant_service_project_label" in sql
    assert "fk_chat_artifacts_session_tenant" in sql
