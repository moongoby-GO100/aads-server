"""Single-file HTML import into the mockup asset store (upload, allow-listed own URL, chat upload).

Stored at <mockup_assets root>/<PROJECT>/html/<sha256[:16]>/index.html so the same bytes always land on the same URI.
The completeness/hash gates in mockup_review_service are untouched: a page that still points at external or
sibling files is rejected here instead of being stored.
"""
from __future__ import annotations

import asyncio
import html
import http.client
import ipaddress
import os
import re
import socket
import ssl
import time
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from app.api.canonical_documents import SECRET
from app.services import mockup_review_service as svc

DEFAULT_HOSTS = ("aads.newtalk.kr", "v2.newtalk.kr")
FETCH_TIMEOUT = 20.0
MAX_URL_LENGTH = 2048
MAX_REPORTED_REFS = 20
HTML_SUFFIXES = (".html", ".htm")
_HOSTNAME = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_CHUNK = 64 * 1024


# Not "Accept: text/html": Cloudflare injects its analytics beacon (an external script) only into answers to that
# header, so the page we stored would differ from the source and fail the external-reference check.
_REQUEST_HEADERS = {"Accept": "*/*", "Accept-Encoding": "identity", "User-Agent": "AADS-MockupHtmlImport/1"}


class _TooLarge(Exception):
    pass


def allowed_hosts() -> frozenset[str]:
    extra = {h.strip().lower() for h in os.getenv("MOCKUP_HTML_IMPORT_HOSTS", "").split(",")}
    return frozenset(set(DEFAULT_HOSTS) | {h for h in extra if _HOSTNAME.fullmatch(h)})


def chat_files_root() -> Path:
    return Path(os.getenv("CHAT_FILES_DIR", "/root/aads/uploads/chat/files"))


def extract_title(text: str) -> str:
    match = _TITLE.search(text[:200_000])
    return re.sub(r"\s+", " ", html.unescape(match.group(1))).strip()[:200] if match else ""


# ------------------------------------------------------------------ safe fetch

def validate_source_url(url: str) -> tuple[str, int, str]:
    """-> (host, port, request target). https only, allow-listed host, no credentials, default port."""
    url = (url or "").strip()
    if not url or len(url) > MAX_URL_LENGTH or re.search(r"[\s\x00-\x1f\x7f\\]", url):
        svc.fail(422, "source_url_invalid", message="source_url 이 올바른 https 주소가 아닙니다")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        svc.fail(422, "source_url_invalid", message="source_url 이 올바른 https 주소가 아닙니다")
    if parts.scheme != "https" or not parts.hostname or parts.username is not None or parts.password is not None:
        svc.fail(422, "source_url_invalid", message="https 주소만 가져올 수 있습니다(계정 정보 포함 불가)")
    if port not in (None, 443):
        svc.fail(422, "source_url_invalid", message="기본 포트(443)만 사용할 수 있습니다")
    host = parts.hostname.lower()
    if host not in allowed_hosts():
        svc.fail(422, "source_host_not_allowed", host=host, allowed=sorted(allowed_hosts()),
                 message=f"허용되지 않은 주소입니다({host}). 허용 호스트: {', '.join(sorted(allowed_hosts()))}")
    target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    return host, 443, target


def resolve_public_ip(host: str, port: int) -> str:
    """Resolve and require every answer to be a public address; the returned IP is what we connect to."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        svc.fail(502, "source_fetch_failed", message=f"{host} 주소를 찾을 수 없습니다")
    addresses = []
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        addresses.append(ip)
    if not addresses or not all(ip.is_global for ip in addresses):
        svc.fail(422, "source_resolves_private", host=host,
                 message=f"{host} 가 사설·내부 주소로 해석되어 가져올 수 없습니다")
    return str(addresses[0])


class _PinnedHTTPS(http.client.HTTPSConnection):
    """TLS is verified against the hostname while the TCP connection goes to the IP we already validated."""

    def __init__(self, host: str, port: int, ip: str, timeout: float) -> None:
        super().__init__(host, port, timeout=timeout, context=ssl.create_default_context())
        self._pinned_ip = ip

    def connect(self) -> None:
        sock = socket.create_connection((self._pinned_ip, self.port), self.timeout)
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            raise


def _http_get(host: str, port: int, target: str, ip: str, deadline: float) -> tuple[int, dict[str, str], bytes]:
    def remaining() -> float:
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError
        return left

    conn = _PinnedHTTPS(host, port, ip, remaining())
    response = None
    timer = None
    try:
        conn.connect()
        # getresponse() detaches conn.sock on Connection: close. The response's
        # makefile retains this socket until response.close(), including HTTP/1.0.
        body_socket = conn.sock

        def expire() -> None:
            try:
                body_socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        timer = threading.Timer(remaining(), expire)
        timer.daemon = True
        timer.start()
        body_socket.settimeout(remaining())
        conn.request("GET", target, headers=_REQUEST_HEADERS)
        body_socket.settimeout(remaining())
        response = conn.getresponse()
        headers = {k.lower(): v for k, v in response.getheaders()}
        if response.status != 200:
            return response.status, headers, b""
        declared = headers.get("content-length", "")
        if declared.isdigit() and int(declared) > svc.MAX_ASSET_BYTES:
            raise _TooLarge
        body = bytearray()
        while len(body) <= svc.MAX_ASSET_BYTES:
            body_socket.settimeout(remaining())
            chunk = response.read1(_CHUNK)
            remaining()
            if not chunk:
                if response.length not in (None, 0):
                    raise http.client.IncompleteRead(bytes(body), response.length)
                break
            body.extend(chunk)
            if response.isclosed():
                break
        return 200, headers, bytes(body)
    except (OSError, http.client.HTTPException):
        remaining()  # deadline-triggered shutdown maps to 504, not 502
        raise
    finally:
        if timer is not None:
            timer.cancel()
        if response is not None:
            response.close()
        conn.close()


def fetch_html_url(url: str) -> bytes:
    host, port, target = validate_source_url(url)
    ip = resolve_public_ip(host, port)
    try:
        status, headers, body = _http_get(host, port, target, ip, time.monotonic() + FETCH_TIMEOUT)
    except _TooLarge:
        svc.fail(413, "asset_too_large", max_bytes=svc.MAX_ASSET_BYTES,
                 message=f"HTML 이 {svc.MAX_ASSET_BYTES // (1024 * 1024)}MB 를 넘어 등록할 수 없습니다")
    except (TimeoutError, socket.timeout):
        svc.fail(504, "source_fetch_timeout", message=f"{int(FETCH_TIMEOUT)}초 안에 응답이 없어 가져오지 못했습니다")
    except (OSError, http.client.HTTPException):
        svc.fail(502, "source_fetch_failed", message="원본 주소에 연결하지 못했습니다")
    if 300 <= status < 400:
        svc.fail(422, "source_redirect_not_allowed", status=status,
                 message="리다이렉트되는 주소는 가져올 수 없습니다. 최종 주소를 직접 입력해 주세요")
    if status != 200:
        svc.fail(502, "source_fetch_failed", status=status, message=f"원본 주소가 HTTP {status} 를 돌려주었습니다")
    if headers.get("content-type", "").split(";")[0].strip().lower() != "text/html":
        svc.fail(422, "source_not_html", message="원본 주소가 text/html 이 아닙니다")
    return body


# --------------------------------------------------------------------- storage

def scan_references(uri: str, text: str) -> tuple[list[str], list[str]]:
    """-> (external/absolute references, relative or malformed ones), each de-duplicated in document order."""
    external: dict[str, None] = {}
    relative: dict[str, None] = {}
    for ref in svc.raw_references("text/html", text):
        try:
            if svc._child_uri(uri, ref):
                relative[ref.strip()[:200]] = None
        except svc.AssetProblem as exc:
            bucket = external if exc.code in ("external_reference_not_allowed", "absolute_reference_not_allowed") \
                else relative
            bucket[ref.strip()[:200]] = None
    return list(external), list(relative)


def _write_once(root: Path, rel: str, data: bytes, digest: str) -> bool:
    target = root / rel
    directory = target.parent
    directory.mkdir(parents=True, exist_ok=True)
    if not directory.resolve().is_relative_to(root.resolve()):
        svc.fail(500, "html_store_failed", message="저장 경로가 허용 범위를 벗어났습니다")
    if target.exists():
        if svc.sha256_hex(target.read_bytes()) != digest:
            svc.fail(409, "html_store_conflict", message="같은 경로에 내용이 다른 파일이 이미 있습니다")
        return False
    temp = directory / f".{uuid.uuid4().hex}.tmp"
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, target)
    finally:
        temp.unlink(missing_ok=True)
    return True


def store_html(project: str, data: bytes) -> tuple[dict[str, Any], bool]:
    """Validate and persist one HTML document. -> (manifest-ready asset fields, created)."""
    if len(data) > svc.MAX_ASSET_BYTES:
        svc.fail(413, "asset_too_large", max_bytes=svc.MAX_ASSET_BYTES,
                 message=f"HTML 이 {svc.MAX_ASSET_BYTES // (1024 * 1024)}MB 를 넘어 등록할 수 없습니다")
    if not data.strip():
        svc.fail(422, "html_empty", message="비어 있는 HTML 입니다")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        svc.fail(422, "asset_not_utf8", message="UTF-8 로 저장된 HTML 만 등록할 수 있습니다")
    if SECRET.search(text):
        svc.fail(422, "credential_content_rejected", message="HTML 안에 키·비밀번호로 보이는 내용이 있어 등록할 수 없습니다")
    digest = svc.sha256_hex(data)
    rel = f"{project}/html/{digest[:16]}/index.html"
    uri = f"{svc.ASSET_SCHEME}mockup_assets/{rel}"
    external, relative = scan_references(uri, text)
    if external:
        svc.fail(422, "html_external_reference", count=len(external), external_refs=external[:MAX_REPORTED_REFS],
                 message=f"HTML 안에 외부 주소 {len(external)}건이 있어 그대로는 등록할 수 없습니다. "
                         "이미지·폰트를 파일 안에 넣은(data:) 단일 HTML 로 저장해 다시 올려 주세요")
    if relative:
        svc.fail(422, "html_relative_reference", count=len(relative), relative_refs=relative[:MAX_REPORTED_REFS],
                 message=f"HTML 이 같이 올라가지 않는 파일 {len(relative)}건을 상대 경로로 참조합니다. "
                         "이미지·스타일·스크립트를 HTML 안에 넣은 단일 파일로 저장해 다시 올려 주세요")
    created = _write_once(svc.asset_roots()["mockup_assets"], rel, data, digest)
    return {"uri": uri, "mime": "text/html", "sha256": digest, "byte_size": len(data),
            "title": extract_title(text), "external_refs": []}, created


def stored_at(uri: str) -> str:
    """Stable capture time for a stored asset: the file's mtime, so a re-import yields the same manifest."""
    alias, rel = svc.parse_asset_uri(uri)
    mtime = (svc.asset_roots()[alias] / rel).stat().st_mtime
    return datetime.fromtimestamp(int(mtime), tz=timezone.utc).isoformat().replace("+00:00", "Z")


def read_chat_upload(session_id: str, upload_path: str) -> bytes:
    """Only files under <chat files root>/<this session id>/ — never another session's or an arbitrary path."""
    try:
        session = str(uuid.UUID(str(session_id)))
    except (ValueError, TypeError):
        svc.fail(422, "upload_path_not_allowed", message="채팅 세션을 확인할 수 없습니다")
    base = (chat_files_root() / session).resolve()
    raw = (upload_path or "").strip()
    if not raw or "\x00" in raw:
        svc.fail(422, "upload_path_not_allowed", message="upload_path 가 비어 있습니다")
    candidate = Path(raw)
    try:
        path = (candidate if candidate.is_absolute() else base / candidate).resolve(strict=True)
    except (OSError, RuntimeError):
        svc.fail(404, "upload_not_found", message="업로드한 파일을 찾을 수 없습니다")
    if not path.is_relative_to(base) or not path.is_file() or path.suffix.lower() not in HTML_SUFFIXES:
        svc.fail(422, "upload_path_not_allowed",
                 message="이 채팅에 올린 .html 파일만 가져올 수 있습니다")
    with path.open("rb") as stream:
        return stream.read(svc.MAX_ASSET_BYTES + 1)


async def import_from_bytes(project: str, data: bytes) -> tuple[dict[str, Any], bool]:
    return await asyncio.to_thread(store_html, project, data)


async def import_from_url(project: str, url: str) -> tuple[dict[str, Any], bool]:
    data = await asyncio.to_thread(fetch_html_url, url)
    return await asyncio.to_thread(store_html, project, data)


async def import_from_chat_upload(project: str, session_id: str, upload_path: str) -> tuple[dict[str, Any], bool]:
    data = await asyncio.to_thread(read_chat_upload, session_id, upload_path)
    return await asyncio.to_thread(store_html, project, data)
