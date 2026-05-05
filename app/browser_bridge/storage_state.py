"""Storage-state persistence boundary for Browser Bridge.

The default implementation writes only under an ignored local state directory and
keeps the envelope API explicit so a real encryption provider can be substituted
without touching browser-session code.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Protocol

from .security import ensure_child_path


class StorageEnvelope(Protocol):
    def protect(self, payload: bytes) -> bytes:
        ...

    def reveal(self, payload: bytes) -> bytes:
        ...


class PlainLocalEnvelope:
    """Local-only envelope used until a KMS/secretbox provider is configured."""

    _HEADER = b"browser-bridge-plain-v1\n"

    def protect(self, payload: bytes) -> bytes:
        return self._HEADER + payload

    def reveal(self, payload: bytes) -> bytes:
        if payload.startswith(self._HEADER):
            return payload[len(self._HEADER):]
        return payload


class StorageStateManager:
    """Manage Playwright storageState JSON without logging cookie/token data."""

    def __init__(self, state_dir: str | Path | None = None, envelope: StorageEnvelope | None = None):
        raw_dir = state_dir or os.environ.get("AADS_BROWSER_BRIDGE_STATE_DIR") or ".browser_bridge_state"
        self.state_dir = Path(raw_dir)
        self.storage_dir = self.state_dir / "storage_states"
        self.envelope = envelope or PlainLocalEnvelope()

    def _path_for_ref(self, ref: str) -> Path:
        name = ref if ref.endswith(".json") else f"{ref}.json"
        return ensure_child_path(self.storage_dir, self.storage_dir / name)

    def save(self, session_id: str, storage_state: dict[str, Any]) -> str:
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        ref = session_id
        path = self._path_for_ref(ref)
        payload = json.dumps(storage_state, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        path.write_bytes(self.envelope.protect(payload))
        try:
            path.chmod(0o600)
        except OSError:
            pass
        return ref

    def load(self, ref: str) -> dict[str, Any]:
        path = self._path_for_ref(ref)
        payload = self.envelope.reveal(path.read_bytes())
        data = json.loads(payload.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("storage state must be a JSON object")
        return data

    def path_for_playwright(self, ref: str) -> str:
        return str(self._path_for_ref(ref))
