"""Managed browser profile helpers for PC Agent work sessions."""
from __future__ import annotations

import asyncio
import contextlib
import socket
import hashlib
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

PROFILE_ROOT = Path("/root/aads/runtime/managed-browser-profiles")
EGRESS_POLICIES = frozenset({"direct", "cafe24", "auto"})
# A 403 is not evidence of regional blocking.  Only explicitly approved
# domains may select the deployment-owned Cafe24 tunnel in auto mode.
CAFE24_AUTO_DOMAINS = frozenset({"store.coupangeats.com"})
_LOOPBACK_PROXY_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def normalize_work_key(work_key: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_.-]+", "-", (work_key or "").strip()).strip("-")
    if not cleaned:
        raise ValueError("work_key_required")
    return cleaned[:120]


def normalize_origin(url: str) -> str:
    parsed = urlparse((url or "").strip())
    if not parsed.scheme or not parsed.netloc:
        return ""
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"


def profile_key(work_key: str, target_url: str = "") -> str:
    normalized = normalize_work_key(work_key)
    origin = normalize_origin(target_url) or (target_url or "").strip()
    return f"{normalized}:{origin}"


def profile_info(work_key: str, target_url: str = "") -> dict[str, Any]:
    normalized = normalize_work_key(work_key)
    origin = normalize_origin(target_url)
    digest = hashlib.sha256(profile_key(normalized, target_url).encode("utf-8")).hexdigest()[:12]
    profile_dir = PROFILE_ROOT / f"{normalized}-{digest}"
    return {
        "work_key": normalized,
        "target_url": target_url,
        "origin": origin,
        "profile_key": profile_key(normalized, target_url),
        "profile_dir": str(profile_dir),
        "isolated_profile": True,
    }


def normalize_egress_policy(value: str | None) -> str:
    """Accept named policies only; callers cannot provide a proxy endpoint."""
    policy = str(value or "direct").strip().lower()
    if policy not in EGRESS_POLICIES:
        raise ValueError("unsupported_egress_policy")
    return policy


def _cafe24_proxy() -> tuple[dict[str, str] | None, str]:
    """Return only an approved local SSH-tunnel listener, never credentials."""
    vault_ref = str(os.getenv("CAFE24_EGRESS_PROXY_VAULT_REF") or "").strip()
    raw_url = str(os.getenv("CAFE24_EGRESS_PROXY_URL") or "").strip()
    if not raw_url:
        return {"server": "managed-ssh://server-114"}, "cafe24_ssh_tunnel_pending"
    if not vault_ref:
        return None, "cafe24_vault_reference_missing"
    try:
        parsed = urlparse(raw_url)
        port = parsed.port
    except ValueError:
        return None, "cafe24_tunnel_endpoint_invalid"
    if (
        parsed.scheme.lower() not in {"http", "https", "socks5"}
        or parsed.hostname not in _LOOPBACK_PROXY_HOSTS
        or not port
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        return None, "cafe24_tunnel_endpoint_invalid"
    return {"server": raw_url.rstrip("/")}, "cafe24_loopback_tunnel_ready"


def egress_for_target(requested: str | None, target_url: str) -> dict[str, Any]:
    """Resolve egress without silently falling back from requested Cafe24."""
    policy = normalize_egress_policy(requested)
    hostname = (urlparse(str(target_url or "")).hostname or "").lower()
    use_cafe24 = policy == "cafe24" or (policy == "auto" and hostname in CAFE24_AUTO_DOMAINS)
    if not use_cafe24:
        reason = "direct_requested" if policy == "direct" else "auto_direct_no_domain_policy"
        return {
            "egress_requested": policy,
            "egress_effective": "direct",
            "egress_status": "direct",
            "egress_reason": reason,
            "proxy": None,
        }
    proxy, reason = _cafe24_proxy()
    if proxy is None:
        return {
            "egress_requested": policy,
            "egress_effective": "unavailable",
            "egress_status": "unavailable",
            "egress_reason": reason,
            "proxy": None,
        }
    return {
        "egress_requested": policy,
        "egress_effective": "cafe24",
        "egress_status": "configured",
        "egress_reason": reason if policy == "cafe24" else "auto_domain_policy:cafe24",
        "proxy": proxy,
    }


@contextlib.asynccontextmanager
async def browser_egress(egress: dict[str, Any]):
    """Own an authenticated loopback SSH SOCKS listener for this browser only.

    Fixed deployment alias and mounted SSH identity; no user-supplied hosts/keys.
    Cancellation and browser errors always close the listener. A tunnel failure
    never yields a direct connection.
    """
    if egress["egress_effective"] == "unavailable":
        raise RuntimeError(egress["egress_reason"])
    proxy = egress.get("proxy")
    if not proxy or proxy.get("server") != "managed-ssh://server-114":
        yield proxy
        return
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    process = await asyncio.create_subprocess_exec(
        "ssh", "-N", "-T", "-D", f"127.0.0.1:{port}",
        "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
        "-o", "ExitOnForwardFailure=yes", "-o", "StrictHostKeyChecking=yes",
        "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=2",
        "server-114", stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        async with asyncio.timeout(10):
            while True:
                if process.returncode is not None:
                    raise RuntimeError("cafe24_ssh_tunnel_failed")
                try:
                    reader, writer = await asyncio.open_connection("127.0.0.1", port)
                    try:
                        writer.write(b"\x05\x01\x00")
                        await writer.drain()
                        reply = await asyncio.wait_for(reader.readexactly(2), timeout=1)
                        if reply != b"\x05\x00":
                            raise RuntimeError("cafe24_invalid_socks_listener")
                    finally:
                        writer.close()
                        await writer.wait_closed()
                    break
                except OSError:
                    await asyncio.sleep(0.1)
        yield {"server": f"socks5://127.0.0.1:{port}"}
    finally:
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=3)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                await process.wait()
