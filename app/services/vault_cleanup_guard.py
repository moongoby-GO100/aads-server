"""Vault 가 참조하는 AADS 로그인 계정·조직을 db_safe_write 정리에서 보호한다.

판정 기준 (CEO 승인 2026-09-30, 리뷰 P1-A 교정)
- 보호 대상은 활성 자격증명이 가리키는 **로그인 계정** 이다. 정규화 이메일
  (``strip().lower()``)과 내부 인증 origin 으로만 식별한다.
  authenticate_saas_user 가 이메일만으로 계정을 찾으므로, 자격증명을 **저장한 조직**
  (Vault 행의 tenant_id)은 로그인 가능 여부와 무관하다. 조직 A 의 Vault 에 조직 B
  계정이 들어 있어도 보호한다. tenant_id·created_by(소유자)는 판정에 쓰지 않는다.
- 외부 사이트 자격증명은 매칭하지 않는다. origin 호스트가 내부 인증 호스트일 때만
  대상이다 — ``aads.newtalk.kr`` + ``AADS_PUBLIC_BASE_URL`` 의 호스트 +
  ``AADS_VAULT_GUARD_INTERNAL_HOSTS``(쉼표, 오비서 standalone 호스트용).
  e2e_credentials 에 login_url 이 비어 있으면 service 가 AADS 대시보드일 때만 내부로 본다.
- 두 저장소(agent_vault_credentials, e2e_credentials)를 모두 본다. username_enc 는
  기존 credential_vault.decrypt_value 로 메모리에서만 풀고, 평문을 SQL 파라미터로
  보내지 않는다(계정 매칭은 파이썬에서). 결과·예외 메시지에 평문·암호문을 넣지 않는다.
- 복호화·조회 실패는 fail closed — VaultGuardUnavailable 로 쓰기 전체를 롤백시킨다.
- 보호 범위: saas_users 삭제·soft delete·status/is_active 비활성·이메일 변경,
  tenant_memberships 삭제·해제, 로그인 조직(default_tenant_id·활성 멤버십 조직)의
  tenants 삭제·비활성.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from app.core.credential_vault import decrypt_value

PROTECTED_TABLES = ("saas_users", "tenant_memberships", "tenants")
DEFAULT_INTERNAL_HOSTS = ("aads.newtalk.kr",)
INTERNAL_E2E_SERVICES = frozenset({"aads", "aads-dashboard", "aads.newtalk.kr"})

_PROTECTED_NAME_RE = re.compile("|".join(PROTECTED_TABLES))


class VaultLoginProtected(Exception):
    """쓰기가 활성 Vault 자격증명의 로그인 경로를 끊는다."""


class VaultGuardUnavailable(VaultLoginProtected):
    """보호 대상을 확정하지 못했다 — fail closed."""


def internal_hosts() -> frozenset[str]:
    hosts = set(DEFAULT_INTERNAL_HOSTS)
    public = urlparse(os.getenv("AADS_PUBLIC_BASE_URL", "")).hostname
    if public:
        hosts.add(public.lower())
    for raw in os.getenv("AADS_VAULT_GUARD_INTERNAL_HOSTS", "").split(","):
        if raw.strip():
            hosts.add(raw.strip().lower().rstrip("."))
    return frozenset(hosts)


def is_internal_origin(value: str | None, hosts: frozenset[str] | None = None) -> bool:
    raw = (value or "").strip()
    if not raw:
        return False
    if "://" not in raw:
        raw = "https://" + raw
    try:
        parsed = urlparse(raw)
        host = (parsed.hostname or "").rstrip(".")
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https") or not host:
        return False
    return host in (hosts if hosts is not None else internal_hosts())


def normalize_email(value: str | None) -> str:
    return (value or "").strip().lower()


def references_login_tables(sql_texts: list[str]) -> bool:
    """보호 테이블 이름이 식별자·리터럴 어디에든 보이면 True (보수적 부분일치)."""
    return any(_PROTECTED_NAME_RE.search(text.lower()) for text in sql_texts)


@dataclass
class LoginSnapshot:
    users: dict[str, dict[str, Any]] = field(default_factory=dict)
    memberships: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    tenants: dict[str, dict[str, Any]] = field(default_factory=dict)
    reference_count: int = 0


def _row_json(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return json.loads(value) if value else {}


def _user_state(row: dict[str, Any]) -> dict[str, Any]:
    # 컬럼 구성이 배포 이력마다 달라(039 는 is_active, auth.py 는 status/deleted_at)
    # to_jsonb 로 받아 있는 것만 본다.
    return {
        "email": normalize_email(row.get("email")),
        "status": row.get("status"),
        "is_active": row.get("is_active"),
        "deleted_at": row.get("deleted_at"),
        "default_tenant_id": row.get("default_tenant_id"),
    }


def _state(row: dict[str, Any]) -> dict[str, Any]:
    return {"status": row.get("status"), "deleted_at": row.get("deleted_at")}


async def _referenced_emails(conn) -> tuple[set[str], int]:
    """활성 내부 origin 자격증명이 가리키는 정규화 이메일 집합."""
    hosts = internal_hosts()
    try:
        # 트랜잭션이 끝날 때까지 참조 집합을 고정한다(동시 등록·비활성화 차단).
        await conn.execute(
            "LOCK TABLE public.agent_vault_credentials, public.e2e_credentials IN SHARE MODE"
        )
        rows = await conn.fetch("""
            SELECT origin AS login_origin, NULL::text AS service, username_enc
              FROM public.agent_vault_credentials
             WHERE is_active IS NOT FALSE
            UNION ALL
            SELECT login_url, service, username_enc
              FROM public.e2e_credentials
             WHERE is_active IS NOT FALSE
        """)
    except Exception as exc:
        raise VaultGuardUnavailable(
            f"Vault 참조 조회 실패({type(exc).__name__}) — 쓰기 차단"
        ) from None
    emails: set[str] = set()
    references = 0
    for row in rows:
        origin = row["login_origin"]
        if (origin or "").strip():
            internal = is_internal_origin(origin, hosts)
        else:
            internal = normalize_email(row["service"]) in INTERNAL_E2E_SERVICES
        if not internal:
            continue
        references += 1
        try:
            username = decrypt_value(row["username_enc"])
        except Exception as exc:
            raise VaultGuardUnavailable(
                f"Vault 로그인 참조 복호화 실패({type(exc).__name__}) — 쓰기 차단"
            ) from None
        email = normalize_email(username)
        # 로그인은 이메일로만 한다. '@' 없는 아이디는 어떤 saas_users 행과도 맞지 않는다.
        if "@" in email:
            emails.add(email)
    return emails, references


async def _load_state(conn, user_ids: list[str]) -> LoginSnapshot:
    snap = LoginSnapshot()
    if not user_ids:
        return snap
    users = await conn.fetch(
        "SELECT u.id::text AS id, to_jsonb(u) AS row FROM public.saas_users u "
        "WHERE u.id::text = ANY($1::text[]) FOR UPDATE",
        user_ids,
    )
    for row in users:
        snap.users[row["id"]] = _user_state(_row_json(row["row"]))
    members = await conn.fetch(
        "SELECT m.user_id::text AS user_id, m.tenant_id::text AS tenant_id, to_jsonb(m) AS row "
        "FROM public.tenant_memberships m WHERE m.user_id::text = ANY($1::text[]) FOR UPDATE",
        user_ids,
    )
    for row in members:
        snap.memberships[(row["user_id"], row["tenant_id"])] = _state(_row_json(row["row"]))
    return snap


async def _load_tenants(conn, tenant_ids: list[str]) -> dict[str, dict[str, Any]]:
    if not tenant_ids:
        return {}
    rows = await conn.fetch(
        "SELECT t.id::text AS id, to_jsonb(t) AS row FROM public.tenants t "
        "WHERE t.id::text = ANY($1::text[]) FOR UPDATE",
        tenant_ids,
    )
    return {row["id"]: _state(_row_json(row["row"])) for row in rows}


def _active(state: dict[str, Any]) -> bool:
    return state.get("deleted_at") is None and state.get("status") in (None, "active")


async def protected_login_snapshot(conn) -> LoginSnapshot:
    """보호 대상 계정·멤버십·조직을 잠그고 상태를 읽는다. 호출자가 트랜잭션을 연다."""
    emails, references = await _referenced_emails(conn)
    if not emails:
        return LoginSnapshot(reference_count=references)
    try:
        # 이메일 매칭은 파이썬에서 — 복호화한 평문을 SQL 로 보내지 않는다.
        rows = await conn.fetch(
            "SELECT u.id::text AS id, u.email FROM public.saas_users u"
        )
        user_ids = sorted(r["id"] for r in rows if normalize_email(r["email"]) in emails)
        snap = await _load_state(conn, user_ids)
        # 잠근 뒤 다시 확인한다(스캔과 잠금 사이 이메일 변경).
        snap.users = {k: v for k, v in snap.users.items() if v["email"] in emails}
        snap.memberships = {
            k: v for k, v in snap.memberships.items() if k[0] in snap.users and _active(v)
        }
        tenant_ids = {t for _, t in snap.memberships}
        tenant_ids |= {
            str(v["default_tenant_id"]) for v in snap.users.values() if v["default_tenant_id"]
        }
        snap.tenants = await _load_tenants(conn, sorted(tenant_ids))
    except Exception as exc:
        raise VaultGuardUnavailable(
            f"보호 계정 조회 실패({type(exc).__name__}) — 쓰기 차단"
        ) from None
    snap.reference_count = references
    return snap


async def reload_snapshot(conn, before: LoginSnapshot) -> LoginSnapshot:
    """before 와 같은 키(id)로 쓰기 후 상태를 다시 읽는다."""
    try:
        after = await _load_state(conn, sorted(before.users))
        after.tenants = await _load_tenants(conn, sorted(before.tenants))
    except Exception as exc:
        raise VaultGuardUnavailable(
            f"보호 계정 재조회 실패({type(exc).__name__}) — 쓰기 차단"
        ) from None
    after.reference_count = before.reference_count
    return after


def _user_lost(old: dict[str, Any], new: dict[str, Any] | None) -> bool:
    if new is None or new["email"] != old["email"]:
        return True
    if old["deleted_at"] is None and new["deleted_at"] is not None:
        return True
    if old["status"] in (None, "active") and new["status"] not in (None, "active"):
        return True
    if old["is_active"] is not False and new["is_active"] is False:
        return True
    return bool(old["default_tenant_id"]) and not new["default_tenant_id"]


def _state_lost(old: dict[str, Any], new: dict[str, Any] | None) -> bool:
    return _active(old) and (new is None or not _active(new))


def assert_login_preserved(before: LoginSnapshot, after: LoginSnapshot) -> None:
    """비활성·삭제 전이가 하나라도 있으면 VaultLoginProtected. 부분 성공은 없다."""
    users = sorted(k for k, v in before.users.items() if _user_lost(v, after.users.get(k)))
    members = sorted(
        k for k, v in before.memberships.items() if _state_lost(v, after.memberships.get(k))
    )
    tenants = sorted(k for k, v in before.tenants.items() if _state_lost(v, after.tenants.get(k)))
    if users or members or tenants:
        parts = []
        if users:
            parts.append(f"saas_users={','.join(users)}")
        if members:
            parts.append("tenant_memberships=" + ",".join(f"{u}@{t}" for u, t in members))
        if tenants:
            parts.append(f"tenants={','.join(tenants)}")
        raise VaultLoginProtected("Vault 참조 로그인 경로 변경 차단: " + " ".join(parts))
