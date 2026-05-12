"""Browser Bridge pairing and session registries."""
from __future__ import annotations

import json
import os
import secrets
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional

from .models import (
    BrowserBridgeSession,
    BrowserEndpoint,
    BrowserEndpointKind,
    PairingCreated,
    PairingTokenRecord,
    utcnow,
)
from .security import constant_time_equals, ensure_child_path, hash_secret, validate_bridge_endpoint


class PairingManager:
    """One-time pairing token manager.

    The raw token is returned only from create_pairing and never stored.
    """

    def __init__(self, default_ttl_seconds: int = 600, state_dir: str | Path | None = None):
        self.default_ttl_seconds = default_ttl_seconds
        raw_dir = state_dir or os.environ.get("AADS_BROWSER_BRIDGE_STATE_DIR") or ".browser_bridge_state"
        self.state_dir = Path(raw_dir)
        self.state_file = ensure_child_path(self.state_dir, self.state_dir / "pairings.json")
        self._records: dict[str, PairingTokenRecord] = {}
        self._lock = threading.RLock()
        self._load()

    def _parse_dt(self, value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None

    def _serialize(self, record: PairingTokenRecord) -> dict:
        return {
            "pairing_id": record.pairing_id,
            "token_hash": record.token_hash,
            "label": record.label,
            "created_by": record.created_by,
            "created_at": record.created_at.isoformat(),
            "expires_at": record.expires_at.isoformat(),
            "consumed_at": record.consumed_at.isoformat() if record.consumed_at else None,
        }

    def _deserialize(self, payload: dict) -> PairingTokenRecord | None:
        created_at = self._parse_dt(payload.get("created_at"))
        expires_at = self._parse_dt(payload.get("expires_at"))
        pairing_id = str(payload.get("pairing_id") or "")
        token_hash = str(payload.get("token_hash") or "")
        if not pairing_id or not token_hash or not created_at or not expires_at:
            return None
        return PairingTokenRecord(
            pairing_id=pairing_id,
            token_hash=token_hash,
            label=str(payload.get("label") or ""),
            created_by=str(payload.get("created_by") or ""),
            created_at=created_at,
            expires_at=expires_at,
            consumed_at=self._parse_dt(payload.get("consumed_at")),
        )

    def _load(self) -> None:
        if not self.state_file.exists():
            return
        try:
            raw = json.loads(self.state_file.read_text(encoding="utf-8"))
            items = raw.get("pairings", []) if isinstance(raw, dict) else []
            for item in items:
                if not isinstance(item, dict):
                    continue
                record = self._deserialize(item)
                if record and not record.is_expired and not record.is_consumed:
                    self._records[record.pairing_id] = record
        except Exception:
            self._records = {}

    def _save_locked(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        records_to_store = [
            record for record in self._records.values()
            if not record.is_expired and not record.is_consumed
        ]
        payload = {
            "version": 1,
            "saved_at": utcnow().isoformat(),
            "pairings": [self._serialize(record) for record in records_to_store],
        }
        tmp = self.state_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.state_file)
        try:
            self.state_file.chmod(0o600)
        except OSError:
            pass

    def create_pairing(
        self,
        label: str = "",
        created_by: str = "",
        ttl_seconds: Optional[int] = None,
    ) -> PairingCreated:
        token = secrets.token_urlsafe(32)
        now = utcnow()
        expires_at = now + timedelta(seconds=ttl_seconds or self.default_ttl_seconds)
        record = PairingTokenRecord(
            pairing_id=f"pair-{uuid.uuid4().hex[:12]}",
            token_hash=hash_secret(token),
            label=label,
            created_by=created_by,
            created_at=now,
            expires_at=expires_at,
        )
        with self._lock:
            self._records[record.pairing_id] = record
            self._save_locked()
        return PairingCreated(record.pairing_id, token, expires_at)

    def consume(self, token: str) -> PairingTokenRecord:
        token_hash = hash_secret(token)
        with self._lock:
            for record in self._records.values():
                if not constant_time_equals(record.token_hash, token_hash):
                    continue
                if record.is_consumed:
                    raise ValueError("pairing token already used")
                if record.is_expired:
                    raise ValueError("pairing token expired")
                record.consumed_at = utcnow()
                self._save_locked()
                return record
        raise ValueError("pairing token invalid")


class SessionRegistry:
    """Process-local registry backed by a small JSON state file.

    Browser Bridge sessions are operational state, not secrets. Persisting the
    endpoint metadata prevents a deploy/restart from dropping all CDP/local-agent
    registrations, while the in-process lock keeps current tool calls cheap.
    """

    def __init__(self, state_dir: str | Path | None = None):
        raw_dir = state_dir or os.environ.get("AADS_BROWSER_BRIDGE_STATE_DIR") or ".browser_bridge_state"
        self.state_dir = Path(raw_dir)
        self.state_file = ensure_child_path(self.state_dir, self.state_dir / "sessions.json")
        self._sessions: dict[str, BrowserBridgeSession] = {}
        self._lock = threading.RLock()
        self._load()

    def _serialize(self, session: BrowserBridgeSession) -> dict:
        return {
            "session_id": session.session_id,
            "label": session.label,
            "endpoint": session.endpoint.public_dict(),
            "registered_at": session.registered_at.isoformat(),
            "pairing_id": session.pairing_id,
            "storage_state_ref": session.storage_state_ref,
            "created_by": session.created_by,
            "active": session.active,
            "expires_at": session.expires_at.isoformat() if session.expires_at else None,
            "last_used_at": session.last_used_at.isoformat() if session.last_used_at else None,
            "lease_owner": session.lease_owner,
            "lease_expires_at": session.lease_expires_at.isoformat() if session.lease_expires_at else None,
        }

    def _parse_dt(self, value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed

    def _deserialize(self, payload: dict) -> BrowserBridgeSession:
        endpoint_payload = payload.get("endpoint") or {}
        endpoint = BrowserEndpoint(
            kind=BrowserEndpointKind(str(endpoint_payload.get("kind") or "headless")),
            url=endpoint_payload.get("url"),
            browser_name=str(endpoint_payload.get("browser_name") or "chromium"),
            metadata=endpoint_payload.get("metadata") or {},
        )
        return BrowserBridgeSession(
            session_id=str(payload.get("session_id") or new_session_id()),
            label=str(payload.get("label") or "Browser Bridge Session"),
            endpoint=endpoint,
            registered_at=self._parse_dt(payload.get("registered_at")) or utcnow(),
            pairing_id=str(payload.get("pairing_id") or ""),
            storage_state_ref=payload.get("storage_state_ref"),
            created_by=str(payload.get("created_by") or ""),
            active=bool(payload.get("active")),
            expires_at=self._parse_dt(payload.get("expires_at")),
            last_used_at=self._parse_dt(payload.get("last_used_at")),
            lease_owner=str(payload.get("lease_owner") or ""),
            lease_expires_at=self._parse_dt(payload.get("lease_expires_at")),
        )

    def _load(self) -> None:
        if not self.state_file.exists():
            return
        try:
            raw = json.loads(self.state_file.read_text(encoding="utf-8"))
            items = raw.get("sessions", []) if isinstance(raw, dict) else []
            for item in items:
                if not isinstance(item, dict):
                    continue
                session = self._deserialize(item)
                if not session.is_expired:
                    self._sessions[session.session_id] = session
        except Exception:
            self._sessions = {}

    def _save_locked(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "saved_at": utcnow().isoformat(),
            "sessions": [self._serialize(session) for session in self._sessions.values()],
        }
        tmp = self.state_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.state_file)
        try:
            self.state_file.chmod(0o600)
        except OSError:
            pass

    def register(self, session: BrowserBridgeSession, *, activate: bool = True) -> BrowserBridgeSession:
        validate_bridge_endpoint(session.endpoint.kind, session.endpoint.url)
        with self._lock:
            if activate:
                for existing in self._sessions.values():
                    existing.active = False
                session.active = True
            self._sessions[session.session_id] = session
            self._save_locked()
        return session

    def list_sessions(self, include_expired: bool = False) -> list[BrowserBridgeSession]:
        with self._lock:
            sessions = list(self._sessions.values())
        if include_expired:
            return sessions
        return [session for session in sessions if not session.is_expired]

    def get(self, session_id: str) -> Optional[BrowserBridgeSession]:
        with self._lock:
            session = self._sessions.get(session_id)
        if session and not session.is_expired:
            return session
        return None

    def get_active(self) -> Optional[BrowserBridgeSession]:
        with self._lock:
            sessions = list(self._sessions.values())
        for session in sessions:
            if session.active and not session.is_expired:
                return session
        return None

    def select(self, session_id: str) -> BrowserBridgeSession:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None or session.is_expired:
                raise ValueError(f"browser bridge session not found: {session_id}")
            for existing in self._sessions.values():
                existing.active = False
            session.active = True
            self._save_locked()
            return session

    def touch(self, session: BrowserBridgeSession) -> None:
        with self._lock:
            if session.session_id in self._sessions:
                self._sessions[session.session_id] = session
                self._save_locked()

    def find_by_metadata(self, **criteria: str) -> Optional[BrowserBridgeSession]:
        with self._lock:
            sessions = list(self._sessions.values())
        for session in sessions:
            if session.is_expired:
                continue
            metadata = dict(session.endpoint.metadata or {})
            if all(str(metadata.get(key) or "") == str(value) for key, value in criteria.items()):
                return session
        return None

    def acquire_lease(
        self,
        *,
        owner: str,
        preferred_session_id: str = "",
        ttl_seconds: int = 300,
    ) -> BrowserBridgeSession:
        owner = (owner or "").strip()
        if not owner:
            raise ValueError("lease owner required")
        now = utcnow()
        expires_at = now + timedelta(seconds=max(30, int(ttl_seconds or 300)))
        with self._lock:
            for session in self._sessions.values():
                if session.lease_expires_at and session.lease_expires_at <= now:
                    session.lease_owner = ""
                    session.lease_expires_at = None

            candidates = [
                session for session in self._sessions.values()
                if not session.is_expired
                and (not preferred_session_id or session.session_id == preferred_session_id)
                and (not session.lease_owner or session.lease_owner == owner)
            ]
            if not candidates:
                raise ValueError("available browser bridge session not found")
            session = candidates[0]
            session.lease_owner = owner
            session.lease_expires_at = expires_at
            self._save_locked()
            return session

    def release_lease(self, *, owner: str = "", session_id: str = "") -> int:
        released = 0
        with self._lock:
            for session in self._sessions.values():
                if session_id and session.session_id != session_id:
                    continue
                if owner and session.lease_owner != owner:
                    continue
                if session.lease_owner:
                    session.lease_owner = ""
                    session.lease_expires_at = None
                    released += 1
            if released:
                self._save_locked()
        return released

    def clear(self) -> None:
        with self._lock:
            self._sessions.clear()
            self._save_locked()

    def public_sessions(self, include_expired: bool = False) -> Iterable[dict]:
        return [session.public_dict() for session in self.list_sessions(include_expired=include_expired)]


def new_session_id() -> str:
    return f"bb-{uuid.uuid4().hex[:12]}"
