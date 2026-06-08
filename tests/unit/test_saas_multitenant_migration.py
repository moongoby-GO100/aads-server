from pathlib import Path


def test_saas_multitenant_migration_defines_tenant_core_tables():
    sql = Path("migrations/100_saas_multitenant_foundation.sql").read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS tenants" in sql
    assert "CREATE TABLE IF NOT EXISTS tenant_memberships" in sql
    assert "CREATE TABLE IF NOT EXISTS tenant_invites" in sql
    assert "VALUES ('internal', 'AADS Internal', 'internal', 'active'" in sql
    assert "UNIQUE (tenant_id, user_id)" in sql
    assert "ux_tenant_invites_pending_email" in sql
    assert "deleted_at" in sql
    assert "CHECK (status IN ('active', 'suspended', 'archived'))" in sql


def test_saas_multitenant_migration_backfills_core_chat_tables():
    sql = Path("migrations/100_saas_multitenant_foundation.sql").read_text(encoding="utf-8")

    for table in ("saas_users", "chat_workspaces", "chat_sessions", "chat_messages"):
        assert f"ALTER TABLE {table}" in sql

    assert "ADD COLUMN IF NOT EXISTS default_tenant_id UUID" in sql
    assert "ADD COLUMN IF NOT EXISTS tenant_id UUID" in sql
    assert "UPDATE chat_workspaces" in sql
    assert "UPDATE chat_sessions s" in sql
    assert "UPDATE chat_messages m" in sql
    assert "ALTER COLUMN tenant_id SET NOT NULL" in sql
    assert "fk_chat_sessions_workspace_tenant" in sql
    assert "fk_chat_messages_session_tenant" in sql


def test_saas_multitenant_migration_keeps_legacy_inserts_tenant_safe():
    sql = Path("migrations/100_saas_multitenant_foundation.sql").read_text(encoding="utf-8")

    assert "CREATE OR REPLACE FUNCTION public.aads_internal_tenant_id()" in sql
    assert "CREATE OR REPLACE FUNCTION public.aads_set_chat_session_tenant()" in sql
    assert "CREATE OR REPLACE FUNCTION public.aads_set_chat_message_tenant()" in sql
    assert "CREATE TRIGGER trg_chat_sessions_set_tenant" in sql
    assert "CREATE TRIGGER trg_chat_messages_set_tenant" in sql
    assert "idx_chat_workspaces_tenant_created" in sql
    assert "idx_chat_sessions_tenant_workspace_updated" in sql
    assert "idx_chat_messages_tenant_session_created" in sql


def test_saas_internal_tenant_lockdown_migration_removes_public_access():
    sql = Path("migrations/104_saas_internal_tenant_access_lockdown.sql").read_text(encoding="utf-8")

    assert "ALTER COLUMN default_tenant_id DROP DEFAULT" in sql
    assert "ALTER COLUMN default_tenant_id DROP NOT NULL" in sql
    assert "preferred_customer_tenant" in sql
    assert "COALESCE(u.role, 'user') NOT IN ('ceo', 'admin', 'system')" in sql


def test_saas_customer_start_migration_enforces_internal_allowlist():
    sql = Path("migrations/105_saas_customer_start_and_internal_allowlist.sql").read_text(encoding="utf-8")

    assert "migration_105_customer_start" in sql
    assert "COALESCE(u.role, 'user') NOT IN ('ceo', 'admin', 'system')" in sql
    assert "COALESCE(u.role, 'user') IN ('ceo', 'admin', 'system')" in sql
    assert "INSERT INTO tenant_memberships" in sql
    assert "UPDATE saas_users" in sql
    assert "status = 'removed'" in sql


def test_saas_user_status_active_consistency_migration():
    sql = Path("migrations/106_saas_user_status_active_consistency.sql").read_text(encoding="utf-8")

    assert "COALESCE(status, 'active') = 'deleted'" in sql
    assert "COALESCE(status, 'active') = 'suspended'" in sql
    assert "is_active = FALSE" in sql
    assert "deleted_at = COALESCE(deleted_at, now())" in sql
