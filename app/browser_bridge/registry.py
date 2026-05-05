"""In-memory Browser Bridge pairing and session registries."""
from __future__ import annotations

import secrets
import threading
import uuid
from datetime import timedelta
from typing import Iterable, Optional

from .models import (
    BrowserBridgeSession,
    BrowserEndpoint,
    PairingCreated,
    PairingTokenRecord,
    utcnow,
)
from .security import constant_time_equals, hash_secret, validate_bridge_endpoint


class PairingManager:
    """One-time pairing token manager.

    The raw token is returned only from create_pairing and never stored.
    """

    def __init__(self, default_ttl_seconds: int = 600):
        self.default_ttl_seconds = default_ttl_seconds
        self._records: dict[str, PairingTokenRecord] = {}
        self._lock = threading.RLock()

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
                return record
        raise ValueError("pairing token invalid")


class SessionRegistry:
    """Local process registry for browser bridge sessions."""

    def __init__(self):
        self._sessions: dict[str, BrowserBridgeSession] = {}
        self._lock = threading.RLock()

    def register(self, session: BrowserBridgeSession, *, activate: bool = True) -> BrowserBridgeSession:
        validate_bridge_endpoint(session.endpoint.kind, session.endpoint.url)
        with self._lock:
            if activate:
                for existing in self._sessions.values():
                    existing.active = False
                session.active = True
            self._sessions[session.session_id] = session
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
            return session

    def clear(self) -> None:
        with self._lock:
            self._sessions.clear()

    def public_sessions(self, include_expired: bool = False) -> Iterable[dict]:
        return [session.public_dict() for session in self.list_sessions(include_expired=include_expired)]


def new_session_id() -> str:
    return f"bb-{uuid.uuid4().hex[:12]}"
