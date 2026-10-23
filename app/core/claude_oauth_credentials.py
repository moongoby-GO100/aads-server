"""Secret-safe Claude CLI OAuth credential health helpers.

The Claude CLI owns the access/refresh token values.  AADS only reads metadata
needed for routing and AUTH-001, and persists a redacted validation outcome.
"""
import contextlib
import fcntl
import json
import os
import re
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional


_TOKEN_PATTERNS = (
    re.compile(r"\bsk-ant-(?:oat|api)[A-Za-z0-9_-]*-[A-Za-z0-9_-]{8,}\b", re.IGNORECASE),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{12,}", re.IGNORECASE),
    re.compile(
        r"(?P<prefix>[\"']?(?:accessToken|refreshToken|oauthToken|apiKey)[\"']?\s*[:=]\s*[\"']?)"
        r"(?P<secret>[^\"'\s,}]{8,})",
        re.IGNORECASE,
    ),
)


def redact_secret_text(value: Any) -> str:
    """Return diagnostic text with common credential forms removed."""
    text = str(value or "")
    text = _TOKEN_PATTERNS[0].sub("[REDACTED]", text)
    text = _TOKEN_PATTERNS[1].sub("Bearer [REDACTED]", text)
    text = _TOKEN_PATTERNS[2].sub(
        lambda match: match.group("prefix") + "[REDACTED]",
        text,
    )
    return text


# 구독이 해지됐거나 조직이 Claude Code 구독 접근을 막은 경우. 토큰은 멀쩡해서
# 인증 오류가 아니지만, 이 슬롯으로는 어떤 호출도 성공하지 못한다.
_SUBSCRIPTION_INACTIVE_MARKERS = (
    "subscription_inactive",
    "disabled claude subscription access",
    "organization has disabled",
    "claude subscription access",
)

# 구독 보류는 이 시간 안에 재확인되지 않으면 풀린다. 재확인 주기(30분)의 두 배다.
SUBSCRIPTION_RECHECK_SEC = 1800
SUBSCRIPTION_HOLD_SEC = 2 * SUBSCRIPTION_RECHECK_SEC
SUBSCRIPTION_INACTIVE = "subscription_inactive"
INACTIVE_SUBSCRIPTION_STATES = frozenset({"canceled", "free"})


def is_subscription_inactive_text(value: Any) -> bool:
    """CLI 가 내는 '구독 접근 차단' 문장인가. 짧은 안내문만 대상으로 한다."""
    text = str(value or "").strip().lower()
    if not text or len(text) > 400:
        return False
    return any(marker in text for marker in _SUBSCRIPTION_INACTIVE_MARKERS)


def is_slot_busy_text(value: Any) -> bool:
    """A call yielded to an in-progress credential renewal (not an auth failure)."""
    return "oauth_slot_busy" in str(value or "").lower()


def classify_auth_error(value: Any) -> str:
    """Classify an auth failure without retaining provider error text."""
    lowered = str(value or "").lower()
    if "oauth_slot_busy" in lowered:
        return "error"
    if any(marker in lowered for marker in _SUBSCRIPTION_INACTIVE_MARKERS):
        return SUBSCRIPTION_INACTIVE
    if "revoked" in lowered or "reused" in lowered:
        return "revoked"
    if "expired" in lowered:
        return "expired"
    if "401" in lowered or "unauthorized" in lowered:
        return "unauthorized"
    if "authenticate" in lowered or "authentication" in lowered or "log in again" in lowered:
        return "authentication_failed"
    return "error"


def validation_path_for_credentials(credentials_path: Path) -> Path:
    return Path(credentials_path).with_name(".oauth-validation.json")


_SIDECAR_THREAD_LOCK = threading.Lock()


@contextlib.contextmanager
def _sidecar_lock(credentials_path: Path):
    """사이드카 읽기-수정-쓰기를 직렬화한다 (스레드 + 프로세스).

    atomic replace 는 파일이 깨지는 것만 막고, 두 쓰기 중 하나가 다른 쪽
    결과를 읽지 못한 채 덮어쓰는 갱신 유실은 막지 못한다.
    """
    lock_path = validation_path_for_credentials(Path(credentials_path)).with_name(
        ".oauth-validation.lock"
    )
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with _SIDECAR_THREAD_LOCK:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)


def _atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    """Write a non-secret JSON state file with same-filesystem atomic replace."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, str(path))
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


def record_slot_validation(
    credentials_path: Path,
    *,
    ok: bool,
    error: Any = "",
    checked_at: Optional[float] = None,
) -> None:
    """Persist only a validation class and timestamp beside the credential."""
    payload = {
        "checked_at": float(checked_at if checked_at is not None else time.time()),
        "status": "ok" if ok else classify_auth_error(error),
    }
    with _sidecar_lock(Path(credentials_path)):
        subscription = _read_validation_file(Path(credentials_path)).get("subscription")
        if isinstance(subscription, dict):
            payload["subscription"] = subscription
        _atomic_write_json(validation_path_for_credentials(Path(credentials_path)), payload)


def _read_validation_file(credentials_path: Path) -> Dict[str, Any]:
    try:
        stored = json.loads(
            validation_path_for_credentials(Path(credentials_path)).read_text(encoding="utf-8")
        )
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    return stored if isinstance(stored, dict) else {}


def classify_subscription_profile(profile: Any) -> str:
    """oauth/profile 응답을 active / canceled / free / unknown 으로 접는다.

    해지(canceled)나 무료(claude_free)라는 **명시적 증거**가 있을 때만 비활성으로
    본다. billing_type=none 만으로는 판단하지 않는다 — 조직 계약형 계정도 그렇게
    보일 수 있어서, 멀쩡한 슬롯을 우리 손으로 멈추는 일이 없어야 한다.
    """
    if not isinstance(profile, dict):
        return "unknown"
    merged: Dict[str, Any] = {}
    for part in (profile, profile.get("account"), profile.get("organization")):
        if isinstance(part, dict):
            merged.update(part)
    sub_status = str(merged.get("subscription_status") or "").lower()
    org_type = str(merged.get("organization_type") or "").lower()
    if sub_status in {"canceled", "cancelled"}:
        return "canceled"
    if "free" in org_type:
        return "free"
    if sub_status in {"active", "trialing", "past_due"} or merged.get("has_claude_max") \
            or merged.get("has_claude_pro"):
        return "active"
    return "unknown"


# 구독 보류가 덮어쓰면 안 되는 상태. 해지 확인이 이 인증 실패를 지우면, 이후
# 구독이 돌아왔을 때 멀쩡한 슬롯처럼 되살아난다.
_TERMINAL_AUTH_STATUSES = frozenset({"revoked", "unauthorized", "authentication_failed"})


def record_subscription_profile(
    credentials_path: Path,
    profile: Any,
    *,
    checked_at: Optional[float] = None,
    observed_at: Optional[float] = None,
) -> str:
    """읽기 전용 프로필 확인 결과를 사이드카에 남기고 구독 상태를 돌려준다.

    비활성이면 슬롯 검증 상태를 subscription_inactive 로 갱신해(= 보류 연장)
    선택 경로가 건너뛰게 하고, 활성으로 돌아왔으면 그 보류만 푼다. 알 수 없으면
    아무것도 바꾸지 않는다. 계정 켜기/끄기는 건드리지 않는다.

    observed_at 은 프로필 요청을 보내기 시작한 시각이다. 그 뒤에 기록된 보류
    (CLI 가 실제로 차단 문장을 받은 것)는 더 새로운 증거이므로 낡은 active
    응답이 지우지 못한다. 기존 revoked/authentication_failed 는 해지 확인이
    덮어쓰지 않는다.
    """
    credentials_path = Path(credentials_path)
    now = float(checked_at if checked_at is not None else time.time())
    observed = float(observed_at if observed_at is not None else now)
    state = classify_subscription_profile(profile)
    with _sidecar_lock(credentials_path):
        stored = _read_validation_file(credentials_path)
        payload: Dict[str, Any] = {
            "checked_at": float(stored.get("checked_at") or now),
            "status": str(stored.get("status") or "never"),
        }
        if state == "unknown":
            previous = stored.get("subscription")
            if isinstance(previous, dict):
                payload["subscription"] = {**previous, "last_attempt_at": now}
                _atomic_write_json(validation_path_for_credentials(credentials_path), payload)
            return state
        if state in INACTIVE_SUBSCRIPTION_STATES:
            if payload["status"] not in _TERMINAL_AUTH_STATUSES:
                payload["status"] = SUBSCRIPTION_INACTIVE
                payload["checked_at"] = now
        elif payload["status"] == SUBSCRIPTION_INACTIVE and payload["checked_at"] <= observed:
            payload["status"] = "ok"
            payload["checked_at"] = now
        payload["subscription"] = {"status": state, "checked_at": now}
        _atomic_write_json(validation_path_for_credentials(credentials_path), payload)
    return state


def read_access_token(credentials_path: Path) -> str:
    """프로필 확인 호출 직전에만 쓴다. 반환값을 로그·응답·커밋에 남기지 않는다."""
    try:
        record = _oauth_record(json.loads(Path(credentials_path).read_text(encoding="utf-8")))
    except (OSError, ValueError, json.JSONDecodeError):
        return ""
    return str(record.get("accessToken") or "")


def _oauth_record(payload: Any) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    nested = payload.get("claudeAiOauth")
    return nested if isinstance(nested, dict) else payload


def _expiry_epoch(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        numeric = float(value)
        if numeric > 100_000_000_000:
            numeric /= 1000.0
        return numeric
    except (TypeError, ValueError):
        pass
    try:
        text = str(value).strip()
        parsed = None
        for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
            try:
                parsed = datetime.strptime(text.replace("Z", "+0000").replace("+00:00", "+0000"), fmt)
                break
            except ValueError:
                continue
        if parsed is None:
            return None
        return parsed.timestamp()
    except (TypeError, ValueError):
        return None


def _iso_timestamp(epoch: Optional[float]) -> Optional[str]:
    if epoch is None:
        return None
    try:
        return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError):
        return None


def read_slot_auth_status(
    credentials_path: Path,
    *,
    env_fallback_available: bool = False,
    now_epoch: Optional[float] = None,
    max_validation_age_seconds: int = 86_400,
    subscription_hold_seconds: int = SUBSCRIPTION_HOLD_SEC,
) -> Dict[str, Any]:
    """Read safe metadata for relay health and AUTH-001.

    Token values are converted to booleans immediately and never returned.
    An existing but incomplete credential does not silently fall back to the
    environment token; that would hide a broken refresh path.
    """
    credentials_path = Path(credentials_path)
    now = float(now_epoch if now_epoch is not None else time.time())
    exists = credentials_path.is_file()
    parse_error = False
    oauth: Dict[str, Any] = {}
    mtime_epoch: Optional[float] = None
    if exists:
        try:
            oauth = _oauth_record(json.loads(credentials_path.read_text(encoding="utf-8")))
            mtime_epoch = credentials_path.stat().st_mtime
        except (OSError, ValueError, json.JSONDecodeError):
            parse_error = True

    access_present = bool(oauth.get("accessToken"))
    refresh_present = bool(oauth.get("refreshToken"))
    expires_epoch = _expiry_epoch(oauth.get("expiresAt"))
    expired = expires_epoch is not None and expires_epoch <= now
    complete = exists and not parse_error and access_present and refresh_present

    validation: Dict[str, Any] = {
        "status": "never",
        "checked_at": None,
        "age_seconds": None,
        "recent": False,
    }
    subscription: Dict[str, Any] = {"status": None, "checked_at": None}
    stored = _read_validation_file(credentials_path)
    stored_subscription = stored.get("subscription")
    if isinstance(stored_subscription, dict):
        try:
            sub_epoch = float(stored_subscription.get("checked_at"))
        except (TypeError, ValueError):
            sub_epoch = None
        subscription = {
            "status": str(stored_subscription.get("status") or "unknown"),
            "checked_at": _iso_timestamp(sub_epoch),
        }
    try:
        checked_epoch = float(stored.get("checked_at"))
        validation_status = str(stored.get("status") or "error")
        age_seconds = max(0, int(now - checked_epoch))
        validation = {
            "status": validation_status,
            "checked_at": _iso_timestamp(checked_epoch),
            "age_seconds": age_seconds,
            "recent": age_seconds <= max_validation_age_seconds,
        }
    except (TypeError, ValueError):
        pass

    subscription_hold = (
        validation["status"] == SUBSCRIPTION_INACTIVE
        and validation["age_seconds"] is not None
        and validation["age_seconds"] <= subscription_hold_seconds
    )

    source = "slot_credentials" if exists else (
        "env_fallback" if env_fallback_available else "none"
    )
    auth_available = (complete or (not exists and env_fallback_available)) and not subscription_hold
    validation_status = validation["status"]
    if parse_error:
        status = "malformed_credentials"
    elif exists and not access_present:
        status = "missing_access_token"
    elif exists and not refresh_present:
        status = "missing_refresh_token"
    elif not exists and env_fallback_available:
        status = "env_fallback_unverified"
    elif not exists:
        status = "missing_credentials"
    elif validation_status == "revoked":
        status = "revoked"
    elif subscription_hold:
        status = SUBSCRIPTION_INACTIVE
    elif validation_status in {"unauthorized", "authentication_failed"}:
        status = "authentication_failed"
    elif expired:
        status = "expired_refreshable"
    elif expires_epoch is None:
        status = "expiration_unknown"
    elif validation_status == "never":
        status = "unverified"
    elif not validation["recent"]:
        status = "validation_stale"
    elif validation_status != "ok":
        status = "validation_failed"
    else:
        status = "ready"

    return {
        "source": source,
        "status": status,
        "healthy": status == "ready",
        "auth_available": auth_available,
        "credential_file_present": exists,
        "access_token_present": access_present,
        "refresh_token_present": refresh_present,
        "refresh_capable": complete,
        "expired": expired,
        "expires_at": _iso_timestamp(expires_epoch),
        "mtime": _iso_timestamp(mtime_epoch),
        "validation": validation,
        "subscription": subscription,
        "subscription_hold": subscription_hold,
    }


def format_auth001_slot(slot: str, status: Dict[str, Any]) -> str:
    """Render one secret-free AUTH-001 line."""
    state = str(status.get("status") or "unknown")
    icon = "✅" if status.get("healthy") else ("❌" if state in {
        "revoked", "authentication_failed", "malformed_credentials",
        "missing_access_token", "missing_refresh_token", "missing_credentials",
    } else "⚠️")
    refresh = "yes" if status.get("refresh_token_present") else "no"
    expires_at = status.get("expires_at") or "unknown"
    validation = status.get("validation") or {}
    validation_state = validation.get("status") or "never"
    return (
        "CLAUDE_SLOT_%s: %s%s source=%s expires_at=%s refresh=%s validation=%s"
        % (slot, icon, state, status.get("source") or "none", expires_at, refresh, validation_state)
    )
