"""Fail-closed configuration and readiness for the Jinah standalone runtime.

The shared AADS entrypoint deliberately does not import this module. This is
the runtime foundation, not authorization to cut over or start collectors.
"""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping
from urllib.parse import parse_qsl, unquote, urlsplit


class RuntimeConfigurationError(RuntimeError):
    """Messages contain setting names only; never include a DSN or secret."""


def _local_database(name: str, value: str) -> str:
    try:
        url = urlsplit(value)
        query = dict(parse_qsl(url.query, strict_parsing=True))
        host = unquote(url.hostname or query.get("host", ""))
        database = unquote(url.path.removeprefix("/"))
        valid = (
            url.scheme in {"postgres", "postgresql"}
            and host in {"localhost", "127.0.0.1", "::1", "/var/run/postgresql"}
            and bool(database) and "/" not in database and not url.fragment
            and set(query) <= {"host", "sslmode"}
            and not (url.hostname and "host" in query)
            and (url.port is None or url.port == 5432)
        )
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise RuntimeConfigurationError(f"{name}: explicit local PostgreSQL DSN required")
    return database


@dataclass(frozen=True)
class RuntimeSettings:
    auth_dsn: str = field(repr=False)
    business_dsn: str = field(repr=False)
    source_dsn: str = field(repr=False)
    data_dir: Path
    upload_dir: Path

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "RuntimeSettings":
        env = os.environ if env is None else env
        names = ("OBYS_AUTH_DATABASE_URL", "OBYS_DATABASE_URL", "ACCT_DATABASE_URL")
        dsns = [(env.get(name) or "").strip() for name in names]
        databases = [_local_database(name, value) for name, value in zip(names, dsns)]
        if len(set(databases)) != 3:
            raise RuntimeConfigurationError("auth, business and source databases must be distinct")
        for alias, expected in (("DATABASE_URL", dsns[0]), ("YEOLJEONG_FINANCE_DATABASE_URL", dsns[1])):
            if env.get(alias) and env[alias] != expected:
                raise RuntimeConfigurationError(f"{alias}: conflicts with standalone database")
        if len(env.get("JWT_SECRET_KEY", "").encode()) < 32:
            raise RuntimeConfigurationError("JWT_SECRET_KEY: at least 32 bytes required")
        paths = []
        source_root = Path(__file__).resolve().parents[2]
        for name in ("YEOLJEONG_FINANCE_DATA_DIR", "OBYS_UPLOAD_ROOT"):
            raw = env.get(name, "")
            path = Path(raw)
            if not raw or not path.is_absolute():
                raise RuntimeConfigurationError(f"{name}: absolute persistent directory required")
            path = path.resolve()
            if path == Path("/") or path == source_root or source_root in path.parents:
                raise RuntimeConfigurationError(f"{name}: cannot be inside the release")
            if not path.is_dir() or not os.access(path, os.W_OK | os.X_OK):
                raise RuntimeConfigurationError(f"{name}: directory is missing or not writable")
            paths.append(path)
        return cls(*dsns, *paths)

    def apply(self) -> None:
        # Set aliases before importing any shared module with import-time config.
        os.environ["DATABASE_URL"] = self.auth_dsn
        os.environ["YEOLJEONG_FINANCE_DATABASE_URL"] = self.business_dsn


TABLES = {
    "auth": ("saas_users", "tenants", "tenant_memberships", "saas_user_consents",
             "tenant_invites", "tenant_plan_limits", "tenant_usage_overrides"),
    "business": ("yeoljeong_businesses", "yeoljeong_business_tenant_mapping",
                 "yeoljeong_uploads", "yeoljeong_uploaded_ledger_rows",
                 "yeoljeong_manual_ledger_entries", "yeoljeong_card_transactions",
                 "yeoljeong_manual_bank_transactions"),
    "source": ("source_file", "atom_record"),
}


async def check_readiness(settings: RuntimeSettings) -> dict[str, bool]:
    """Bounded read-only probes; no DDL, implicit pool, or AADS fallback."""
    import asyncpg

    async def probe(kind: str, dsn: str) -> bool:
        conn = None
        try:
            async with asyncio.timeout(5):
                conn = await asyncpg.connect(dsn, timeout=3)
                async with conn.transaction(readonly=True):
                    if await conn.fetchval(
                        "SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname=current_user"
                    ):
                        return False
                    for table in TABLES[kind]:
                        if not await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", "public." + table):
                            return False
                        # Check actual SELECT grants, not merely catalog visibility.
                        await conn.fetchval(f'SELECT 1 FROM public."{table}" LIMIT 1')
                        if kind == "source" and await conn.fetchval(
                            "SELECT has_table_privilege(current_user,$1,'INSERT,UPDATE,DELETE,TRUNCATE')",
                            "public." + table,
                        ):
                            return False
                    if kind == "auth":
                        if not await conn.fetchval("SELECT public.aads_internal_tenant_id() IS NOT NULL"):
                            return False
                return True
        except Exception:
            return False
        finally:
            if conn is not None:
                try:
                    await conn.close(timeout=1)
                except Exception:
                    conn.terminate()

    kinds = ("auth", "business", "source")
    values = await asyncio.gather(*(probe(k, d) for k, d in zip(
        kinds, (settings.auth_dsn, settings.business_dsn, settings.source_dsn)
    )))
    return dict(zip(kinds, values))
