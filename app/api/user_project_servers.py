"""User-owned project server registry.

This API stores only metadata and ownership for customer servers. Connection
secrets remain user-local through PC Agent or Agent Vault.
"""
from __future__ import annotations

import ipaddress
import json
import re
from typing import Any, Optional
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from app.auth import TenantRole, require_tenant_role
from app.core.db_pool import get_pool

router = APIRouter(prefix="/api/v1/user/project-servers", tags=["user-project-servers"])

require_tenant_member = require_tenant_role(TenantRole.MEMBER)

_HOSTNAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,251}[A-Za-z0-9]$")
_SECRET_METADATA_KEYS = {"password", "private_key", "api_key", "secret"}
_TABLE_READY = False
_DDL = """
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS user_project_servers (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id TEXT NOT NULL REFERENCES saas_users(id) ON DELETE CASCADE,
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    workspace_id UUID REFERENCES chat_workspaces(id) ON DELETE SET NULL,
    project_key TEXT NOT NULL DEFAULT '',
    label TEXT NOT NULL DEFAULT '',
    host TEXT NOT NULL,
    ssh_user TEXT NOT NULL DEFAULT 'partner',
    ssh_port INTEGER NOT NULL DEFAULT 22 CHECK (ssh_port BETWEEN 1 AND 65535),
    auth_type TEXT NOT NULL DEFAULT 'ssh_key'
        CHECK (auth_type IN ('ssh_key', 'agent_vault', 'manual')),
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'disabled', 'archived')),
    connection_state TEXT NOT NULL DEFAULT 'unverified'
        CHECK (connection_state IN ('unverified', 'reachable', 'unreachable', 'auth_failed')),
    last_checked_at TIMESTAMPTZ,
    metadata JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_user_project_servers_no_secret_metadata
        CHECK (
            NOT (metadata ? 'password')
            AND NOT (metadata ? 'private_key')
            AND NOT (metadata ? 'api_key')
            AND NOT (metadata ? 'secret')
        )
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_user_project_servers_owner_target
    ON user_project_servers(user_id, tenant_id, host, ssh_port, ssh_user)
    WHERE status <> 'archived';
CREATE INDEX IF NOT EXISTS idx_user_project_servers_owner
    ON user_project_servers(user_id, tenant_id, status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_user_project_servers_workspace
    ON user_project_servers(workspace_id)
    WHERE workspace_id IS NOT NULL;
"""


def _context_user_id(context: dict[str, Any]) -> str:
    return str(context["user"]["user_id"])


def _context_tenant_id(context: dict[str, Any]) -> str:
    tenant_id = str((context.get("tenant") or {}).get("id") or "").strip()
    if not tenant_id:
        raise HTTPException(status_code=400, detail="tenant context is required")
    return tenant_id


def _validate_host(value: str) -> str:
    host = str(value or "").strip()
    if not host:
        raise ValueError("host is required")
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass
    if not _HOSTNAME_PATTERN.match(host) or ".." in host:
        raise ValueError("host must be an IP address or hostname")
    return host.lower()


def _safe_metadata(value: dict[str, Any] | None) -> dict[str, Any]:
    metadata = dict(value or {})
    blocked = _SECRET_METADATA_KEYS.intersection(k.lower() for k in metadata)
    if blocked:
        raise ValueError(f"metadata contains secret-like keys: {sorted(blocked)}")
    return metadata


async def _ensure_table() -> None:
    global _TABLE_READY
    if _TABLE_READY:
        return
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(_DDL)
    _TABLE_READY = True


class ProjectServerCreate(BaseModel):
    label: str = Field(default="", max_length=120)
    host: str
    ssh_user: str = Field(default="partner", max_length=64)
    ssh_port: int = Field(default=22, ge=1, le=65535)
    auth_type: str = Field(default="ssh_key")
    workspace_id: Optional[str] = None
    project_key: str = Field(default="", max_length=32)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("host")
    @classmethod
    def _v_host(cls, value: str) -> str:
        return _validate_host(value)

    @field_validator("ssh_user")
    @classmethod
    def _v_ssh_user(cls, value: str) -> str:
        user = str(value or "").strip()
        if not user or not re.match(r"^[A-Za-z0-9._-]{1,64}$", user):
            raise ValueError("ssh_user is invalid")
        return user

    @field_validator("auth_type")
    @classmethod
    def _v_auth_type(cls, value: str) -> str:
        auth_type = str(value or "ssh_key").strip().lower()
        if auth_type not in {"ssh_key", "agent_vault", "manual"}:
            raise ValueError("auth_type is invalid")
        return auth_type

    @field_validator("metadata")
    @classmethod
    def _v_metadata(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _safe_metadata(value)


class ProjectServerOut(BaseModel):
    id: str
    label: str
    host: str
    ssh_user: str
    ssh_port: int
    auth_type: str
    status: str
    connection_state: str
    workspace_id: Optional[str] = None
    project_key: str
    metadata: dict[str, Any]
    last_checked_at: Optional[str] = None
    created_at: str
    updated_at: str


def _row_to_out(row: asyncpg.Record) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "label": row["label"],
        "host": row["host"],
        "ssh_user": row["ssh_user"],
        "ssh_port": int(row["ssh_port"]),
        "auth_type": row["auth_type"],
        "status": row["status"],
        "connection_state": row["connection_state"],
        "workspace_id": str(row["workspace_id"]) if row["workspace_id"] else None,
        "project_key": row["project_key"] or "",
        "metadata": dict(row["metadata"] or {}),
        "last_checked_at": row["last_checked_at"].isoformat() if row["last_checked_at"] else None,
        "created_at": row["created_at"].isoformat(),
        "updated_at": row["updated_at"].isoformat(),
    }


async def _assert_workspace_in_tenant(conn: asyncpg.Connection, workspace_id: str, tenant_id: str) -> None:
    try:
        workspace_uuid = UUID(workspace_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid workspace_id") from exc
    exists = await conn.fetchval(
        """
        SELECT EXISTS(
            SELECT 1
              FROM chat_workspaces
             WHERE id = $1
               AND tenant_id = $2::uuid
        )
        """,
        workspace_uuid,
        tenant_id,
    )
    if not exists:
        raise HTTPException(status_code=404, detail="Workspace not found for tenant")


@router.get("", response_model=list[ProjectServerOut])
async def list_project_servers(context: dict = Depends(require_tenant_member)) -> list[dict[str, Any]]:
    await _ensure_table()
    user_id = _context_user_id(context)
    tenant_id = _context_tenant_id(context)
    pool = get_pool()
    rows = await pool.fetch(
        """
        SELECT id, label, host, ssh_user, ssh_port, auth_type, status,
               connection_state, workspace_id, project_key, metadata,
               last_checked_at, created_at, updated_at
          FROM user_project_servers
         WHERE user_id = $1
           AND tenant_id = $2::uuid
           AND status <> 'archived'
         ORDER BY created_at DESC
        """,
        user_id,
        tenant_id,
    )
    return [_row_to_out(row) for row in rows]


@router.post("", response_model=ProjectServerOut, status_code=201)
async def upsert_project_server(
    body: ProjectServerCreate,
    context: dict = Depends(require_tenant_member),
) -> dict[str, Any]:
    await _ensure_table()
    user_id = _context_user_id(context)
    tenant_id = _context_tenant_id(context)
    pool = get_pool()
    async with pool.acquire() as conn:
        if body.workspace_id:
            await _assert_workspace_in_tenant(conn, body.workspace_id, tenant_id)
        row = await conn.fetchrow(
            """
            INSERT INTO user_project_servers (
                user_id, tenant_id, workspace_id, project_key, label, host,
                ssh_user, ssh_port, auth_type, metadata
            )
            VALUES (
                $1, $2::uuid, $3::uuid, $4, $5, $6, $7, $8, $9, $10::jsonb
            )
            ON CONFLICT (user_id, tenant_id, host, ssh_port, ssh_user)
                WHERE status <> 'archived'
            DO UPDATE SET
                workspace_id = EXCLUDED.workspace_id,
                project_key = EXCLUDED.project_key,
                label = EXCLUDED.label,
                auth_type = EXCLUDED.auth_type,
                metadata = EXCLUDED.metadata,
                status = 'active',
                updated_at = NOW()
            RETURNING id, label, host, ssh_user, ssh_port, auth_type, status,
                      connection_state, workspace_id, project_key, metadata,
                      last_checked_at, created_at, updated_at
            """,
            user_id,
            tenant_id,
            body.workspace_id,
            (body.project_key or "").strip().upper(),
            (body.label or "").strip(),
            body.host,
            body.ssh_user,
            body.ssh_port,
            body.auth_type,
            json.dumps(body.metadata, ensure_ascii=False),
        )
    return _row_to_out(row)


@router.delete("/{server_id}")
async def archive_project_server(
    server_id: str,
    context: dict = Depends(require_tenant_member),
) -> dict[str, Any]:
    await _ensure_table()
    user_id = _context_user_id(context)
    tenant_id = _context_tenant_id(context)
    try:
        server_uuid = UUID(server_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid server_id") from exc
    pool = get_pool()
    row = await pool.fetchrow(
        """
        UPDATE user_project_servers
           SET status = 'archived', updated_at = NOW()
         WHERE id = $1
           AND user_id = $2
           AND tenant_id = $3::uuid
         RETURNING id
        """,
        server_uuid,
        user_id,
        tenant_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Server not found")
    return {"ok": True, "id": str(row["id"])}
