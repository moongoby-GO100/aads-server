"""Secret-safe Claude CLI OAuth credential health helpers.

The Claude CLI owns the access/refresh token values.  AADS only reads metadata
needed for routing and AUTH-001, and persists a redacted validation outcome.
"""
import json
import os
import re
import tempfile
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


def classify_auth_error(value: Any) -> str:
    """Classify an auth failure without retaining provider error text."""
    lowered = str(value or "").lower()
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
    _atomic_write_json(validation_path_for_credentials(Path(credentials_path)), payload)


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
    try:
        stored = json.loads(
            validation_path_for_credentials(credentials_path).read_text(encoding="utf-8")
        )
        checked_epoch = float(stored.get("checked_at"))
        validation_status = str(stored.get("status") or "error")
        age_seconds = max(0, int(now - checked_epoch))
        validation = {
            "status": validation_status,
            "checked_at": _iso_timestamp(checked_epoch),
            "age_seconds": age_seconds,
            "recent": age_seconds <= max_validation_age_seconds,
        }
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        pass

    source = "slot_credentials" if exists else (
        "env_fallback" if env_fallback_available else "none"
    )
    auth_available = complete or (not exists and env_fallback_available)
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
