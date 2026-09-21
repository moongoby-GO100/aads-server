"""Integrated screen E2E verification and Pipeline Runner evidence gate."""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable
from urllib.parse import urljoin, urlparse

import aiohttp

_SCREEN_MARKERS = (
    "ui ", "ui변경", "ui 변경", "화면", "프론트엔드", "frontend", "front-end",
    "로그인 필요", "login required", "스크린샷", "screenshot", "snapshot", "visual qa",
    "visual-qa", "시각 검수", "캡처",
)
_SCREEN_SUFFIXES = (".html", ".css", ".scss", ".sass", ".less", ".tsx", ".jsx", ".vue", ".svelte")
_SCREEN_PATH_MARKERS = ("/static/", "/templates/", "/frontend/", "/components/", "/pages/", "/app/")


def screen_verification_required(instruction: str, changed_files: list[str] | None = None) -> bool:
    """Classify only explicit screen work or files that necessarily render UI."""
    text = f" {(instruction or '').lower()} "
    if any(marker in text for marker in _SCREEN_MARKERS):
        return True
    for raw_path in changed_files or []:
        path = str(raw_path).lower().replace("\\", "/")
        if path.endswith(_SCREEN_SUFFIXES) and any(marker in f"/{path}" for marker in _SCREEN_PATH_MARKERS):
            return True
    return False


def evidence_passes_gate(evidence: Any) -> bool:
    if isinstance(evidence, str):
        try:
            evidence = json.loads(evidence)
        except json.JSONDecodeError:
            return False
    if not isinstance(evidence, dict) or evidence.get("schema") != "aads.e2e_verify.v1":
        return False
    stages = evidence.get("stages")
    if not isinstance(stages, dict):
        return False
    dom = stages.get("dom_assertion") or {}
    capture = stages.get("screenshot") or {}
    return evidence.get("passed") is True and dom.get("passed") is True and capture.get("success") is True


async def load_passing_evidence(conn: Any, job_id: str) -> dict[str, Any] | None:
    row = await conn.fetchrow(
        """SELECT metadata FROM task_logs
             WHERE task_id=$1 AND log_type='e2e_evidence'
             ORDER BY created_at DESC LIMIT 1""",
        job_id,
    )
    if not row:
        return None
    metadata = row["metadata"]
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except json.JSONDecodeError:
            return None
    evidence = metadata.get("evidence") if isinstance(metadata, dict) else None
    return evidence if evidence_passes_gate(evidence) else None


async def assert_screen_evidence_gate(
    conn: Any,
    *,
    job_id: str,
    instruction: str,
    changed_files: list[str] | None = None,
) -> None:
    if not screen_verification_required(instruction, changed_files):
        return
    if not await load_passing_evidence(conn, job_id):
        raise ValueError("screen_e2e_evidence_required")


async def _http_probe(url: str, timeout: float = 10.0) -> dict[str, Any]:
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
            async with session.get(url, allow_redirects=True) as response:
                return {
                    "success": response.status < 500,
                    "status_code": response.status,
                    "final_url": str(response.url),
                    "tool": "aiohttp",
                }
    except Exception as exc:  # network evidence must be returned, not raised
        return {"success": False, "status_code": None, "url": url, "tool": "aiohttp", "error": str(exc)[:500]}


async def _process_probe() -> dict[str, Any]:
    """Last-resort local runtime check; does not mutate or terminate processes."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ps", "-eo", "pid=,comm=", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5)
        lines = stdout.decode(errors="replace").splitlines()
        matches = [line.strip() for line in lines if any(name in line.lower() for name in ("uvicorn", "gunicorn", "python"))]
        return {"success": proc.returncode == 0 and bool(matches), "tool": "ps", "matches": matches[:10], "error": stderr.decode(errors="replace")[:300]}
    except Exception as exc:
        return {"success": False, "tool": "ps", "error": str(exc)[:500]}


async def run_e2e_verify(
    *,
    job_id: str,
    project: str,
    url: str,
    tenant_id: str,
    selectors: list[str],
    browser_session_id: str = "",
    browser_work_key: str = "",
    full_page: bool = False,
    http_probe: Callable[[str], Awaitable[dict[str, Any]]] = _http_probe,
    process_probe: Callable[[], Awaitable[dict[str, Any]]] = _process_probe,
) -> dict[str, Any]:
    """Run preflight → vault login → DOM assertions → existing screenshot capture."""
    if not url or not selectors:
        raise ValueError("url_and_selectors_required")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("http_url_required")
    from app.api.ceo_chat_tools import _browser_domain_ok

    blocked_reason = _browser_domain_ok(url)
    if blocked_reason:
        raise ValueError(f"url_not_allowed:{blocked_reason}")

    evidence: dict[str, Any] = {
        "schema": "aads.e2e_verify.v1",
        "job_id": job_id,
        "project": project.upper(),
        "url": url,
        "route": parsed.path or "/",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "stages": {},
        "fallback_used": False,
        "passed": False,
    }
    evidence["stages"]["preflight"] = await http_probe(url)
    browser_error = ""
    page = None
    try:
        from app.api.ceo_chat_tools import (
            _acquire_pw_context,
            _pre_inject_vault_token,
            tool_capture_screenshot,
        )
        from app.core.credential_vault import list_credentials
        from app.services.agent_vault_service import list_agent_credentials, normalize_origin

        agent_credentials = await list_agent_credentials(
            tenant_id=tenant_id,
            work_key=browser_work_key or project,
            origin=normalize_origin(url),
        ) if tenant_id else []
        legacy_credentials = await list_credentials(
            project=project,
            include_secrets=False,
            tenant_id=tenant_id,
        ) if tenant_id else []
        target_host = parsed.netloc.lower()
        legacy_matches = [
            item for item in legacy_credentials
            if urlparse(str(item.get("login_url") or item.get("url") or "")).netloc.lower() == target_host
        ]
        credentials = agent_credentials or legacy_matches
        credential_source = "agent_vault" if agent_credentials else ("credential_vault" if legacy_matches else "none")
        login = {
            "attempted": bool(credentials),
            "credential_matched": bool(credentials),
            "credential_source": credential_source,
            "credential_id": str(credentials[0]["id"]) if credentials else None,
            "success": False,
            "tool": "credential_vault/agent_vault",
        }
        context, context_error = await _acquire_pw_context(browser_session_id, browser_work_key, url)
        if context_error:
            raise RuntimeError(context_error)
        page = await context.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        if credentials:
            login["success"] = await _pre_inject_vault_token(
                page, url, tenant_id=tenant_id, browser_work_key=browser_work_key or project,
            )
            if login["success"]:
                await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        evidence["stages"]["credential_login"] = login

        assertions = []
        for selector in selectors:
            found = bool(await page.locator(selector).count())
            assertions.append({"selector": selector, "found": found})
        evidence["stages"]["dom_assertion"] = {
            "passed": all(item["found"] for item in assertions),
            "assertions": assertions,
            "tool": "playwright",
            "final_url": str(page.url),
        }
        await page.close()
        page = None

        capture_result = await tool_capture_screenshot(
            url,
            full_page,
            browser_session_id=browser_session_id,
            browser_work_key=browser_work_key or project,
            tenant_id=tenant_id,
            close_on_complete=True,
        )
        screenshot_url = re.search(r"https?://[^\s)]+", capture_result or "")
        evidence["stages"]["screenshot"] = {
            "success": "[ERROR]" not in (capture_result or "") and bool(screenshot_url),
            "url": screenshot_url.group(0) if screenshot_url else None,
            "tool": "capture_screenshot",
            "result": (capture_result or "")[:500],
        }
    except Exception as exc:
        browser_error = str(exc)[:500]
    finally:
        if page is not None:
            try:
                await page.close()
            except Exception:
                pass

    browser_ok = (
        evidence["stages"].get("dom_assertion", {}).get("passed") is True
        and evidence["stages"].get("screenshot", {}).get("success") is True
    )
    if not browser_ok:
        evidence["fallback_used"] = True
        origin = f"{parsed.scheme}://{parsed.netloc}"
        evidence["stages"]["fallback"] = {
            "browser_error": browser_error,
            "http_status": evidence["stages"]["preflight"],
            "api_health": await http_probe(urljoin(origin, "/health")),
            "process": await process_probe(),
            "order": ["http_status", "api_health", "process"],
        }
    evidence["passed"] = browser_ok and evidence["stages"]["preflight"].get("success") is True

    from app.core.db_pool import get_pool

    async with get_pool().acquire() as conn:
        await conn.execute(
            """INSERT INTO task_logs (task_id, log_type, content, phase, metadata)
               VALUES ($1, 'e2e_evidence', $2, 'e2e_verify', $3::jsonb)""",
            job_id,
            json.dumps(evidence, ensure_ascii=False)[:2000],
            json.dumps({"evidence": evidence}, ensure_ascii=False),
        )
    return evidence
