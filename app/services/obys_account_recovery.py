"""오비서 아이디 찾기·비밀번호 재설정.

- 아이디 찾기: 이름+연락처가 승인된 가입요청과 정확히 한 계정에 일치할 때만 가린 이메일을 돌려준다.
  일치 없음·복수 일치는 같은 문구다(계정 존재 여부·동명이인 수를 드러내지 않는다).
- 비밀번호 재설정(기본): 직원이 이메일+이름+연락처로 요청 → 같은 조직 owner/admin 이 임시 비밀번호를 발급(24시간·1회) →
  직원이 임시 비밀번호로 로그인하면 must_change_password 가 켜져 새 비밀번호를 정하기 전에는 다른 API 를 못 쓴다.
- 비밀번호 재설정(선택): 알리고 설정이 있을 때만 6자리 인증번호(5분·5회 실패 잠금).

이메일·연락처·비밀번호 원문은 로그와 재설정 테이블에 남기지 않는다. 저장은 bcrypt/HMAC 요약뿐이다.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import re
import secrets
import time
import unicodedata
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Protocol

logger = logging.getLogger(__name__)

TEMP_PASSWORD_TTL = timedelta(hours=24)
SMS_CODE_TTL = timedelta(minutes=5)
SMS_MAX_FAILURES = 5
RATE_LIMIT_MAX = 5
RATE_LIMIT_WINDOW_SECONDS = 600
MIN_PASSWORD_LENGTH = 8

MSG_FIND_ID_NO_RESULT = (
    "입력하신 정보와 일치하는 계정을 확인하지 못했습니다. 이름과 연락처를 다시 확인하시거나, "
    "소속 관리자에게 문의해 주십시오."
)
MSG_RESET_ACCEPTED = (
    "재설정 요청을 접수했습니다. 입력하신 정보가 등록 정보와 일치하면 소속 관리자에게 알림이 전달됩니다. "
    "관리자가 임시 비밀번호를 알려주면 그 비밀번호로 로그인한 뒤 새 비밀번호를 정해 주십시오."
)
MSG_RATE_LIMITED = "시도가 너무 많습니다. 10분 뒤에 다시 시도해 주십시오."
MSG_SMS_ACCEPTED = "입력하신 정보가 등록 정보와 일치하면 인증번호를 문자로 보냈습니다. 5분 안에 입력해 주십시오."
MSG_SMS_INVALID = "인증번호가 올바르지 않거나 만료되었습니다. 다시 요청하거나 관리자 승인 방식을 이용해 주십시오."

_ROLE_RANK = {"viewer": 10, "member": 20, "admin": 30, "owner": 40}
_TEMP_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789"


class RecoveryError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


# ── 순수 함수 ────────────────────────────────────────────────────────────

def mask_email(email: str) -> str:
    """ho***@gmail.com — 앞 2글자(짧으면 1글자)만 남기고 로컬파트를 가린다. 도메인은 그대로 둔다."""
    value = str(email or "").strip()
    if "@" not in value:
        return "***"
    local, _, domain = value.rpartition("@")
    keep = 2 if len(local) > 3 else 1
    return f"{local[:keep]}***@{domain}"


def normalize_name(name: str) -> str:
    text = unicodedata.normalize("NFC", str(name or ""))
    return re.sub(r"\s+", "", text).casefold()


def normalize_phone(phone: str) -> str:
    """숫자만 남기고 +82 를 0 으로 바꾼다. 휴대전화·일반전화 길이(9~11자리) 밖이면 빈 문자열."""
    digits = re.sub(r"\D+", "", str(phone or ""))
    if digits.startswith("82") and len(digits) in (11, 12):
        digits = "0" + digits[2:]
    return digits if 9 <= len(digits) <= 11 else ""


def normalize_email(email: str) -> str:
    return str(email or "").strip().lower()


def _hmac_key() -> bytes:
    from app import auth as auth_module

    return (auth_module.SECRET_KEY or "obys-recovery").encode("utf-8")


def hash_ip(ip: str) -> str:
    return hmac.new(_hmac_key(), f"ip:{ip or ''}".encode("utf-8"), hashlib.sha256).hexdigest()[:32]


def generate_temp_password() -> str:
    while True:
        value = "".join(secrets.choice(_TEMP_ALPHABET) for _ in range(10))
        if any(c.isupper() for c in value) and any(c.islower() for c in value) and any(c.isdigit() for c in value):
            return value


def generate_sms_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def validate_new_password(password: str) -> str:
    value = str(password or "")
    if len(value) < MIN_PASSWORD_LENGTH:
        raise RecoveryError(400, f"비밀번호는 {MIN_PASSWORD_LENGTH}자 이상이어야 합니다.")
    if len(value.encode("utf-8")) > 72:
        raise RecoveryError(400, "비밀번호가 너무 깁니다. 72바이트 이하로 입력해 주십시오.")
    if not (any(c.isalpha() for c in value) and any(c.isdigit() for c in value)):
        raise RecoveryError(400, "비밀번호는 영문과 숫자를 모두 포함해야 합니다.")
    return value


def _bcrypt():
    from app import auth as auth_module

    if auth_module._bcrypt_mod is None:
        raise RecoveryError(503, "비밀번호 모듈을 사용할 수 없습니다.")
    return auth_module._bcrypt_mod


def hash_secret(secret: str) -> str:
    return _bcrypt().hashpw(secret.encode("utf-8"), _bcrypt().gensalt()).decode("utf-8")


def check_secret(secret: str, hashed: str) -> bool:
    try:
        return bool(_bcrypt().checkpw(secret.encode("utf-8"), str(hashed or "").encode("utf-8")))
    except (ValueError, TypeError):
        return False


def role_rank(role: Optional[str]) -> int:
    return _ROLE_RANK.get(str(role or "").lower(), 0)


class RateLimiter:
    """프로세스 안 슬라이딩 윈도우. 키에는 원문 대신 해시를 넣는다."""

    def __init__(self, max_attempts: int = RATE_LIMIT_MAX, window_seconds: int = RATE_LIMIT_WINDOW_SECONDS, clock=time.monotonic):
        self.max_attempts = max_attempts
        self.window = window_seconds
        self._clock = clock
        self._hits: dict[str, deque] = defaultdict(deque)

    def hit(self, key: str) -> bool:
        """시도를 기록한다. 한도를 넘었으면 False."""
        now = self._clock()
        bucket = self._hits[key]
        while bucket and now - bucket[0] > self.window:
            bucket.popleft()
        if len(bucket) >= self.max_attempts:
            return False
        bucket.append(now)
        return True

    def reset(self) -> None:
        self._hits.clear()


_limiter = RateLimiter()


def limiter() -> RateLimiter:
    return _limiter


def _rate_key(kind: str, ip: str, subject: str) -> str:
    return f"{kind}:{hash_ip(ip)}:{hashlib.sha256(subject.encode('utf-8')).hexdigest()[:16]}"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ── 저장소 계약 ───────────────────────────────────────────────────────────

class RecoveryStore(Protocol):
    async def schema_ready(self) -> bool: ...
    async def find_accounts(self, name: str, phone: str, email: Optional[str]) -> list[dict]: ...
    async def open_request(self, account: dict, method: str, ip_hash: str) -> str: ...
    async def notify_admins(self, account: dict, request_id: str) -> None: ...
    async def list_requests(self, tenant_id: str) -> list[dict]: ...
    async def get_request(self, request_id: str) -> Optional[dict]: ...
    async def member_roles(self, user_id: str) -> dict[str, str]: ...
    async def issue_temp(self, request_id: str, tenant_id: str, issued_by: str, temp_hash: str, expires_at: datetime) -> bool: ...
    async def cancel_request(self, request_id: str, tenant_id: str, cancelled_by: str) -> bool: ...
    async def consume_temp(self, email: str, password: str) -> Optional[dict]: ...
    async def set_sms_code(self, request_id: str, code_hash: str, expires_at: datetime) -> None: ...
    async def verify_sms(self, email: str, code: str) -> Optional[dict]: ...
    async def apply_new_password(self, user_id: str, password_hash: str, complete_methods: tuple[str, ...]) -> dict: ...
    async def user_password_hash(self, user_id: str) -> Optional[str]: ...
    async def audit(self, event: str, *, request_id: Optional[str] = None, tenant_id: Optional[str] = None,
                    user_id: Optional[str] = None, actor: Optional[str] = None, ip_hash: str = "") -> None: ...
    async def read_phone(self, account: dict) -> str: ...


async def _hash_async(secret: str) -> str:
    return await asyncio.to_thread(hash_secret, secret)


async def _check_async(secret: str, hashed: str) -> bool:
    return await asyncio.to_thread(check_secret, secret, hashed)


def sms_enabled() -> bool:
    try:
        from app.services import aligo_client

        return bool(aligo_client.is_available() and (os.environ.get("ALIGO_SENDER") or "").strip())
    except Exception:  # noqa: BLE001
        return False


def options() -> dict:
    return {
        "find_id": True,
        "reset_admin_approval": True,
        "reset_sms": sms_enabled(),
        "admin_approval_note": "관리자가 임시 비밀번호를 발급하면 그 비밀번호로 로그인한 뒤 새 비밀번호를 정합니다.",
    }


# ── 서비스 ────────────────────────────────────────────────────────────────

def _unique_accounts(accounts: list[dict]) -> list[dict]:
    seen: dict[str, dict] = {}
    for acc in accounts:
        seen.setdefault(str(acc["user_id"]), acc)
    return list(seen.values())


async def find_id(store: RecoveryStore, name: str, phone: str, ip: str) -> dict:
    norm_name, norm_phone = normalize_name(name), normalize_phone(phone)
    if not _limiter.hit(_rate_key("find", ip, norm_name)):
        raise RecoveryError(429, MSG_RATE_LIMITED)
    miss = {"found": False, "masked_email": None, "message": MSG_FIND_ID_NO_RESULT}
    if not norm_name or not norm_phone:
        return miss
    accounts = _unique_accounts(await store.find_accounts(norm_name, norm_phone, None))
    await store.audit("find_id_hit" if len(accounts) == 1 else "find_id_miss", ip_hash=hash_ip(ip))
    if len(accounts) != 1:
        return miss
    return {"found": True, "masked_email": mask_email(accounts[0]["email"]),
            "message": "가입하신 아이디(이메일)는 아래와 같습니다."}


async def request_password_reset(store: RecoveryStore, email: str, name: str, phone: str, ip: str) -> dict:
    norm_email, norm_name, norm_phone = normalize_email(email), normalize_name(name), normalize_phone(phone)
    if not _limiter.hit(_rate_key("reset", ip, norm_email)):
        raise RecoveryError(429, MSG_RATE_LIMITED)
    accepted = {"accepted": True, "message": MSG_RESET_ACCEPTED}
    if not (norm_email and norm_name and norm_phone):
        return accepted
    ip_h = hash_ip(ip)
    accounts = _unique_accounts(await store.find_accounts(norm_name, norm_phone, norm_email))
    if len(accounts) != 1:
        await store.audit("reset_request_miss", ip_hash=ip_h)
        return accepted
    account = accounts[0]
    request_id = await store.open_request(account, "admin", ip_h)
    await store.audit("reset_requested", request_id=request_id, tenant_id=account["tenant_id"],
                      user_id=account["user_id"], ip_hash=ip_h)
    try:
        await store.notify_admins(account, request_id)
    except Exception as exc:  # noqa: BLE001 - 알림 실패가 요청 접수를 막지 않는다. 목록에는 남는다.
        logger.warning("obys recovery: admin notify failed: %s", type(exc).__name__)
    return accepted


async def request_sms_code(store: RecoveryStore, email: str, name: str, phone: str, ip: str) -> dict:
    if not sms_enabled():
        raise RecoveryError(404, "문자 인증은 사용할 수 없습니다. 관리자 승인 방식을 이용해 주십시오.")
    norm_email, norm_name, norm_phone = normalize_email(email), normalize_name(name), normalize_phone(phone)
    if not _limiter.hit(_rate_key("sms", ip, norm_email)):
        raise RecoveryError(429, MSG_RATE_LIMITED)
    accepted = {"accepted": True, "message": MSG_SMS_ACCEPTED}
    if not (norm_email and norm_name and norm_phone):
        return accepted
    ip_h = hash_ip(ip)
    accounts = _unique_accounts(await store.find_accounts(norm_name, norm_phone, norm_email))
    if len(accounts) != 1:
        await store.audit("sms_request_miss", ip_hash=ip_h)
        return accepted
    account = accounts[0]
    request_id = await store.open_request(account, "sms", ip_h)
    code = generate_sms_code()
    await store.set_sms_code(request_id, await _hash_async(code), _utcnow() + SMS_CODE_TTL)
    try:
        from app.services import aligo_client

        result = await aligo_client.send_sms(await store.read_phone(account), f"[오비서] 인증번호 {code} (5분 안에 입력)")
        sent = str(result.get("result_code")) == "1"
    except Exception as exc:  # noqa: BLE001
        logger.warning("obys recovery: sms send failed: %s", type(exc).__name__)
        sent = False
    await store.audit("sms_sent" if sent else "sms_send_failed", request_id=request_id,
                      tenant_id=account["tenant_id"], user_id=account["user_id"], ip_hash=ip_h)
    return accepted


async def confirm_sms_reset(store: RecoveryStore, email: str, code: str, new_password: str, ip: str) -> dict:
    if not sms_enabled():
        raise RecoveryError(404, "문자 인증은 사용할 수 없습니다. 관리자 승인 방식을 이용해 주십시오.")
    norm_email = normalize_email(email)
    if not _limiter.hit(_rate_key("smsconfirm", ip, norm_email)):
        raise RecoveryError(429, MSG_RATE_LIMITED)
    validate_new_password(new_password)
    user = await store.verify_sms(norm_email, str(code or "").strip())
    if not user:
        raise RecoveryError(400, MSG_SMS_INVALID)
    result = await store.apply_new_password(user["user_id"], await _hash_async(new_password), ("sms", "admin"))
    await store.audit("sms_reset_completed", user_id=user["user_id"], ip_hash=hash_ip(ip))
    return {"completed": True, "message": "비밀번호를 변경했습니다. 새 비밀번호로 로그인해 주십시오.", **result}


async def list_reset_requests(store: RecoveryStore, tenant_id: str) -> list[dict]:
    return await store.list_requests(tenant_id)


async def issue_temp_password(store: RecoveryStore, *, request_id: str, actor_id: str, actor_role: str,
                              actor_tenant_id: str, actor_is_internal_admin: bool = False) -> dict:
    """같은 조직 요청만, 자기보다 낮은 역할의 계정에만 발급한다. 임시 비밀번호 원문은 이 응답에서 한 번만 나간다."""
    req = await store.get_request(request_id)
    if not req or str(req["tenant_id"]) != str(actor_tenant_id) or req["method"] != "admin":
        raise RecoveryError(404, "재설정 요청을 찾을 수 없습니다.")
    if req["status"] not in ("pending", "issued"):
        raise RecoveryError(409, "이미 처리된 요청입니다.")
    target_id = str(req["user_id"])
    if target_id == str(actor_id):
        raise RecoveryError(403, "본인의 재설정 요청은 직접 승인할 수 없습니다. 다른 관리자에게 요청해 주십시오.")
    if not actor_is_internal_admin:
        roles = await store.member_roles(target_id)
        if str(actor_tenant_id) not in roles:
            raise RecoveryError(404, "재설정 요청을 찾을 수 없습니다.")
        if role_rank(roles[str(actor_tenant_id)]) >= role_rank(actor_role):
            raise RecoveryError(403, "같거나 높은 권한의 계정은 발급할 수 없습니다.")
        if any(t != str(actor_tenant_id) and role_rank(r) >= role_rank("admin") for t, r in roles.items()):
            raise RecoveryError(403, "다른 조직의 관리자 권한을 가진 계정은 이 조직에서 발급할 수 없습니다.")
    temp = generate_temp_password()
    expires_at = _utcnow() + TEMP_PASSWORD_TTL
    ok = await store.issue_temp(request_id, str(actor_tenant_id), actor_id, await _hash_async(temp), expires_at)
    if not ok:
        raise RecoveryError(409, "이미 처리된 요청입니다.")
    await store.audit("temp_issued", request_id=request_id, tenant_id=str(actor_tenant_id), user_id=target_id, actor=actor_id)
    return {"request_id": request_id, "temp_password": temp, "expires_at": expires_at.isoformat(),
            "message": "임시 비밀번호는 지금 한 번만 표시됩니다. 직원에게 직접 전달해 주십시오."}


async def cancel_reset_request(store: RecoveryStore, *, request_id: str, actor_id: str, actor_tenant_id: str) -> dict:
    req = await store.get_request(request_id)
    if not req or str(req["tenant_id"]) != str(actor_tenant_id):
        raise RecoveryError(404, "재설정 요청을 찾을 수 없습니다.")
    if not await store.cancel_request(request_id, str(actor_tenant_id), actor_id):
        raise RecoveryError(409, "이미 처리된 요청입니다.")
    await store.audit("request_cancelled", request_id=request_id, tenant_id=str(actor_tenant_id),
                      user_id=str(req["user_id"]), actor=actor_id)
    return {"cancelled": True}


async def try_temp_password_login(store: RecoveryStore, email: str, password: str) -> Optional[dict]:
    """일반 비밀번호가 틀렸을 때만 호출. 유효한 임시 비밀번호면 소진하고 must_change_password 를 켠다."""
    user = await store.consume_temp(normalize_email(email), password)
    if user:
        await store.audit("temp_used", request_id=user.get("request_id"), tenant_id=user.get("tenant_id"), user_id=user["id"])
    return user


async def change_password(store: RecoveryStore, *, user_id: str, new_password: str,
                          current_password: Optional[str], must_change: bool) -> dict:
    validate_new_password(new_password)
    if not must_change:
        stored = await store.user_password_hash(user_id)
        if not current_password or not stored or not await _check_async(current_password, stored):
            raise RecoveryError(400, "현재 비밀번호가 올바르지 않습니다.")
    result = await store.apply_new_password(user_id, await _hash_async(new_password), ("admin", "sms"))
    await store.audit("password_changed", user_id=user_id, actor=user_id)
    return result


# ── Postgres 구현 ─────────────────────────────────────────────────────────

_REQ_COLUMNS = (
    "id::text AS id, tenant_id::text AS tenant_id, user_id, business_id, method, status, requested_at, "
    "issued_by, issued_at, temp_expires_at, temp_used_at"
)


def _business_dsn() -> str:
    url = (os.getenv("YEOLJEONG_FINANCE_DATABASE_URL") or os.getenv("OBYS_DATABASE_URL") or "").strip()
    return url.replace("postgresql://", "postgres://")


class PgStore:
    """인증 DB(obys_auth: saas_users·재설정 테이블)와 업무 DB(obys: 가입요청 연락처)를 따로 연다."""

    async def _auth_pool(self):
        from app import auth as auth_module

        return await auth_module._ensure_pool()

    async def schema_ready(self) -> bool:
        pool = await self._auth_pool()
        async with pool.acquire() as conn:
            return bool(await conn.fetchval(
                "SELECT to_regclass('public.obys_password_reset_requests') IS NOT NULL "
                "AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' "
                "AND table_name='saas_users' AND column_name='must_change_password')"
            ))

    async def _business_rows(self, name_norm: str, phone_norm: str, email: Optional[str]) -> list[dict]:
        dsn = _business_dsn()
        if not dsn:
            return []
        import asyncpg

        conn = await asyncpg.connect(dsn, timeout=5)
        try:
            rows = await conn.fetch(
                """
                SELECT lower(employee_email) AS email, employee_name, phone, business_id, tenant_id::text AS tenant_id
                  FROM yeoljeong_employee_join_requests
                 WHERE lower(status) = 'approved' AND deleted_at IS NULL
                   AND ($1::text IS NULL OR lower(employee_email) = $1)
                """,
                email,
            )
        finally:
            await conn.close()
        return [
            dict(r) for r in rows
            if normalize_name(r["employee_name"]) == name_norm and normalize_phone(r["phone"]) == phone_norm
        ]

    async def find_accounts(self, name: str, phone: str, email: Optional[str]) -> list[dict]:
        matched = await self._business_rows(name, phone, email)
        if not matched:
            return []
        pool = await self._auth_pool()
        out: list[dict] = []
        async with pool.acquire() as conn:
            for row in matched:
                user = await conn.fetchrow(
                    """
                    SELECT u.id, u.email FROM saas_users u
                      JOIN tenant_memberships m ON m.user_id = u.id AND m.tenant_id = $2::uuid
                           AND m.status = 'active' AND m.deleted_at IS NULL
                     WHERE lower(u.email) = $1 AND COALESCE(u.status, 'active') = 'active' AND u.deleted_at IS NULL
                    """,
                    row["email"], row["tenant_id"],
                )
                if user:
                    out.append({"user_id": str(user["id"]), "email": user["email"], "tenant_id": row["tenant_id"],
                                "business_id": row["business_id"] or "", "name": row["employee_name"]})
        return out

    async def read_phone(self, account: dict) -> str:
        dsn = _business_dsn()
        if not dsn:
            return ""
        import asyncpg

        conn = await asyncpg.connect(dsn, timeout=5)
        try:
            value = await conn.fetchval(
                "SELECT phone FROM yeoljeong_employee_join_requests WHERE lower(employee_email) = $1 "
                "AND tenant_id = $2::uuid AND lower(status) = 'approved' AND deleted_at IS NULL "
                "ORDER BY requested_at DESC LIMIT 1",
                account["email"].lower(), account["tenant_id"],
            )
        finally:
            await conn.close()
        return normalize_phone(value or "")

    async def open_request(self, account: dict, method: str, ip_hash: str) -> str:
        pool = await self._auth_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO obys_password_reset_requests (tenant_id, user_id, business_id, method, requested_ip_hash)
                VALUES ($1::uuid, $2, $3, $4, $5)
                ON CONFLICT (user_id, tenant_id, method) WHERE status IN ('pending', 'issued')
                DO UPDATE SET requested_at = now(), updated_at = now(), requested_ip_hash = EXCLUDED.requested_ip_hash
                RETURNING id::text AS id
                """,
                account["tenant_id"], account["user_id"], account.get("business_id", ""), method, ip_hash,
            )
        return row["id"]

    async def notify_admins(self, account: dict, request_id: str) -> None:
        pool = await self._auth_pool()
        async with pool.acquire() as conn:
            admins = await conn.fetch(
                """
                SELECT u.email FROM tenant_memberships m JOIN saas_users u ON u.id = m.user_id
                 WHERE m.tenant_id = $1::uuid AND m.role IN ('owner', 'admin') AND m.status = 'active'
                   AND m.deleted_at IS NULL AND u.deleted_at IS NULL AND u.id <> $2
                """,
                account["tenant_id"], account["user_id"],
            )
        from app.services import yeoljeong_ops_service

        who = str(account.get("name") or "직원").strip() or "직원"
        for admin in admins:
            await yeoljeong_ops_service.create_notification(
                business_id=account.get("business_id", ""),
                target_user=str(admin["email"]).lower(),
                notification_type="password_reset_requested",
                title=f"{who}님 비밀번호 재설정 요청",
                body="직원 관리의 '비밀번호 재설정 요청'에서 임시 비밀번호를 발급해 주십시오.",
                reference_type="password_reset_request",
                reference_id=request_id,
            )

    async def list_requests(self, tenant_id: str) -> list[dict]:
        pool = await self._auth_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT r.id::text AS id, r.user_id, r.status, r.requested_at, r.issued_at, r.temp_expires_at,
                       u.name AS user_name, u.email AS user_email
                  FROM obys_password_reset_requests r JOIN saas_users u ON u.id = r.user_id
                 WHERE r.tenant_id = $1::uuid AND r.method = 'admin' AND r.status IN ('pending', 'issued')
                 ORDER BY r.requested_at DESC LIMIT 100
                """,
                tenant_id,
            )
        return [
            {
                "id": r["id"], "user_id": r["user_id"], "user_name": r["user_name"] or "",
                "masked_email": mask_email(r["user_email"]), "status": r["status"],
                "requested_at": r["requested_at"].isoformat() if r["requested_at"] else None,
                "issued_at": r["issued_at"].isoformat() if r["issued_at"] else None,
                "temp_expires_at": r["temp_expires_at"].isoformat() if r["temp_expires_at"] else None,
            }
            for r in rows
        ]

    async def get_request(self, request_id: str) -> Optional[dict]:
        if not re.fullmatch(r"[0-9a-fA-F-]{36}", str(request_id or "")):
            return None
        pool = await self._auth_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT {_REQ_COLUMNS} FROM obys_password_reset_requests WHERE id = $1::uuid", request_id)
        return dict(row) if row else None

    async def member_roles(self, user_id: str) -> dict[str, str]:
        pool = await self._auth_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT tenant_id::text AS tenant_id, role FROM tenant_memberships "
                "WHERE user_id = $1 AND status = 'active' AND deleted_at IS NULL", user_id)
        return {r["tenant_id"]: r["role"] for r in rows}

    async def issue_temp(self, request_id: str, tenant_id: str, issued_by: str, temp_hash: str, expires_at: datetime) -> bool:
        pool = await self._auth_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    UPDATE obys_password_reset_requests
                       SET status = 'issued', issued_by = $3, issued_at = now(), temp_password_hash = $4,
                           temp_expires_at = $5, temp_used_at = NULL, updated_at = now()
                     WHERE id = $1::uuid AND tenant_id = $2::uuid AND method = 'admin' AND status IN ('pending', 'issued')
                    RETURNING user_id
                    """,
                    request_id, tenant_id, issued_by, temp_hash, expires_at,
                )
                if not row:
                    return False
                await conn.execute("UPDATE saas_users SET session_version = session_version + 1 WHERE id = $1", row["user_id"])
        return True

    async def cancel_request(self, request_id: str, tenant_id: str, cancelled_by: str) -> bool:
        pool = await self._auth_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE obys_password_reset_requests
                   SET status = 'cancelled', cancelled_at = now(), cancelled_by = $3, temp_password_hash = NULL, updated_at = now()
                 WHERE id = $1::uuid AND tenant_id = $2::uuid AND status IN ('pending', 'issued')
                RETURNING id
                """,
                request_id, tenant_id, cancelled_by,
            )
        return bool(row)

    async def consume_temp(self, email: str, password: str) -> Optional[dict]:
        pool = await self._auth_pool()
        async with pool.acquire() as conn:
            cands = await conn.fetch(
                """
                SELECT r.id::text AS id, r.tenant_id::text AS tenant_id, r.temp_password_hash, u.id AS user_id,
                       u.email, u.name, u.default_tenant_id, u.role
                  FROM obys_password_reset_requests r JOIN saas_users u ON u.id = r.user_id
                 WHERE lower(u.email) = $1 AND r.status = 'issued' AND r.method = 'admin'
                   AND r.temp_expires_at > now() AND COALESCE(u.status, 'active') = 'active' AND u.deleted_at IS NULL
                 ORDER BY r.issued_at DESC
                """,
                email,
            )
            for cand in cands:
                if not await _check_async(password, cand["temp_password_hash"] or ""):
                    continue
                async with conn.transaction():
                    claimed = await conn.fetchrow(
                        """
                        UPDATE obys_password_reset_requests
                           SET status = 'used', temp_used_at = now(), temp_password_hash = NULL, updated_at = now()
                         WHERE id = $1::uuid AND status = 'issued' AND temp_expires_at > now()
                        RETURNING id
                        """,
                        cand["id"],
                    )
                    if not claimed:
                        return None
                    await conn.execute("UPDATE saas_users SET must_change_password = true WHERE id = $1", cand["user_id"])
                return {
                    "id": cand["user_id"], "email": cand["email"], "name": cand["name"],
                    "tenant_id": str(cand["default_tenant_id"]) if cand["default_tenant_id"] else None,
                    "role": cand["role"], "request_id": cand["id"], "request_tenant_id": cand["tenant_id"],
                }
        return None

    async def set_sms_code(self, request_id: str, code_hash: str, expires_at: datetime) -> None:
        pool = await self._auth_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE obys_password_reset_requests SET sms_code_hash = $2, sms_code_expires_at = $3, "
                "sms_failed_attempts = 0, updated_at = now() WHERE id = $1::uuid AND method = 'sms'",
                request_id, code_hash, expires_at,
            )

    async def verify_sms(self, email: str, code: str) -> Optional[dict]:
        pool = await self._auth_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT r.id::text AS id, r.user_id, r.sms_code_hash, r.sms_code_expires_at, r.sms_failed_attempts
                  FROM obys_password_reset_requests r JOIN saas_users u ON u.id = r.user_id
                 WHERE lower(u.email) = $1 AND r.method = 'sms' AND r.status = 'pending'
                 ORDER BY r.requested_at DESC LIMIT 1
                """,
                email,
            )
            if not row or not row["sms_code_hash"] or row["sms_failed_attempts"] >= SMS_MAX_FAILURES:
                return None
            if row["sms_code_expires_at"] <= _utcnow():
                return None
            if not await _check_async(code, row["sms_code_hash"]):
                await conn.execute(
                    "UPDATE obys_password_reset_requests SET sms_failed_attempts = sms_failed_attempts + 1, updated_at = now() "
                    "WHERE id = $1::uuid", row["id"])
                return None
            claimed = await conn.fetchrow(
                "UPDATE obys_password_reset_requests SET status = 'used', sms_code_hash = NULL, temp_used_at = now(), "
                "updated_at = now() WHERE id = $1::uuid AND status = 'pending' RETURNING id", row["id"])
            return {"user_id": row["user_id"], "request_id": row["id"]} if claimed else None

    async def user_password_hash(self, user_id: str) -> Optional[str]:
        pool = await self._auth_pool()
        async with pool.acquire() as conn:
            return await conn.fetchval("SELECT password_hash FROM saas_users WHERE id = $1", user_id)

    async def apply_new_password(self, user_id: str, password_hash: str, complete_methods: tuple[str, ...]) -> dict:
        pool = await self._auth_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    UPDATE saas_users SET password_hash = $2, must_change_password = false,
                           session_version = session_version + 1, password_changed_at = now()
                     WHERE id = $1 RETURNING session_version
                    """,
                    user_id, password_hash,
                )
                await conn.execute(
                    "UPDATE obys_password_reset_requests SET status = 'completed', completed_at = now(), "
                    "temp_password_hash = NULL, sms_code_hash = NULL, updated_at = now() "
                    "WHERE user_id = $1 AND status IN ('pending', 'issued', 'used') AND method = ANY($2::text[])",
                    user_id, list(complete_methods),
                )
        return {"session_version": int(row["session_version"]) if row else 0}

    async def audit(self, event: str, *, request_id: Optional[str] = None, tenant_id: Optional[str] = None,
                    user_id: Optional[str] = None, actor: Optional[str] = None, ip_hash: str = "") -> None:
        try:
            pool = await self._auth_pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO obys_password_reset_audit (request_id, tenant_id, user_id, event, actor, ip_hash) "
                    "VALUES ($1::uuid, $2::uuid, $3, $4, $5, $6)",
                    request_id, tenant_id, user_id, event, actor, ip_hash,
                )
        except Exception as exc:  # noqa: BLE001 - 감사 기록 실패가 본 흐름을 막지 않는다
            logger.warning("obys recovery: audit insert failed: %s", type(exc).__name__)


def get_store() -> RecoveryStore:
    return PgStore()
