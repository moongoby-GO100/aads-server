import inspect
import os
from pathlib import Path

os.environ.setdefault("JWT_SECRET_KEY", "unit-test-secret")
os.environ.setdefault("E2B_API_KEY", "unit-test-e2b-key")

import app.auth as auth_module
from app.auth import TenantRole, tenant_role_allows
from app.api import agenda as agenda_router
from app.api import artifacts as artifacts_router
from app.api import assistant as assistant_router
from app.api import auth as auth_router
import app.core.memory_recall as memory_recall
from app.routers import chat as chat_router
import app.services.workspace_preloader as workspace_preloader
from app.services import agent_hooks
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
        inspect.getsource(auth_router.list_tenant_members),
        inspect.getsource(auth_router.get_tenant_usage),
    ]
    admin_sources = [
        inspect.getsource(auth_router.create_tenant_invite),
        inspect.getsource(auth_router.list_tenant_invites),
        inspect.getsource(auth_router.update_tenant_plan),
    ]

    assert all("Depends(require_tenant_role(TenantRole.VIEWER))" in source for source in viewer_sources)
    assert all("Depends(require_tenant_role(TenantRole.ADMIN))" in source for source in admin_sources)
    assert all("accept_tenant_invite" in source for source in public_sources)
    assert "_assert_path_tenant(context, tenant_id)" in inspect.getsource(auth_router.create_tenant_invite)
    assert "_assert_path_tenant(context, tenant_id)" in inspect.getsource(auth_router.list_tenant_members)
    assert "_assert_path_tenant(context, tenant_id)" in inspect.getsource(auth_router.list_tenant_invites)
    assert "_assert_path_tenant(context, tenant_id)" in inspect.getsource(auth_router.update_tenant_plan)
    assert "get_tenant_usage_summary" in inspect.getsource(auth_router.get_tenant_usage)
    assert "team_invites" in inspect.getsource(auth_router.RegisterRequest)
    assert "TenantOnboardingRequest" in inspect.getsource(auth_router.complete_onboarding)
    assert "finalize_customer_tenant_onboarding" in inspect.getsource(auth_router.complete_onboarding)
    assert "create_tenant_for_user" not in inspect.getsource(auth_router.complete_onboarding)


def test_saas_onboarding_service_uses_invite_tokens_and_memberships():
    service_sources = [
        inspect.getsource(auth_module.create_tenant_for_user),
        inspect.getsource(auth_module.create_tenant_invite),
        inspect.getsource(auth_module.accept_tenant_invite),
        inspect.getsource(auth_module.switch_user_tenant),
        inspect.getsource(auth_module.update_tenant_plan),
        inspect.getsource(auth_module.resolve_login_tenant_for_user),
        inspect.getsource(auth_module.list_tenant_members),
        inspect.getsource(auth_module.list_tenant_pending_invites),
    ]
    source = "\n".join(service_sources)

    assert "token_urlsafe" in source
    assert "sha256" in inspect.getsource(auth_module._hash_invite_token)
    assert "tenant_invites" in source
    assert "tenant_memberships" in source
    assert "default_tenant_id" in source
    assert "jsonb_set" in inspect.getsource(auth_module.update_tenant_plan)
    assert "Internal tenant invites are restricted" in inspect.getsource(auth_module.create_tenant_invite)
    assert "Customer tenant membership required" in inspect.getsource(auth_module.finalize_customer_tenant_onboarding)
    assert "token_hash" not in inspect.getsource(auth_module.list_tenant_pending_invites).split("SELECT", 1)[1]


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
    assert "WHERE role IN ('ceo', 'admin', 'system')" in bootstrap_source
    assert "WHERE role IN ('ceo', 'admin', 'owner')" not in bootstrap_source


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


def test_jarvis_external_surfaces_are_internal_or_tenant_scoped():
    assistant_source = inspect.getsource(assistant_router.get_assistant_readiness)
    assistant_guard_source = inspect.getsource(assistant_router._require_internal_admin)
    agenda_source = "\n".join(
        inspect.getsource(fn)
        for fn in [
            agenda_router.list_agendas,
            agenda_router.search_agendas,
            agenda_router.get_agenda,
            agenda_router.update_agenda,
            agenda_router.decide_agenda,
        ]
    )
    artifact_source = "\n".join(
        inspect.getsource(fn)
        for fn in [
            artifacts_router.create_artifact,
            artifacts_router.list_artifacts,
            artifacts_router.get_artifact,
        ]
    )

    assert "Depends(require_tenant_role(TenantRole.VIEWER))" in assistant_source
    assert "_require_internal_admin(context)" in assistant_source
    assert "is_internal_admin" in assistant_guard_source
    assert "Personal Assistant Hub is internal-admin only" in assistant_guard_source

    assert "include_global" in agenda_source
    assert "not include_global" in agenda_source
    assert "tenant_id=tenant_id" in agenda_source
    assert "결정(decide)은 internal admin만 수행할 수 있습니다." in agenda_source

    assert "Depends(require_tenant_member)" in artifact_source
    assert "Depends(require_tenant_viewer)" in artifact_source
    assert "tenant_id = $1::uuid" in artifact_source
    assert "AND tenant_id = $2::uuid" in artifact_source


def test_memory_context_route_and_service_are_tenant_scoped():
    route_source = inspect.getsource(chat_router.get_memory_context)
    service_source = inspect.getsource(chat_service.get_memory_context_info)
    memory_source = inspect.getsource(memory_recall)
    preload_source = inspect.getsource(workspace_preloader)

    assert "Depends(require_tenant_viewer)" in route_source
    assert "tenant_id=_tenant_id(context)" in route_source
    assert "user_id=_user_id(context)" in route_source

    assert "tenant_id" in inspect.signature(chat_service.get_memory_context_info).parameters
    assert "s.tenant_id = $2::uuid" in service_source
    assert "cs.tenant_id = $2::uuid" in service_source
    assert "cs.user_id = $3::text" in service_source

    assert "tenant_id: Optional[str] = None" in memory_source
    assert "cs.tenant_id = $1::uuid" in memory_source
    assert "cs.user_id = ${len(params)}::text" in memory_source

    assert "tenant_id = str(scope_row[\"tenant_id\"]) if scope_row[\"tenant_id\"] else None" in preload_source
    assert "cs.tenant_id = $2::uuid" in preload_source
    assert "cs.user_id = $3::text" in preload_source


def test_high_risk_action_policy_does_not_unconditionally_block_approved_ops():
    source = inspect.getsource(agent_hooks)

    assert "_HIGH_RISK_ACTION_APPROVAL_POLICY" in source
    assert "_APPROVAL_REQUIRED_COMMAND_PATTERNS" in source
    assert "_detect_approval_required_action" in source
    assert "force push 차단" in source
    assert "git push는 CEO 명시 승인" in source
    assert "approval_required_but_allowed_by_context" in source
    assert 'if tool_name == "git_remote_push":' in source
    assert 'return {"behavior": "deny", "message": reason}' not in inspect.getsource(agent_hooks.pre_tool_use_hook).split('if tool_name == "git_remote_push":', 1)[1].split("# ── query_project_database", 1)[0]


def test_chat_router_session_action_routes_are_tenant_guarded():
    viewer_sources = [
        inspect.getsource(chat_router.execution_events),
        inspect.getsource(chat_router.get_streaming_status),
        inspect.getsource(chat_router.get_last_response),
        inspect.getsource(chat_router.get_multi_discussion_status),
        inspect.getsource(chat_router.list_branches),
    ]
    member_sources = [
        inspect.getsource(chat_router.start_multi_discussion),
        inspect.getsource(chat_router.continue_multi_discussion),
        inspect.getsource(chat_router.stop_multi_discussion),
        inspect.getsource(chat_router.inject_discussion_directive),
        inspect.getsource(chat_router.stop_session_streaming),
        inspect.getsource(chat_router.interrupt_session),
        inspect.getsource(chat_router.resume_interrupted),
        inspect.getsource(chat_router.regenerate_message),
        inspect.getsource(chat_router.create_branch),
    ]

    assert all("Depends(require_tenant_viewer)" in source for source in viewer_sources)
    assert all("Depends(require_tenant_member)" in source for source in member_sources)
    assert all("tenant_id=_tenant_id(context)" in source or "tenant_id = _tenant_id(context)" in source for source in viewer_sources)
    assert all("tenant_id=_tenant_id(context)" in source or "tenant_id = _tenant_id(context)" in source for source in member_sources)
    assert "svc.get_execution" in inspect.getsource(chat_router.execution_events)
    assert "svc.get_session" in inspect.getsource(chat_router.get_last_response)
    assert "UPDATE chat_messages SET intent = 'regenerated' WHERE id = $1 AND tenant_id = $2::uuid" in inspect.getsource(chat_router.regenerate_message)
    assert "INSERT INTO chat_messages" in inspect.getsource(chat_router.create_branch)
    assert "AND tenant_id = $2::uuid" in inspect.getsource(chat_router.list_branches)


def test_tenant_isolation_migration_scopes_high_risk_tables_without_rls():
    sql = Path("migrations/101_saas_tenant_isolation_guards.sql").read_text(encoding="utf-8")

    for table in ("chat_artifacts", "e2e_credentials", "project_artifacts", "pipeline_jobs", "directive_lifecycle"):
        assert f"ALTER TABLE {table}" in sql
        assert "ADD COLUMN IF NOT EXISTS tenant_id UUID" in sql
        assert f"idx_{table.split('_')[0]}" in sql or table in {"e2e_credentials", "directive_lifecycle"}

    assert "ENABLE ROW LEVEL SECURITY" not in sql
    assert "idx_e2e_cred_tenant_service_project_label" in sql
    assert "fk_chat_artifacts_session_tenant" in sql


def test_internal_allowlist_cleanup_migration_removes_legacy_owner_access():
    sql = Path("migrations/107_saas_internal_allowlist_owner_cleanup.sql").read_text(encoding="utf-8")

    assert "slug = 'internal'" in sql
    assert "NOT IN ('ceo', 'admin', 'system')" in sql
    assert "default_tenant_id = NULL" in sql
    assert "status = 'removed'" in sql
