"""Integrated screen E2E verification and Pipeline Runner evidence gate."""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
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
_NON_RENDERING_SUFFIXES = (
    ".py", ".sql", ".md", ".txt", ".yml", ".yaml", ".toml", ".cfg", ".ini", ".sh", ".env.example", ".local",
    # Drafts/backups/patches are never served to a browser, even when the stem is a UI file (a.tsx.bak).
    ".sql_draft", ".md_draft", ".draft", ".bak", ".orig", ".patch", ".diff",
)
# Data/manifest files render nothing on their own, so a backend release manifest
# must not demand screen evidence. They count as screen work only inside a UI
# source tree (dashboard config, i18n strings), where the path markers apply.
#
# Raster images follow the same rule: a report evidence screenshot (reports/x/01.png)
# is an artifact, not a UI change, but dashboard public/components assets still render.
# .svg is deliberately NOT listed: it is XML markup the browser renders and can carry
# script/CSS, so it stays screen work everywhere.
_NON_RENDERING_DATA_SUFFIXES = (
    ".json", ".csv", ".tsv",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico",
)
_BROWSER_FLOW_TIMEOUT_SECONDS = 150.0
_DOM_SETTLE_TIMEOUT_MS = 20_000
_DOM_ASSERTION_BUDGET_SECONDS = 20.0


def _renders_nothing(raw_path: str) -> bool:
    """True when the file cannot render UI by itself, so it needs no screen proof."""
    path = str(raw_path).lower().replace("\\", "/")
    if path.endswith(_NON_RENDERING_SUFFIXES):
        return True
    # Extensionless files (.gitignore, Dockerfile, Makefile) are never parsed as markup/styles by a browser, so exempt them regardless of path markers.
    basename = path.rstrip("/").rsplit("/", 1)[-1]
    if path and not path.endswith("/") and basename and "." not in basename.lstrip("."):
        return True
    if not path.endswith(_NON_RENDERING_DATA_SUFFIXES):
        return False
    return not any(marker in f"/{path}" for marker in _SCREEN_PATH_MARKERS)


def screen_verification_required(instruction: str, changed_files: list[str] | None = None) -> bool:
    """Classify only explicit screen work or files that necessarily render UI."""
    text = f" {(instruction or '').lower()} "
    if any(marker in text for marker in _SCREEN_MARKERS):
        if not changed_files:
            return True
        return not all(_renders_nothing(path) for path in changed_files)
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


# task_logs_log_type_check only allows info/command/output/error/phase_change/e2e_evidence,
# so deferral/overdue rows are 'info' and told apart by phase (varchar(50), unconstrained).
DEFERRED_LOG_TYPE = "info"
OVERDUE_LOG_TYPE = "info"
DEFERRED_PHASE = "e2e_screen_evidence_deferred"
OVERDUE_PHASE = "e2e_screen_evidence_overdue"
DEFER_REASON_MIN_CHARS = 10
DEFER_DEADLINE_MINUTES = 60


def _as_metadata_dict(metadata: Any) -> dict[str, Any]:
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except json.JSONDecodeError:
            return {}
    return metadata if isinstance(metadata, dict) else {}


def validate_defer_reason(reason: Any) -> str:
    text = str(reason or "").strip()
    if len(text) < DEFER_REASON_MIN_CHARS:
        raise ValueError("screen_evidence_defer_reason_required")
    return text


async def load_deferred_evidence(conn: Any, job_id: str) -> dict[str, Any] | None:
    """Latest CEO-approved 'verify after deploy' record for the job, if any."""
    row = await conn.fetchrow(
        """SELECT metadata FROM task_logs
             WHERE task_id=$1 AND log_type=$2 AND phase=$3
             ORDER BY created_at DESC LIMIT 1""",
        job_id,
        DEFERRED_LOG_TYPE,
        DEFERRED_PHASE,
    )
    if not row:
        return None
    metadata = _as_metadata_dict(row["metadata"])
    if not metadata.get("deadline_at") or not metadata.get("reason"):
        return None
    return metadata


async def record_screen_evidence_deferral(
    conn: Any,
    *,
    job_id: str,
    approver: str,
    reason: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    deferred_at = now or datetime.now(timezone.utc)
    metadata = {
        "approver": approver or "unknown",
        "reason": reason,
        "deferred_at": deferred_at.isoformat(),
        "deadline_at": (deferred_at + timedelta(minutes=DEFER_DEADLINE_MINUTES)).isoformat(),
    }
    await conn.execute(
        """INSERT INTO task_logs (task_id, log_type, content, phase, metadata)
           VALUES ($1, $2, $3, $4, $5::jsonb)""",
        job_id,
        DEFERRED_LOG_TYPE,
        f"screen evidence deferred to post-deploy: {reason}"[:2000],
        DEFERRED_PHASE,
        json.dumps(metadata, ensure_ascii=False),
    )
    return metadata


async def assert_screen_evidence_gate(
    conn: Any,
    *,
    job_id: str,
    instruction: str,
    changed_files: list[str] | None = None,
    defer_screen_evidence: bool = False,
    defer_reason: str = "",
    approver: str = "",
) -> None:
    reason = validate_defer_reason(defer_reason) if defer_screen_evidence else ""
    if not screen_verification_required(instruction, changed_files):
        return
    if await load_passing_evidence(conn, job_id):
        return
    if await load_deferred_evidence(conn, job_id):
        return
    if defer_screen_evidence:
        await record_screen_evidence_deferral(conn, job_id=job_id, approver=approver, reason=reason)
        return
    raise ValueError("screen_e2e_evidence_required")


async def check_overdue_deferred_evidence(
    conn: Any,
    *,
    now: datetime | None = None,
    notify: Callable[..., Awaitable[Any]] | None = None,
) -> list[str]:
    """Alert once per job whose deferred screen evidence is still missing past its deadline.

    Alert only; never rolls anything back. Returns the job_ids alerted this pass.
    """
    current = now or datetime.now(timezone.utc)
    rows = await conn.fetch(
        """SELECT t.task_id AS job_id, t.metadata
             FROM task_logs t
            WHERE t.log_type=$1 AND t.phase=$2
              AND t.created_at > NOW() - INTERVAL '7 days'
              AND NOT EXISTS (
                    SELECT 1 FROM task_logs o
                     WHERE o.task_id=t.task_id AND o.log_type=$3 AND o.phase=$4)
            ORDER BY t.created_at LIMIT 20""",
        DEFERRED_LOG_TYPE,
        DEFERRED_PHASE,
        OVERDUE_LOG_TYPE,
        OVERDUE_PHASE,
    )
    alerted: list[str] = []
    for row in rows or []:
        job_id = row["job_id"]
        metadata = _as_metadata_dict(row["metadata"])
        try:
            deadline = datetime.fromisoformat(str(metadata.get("deadline_at") or ""))
        except ValueError:
            continue
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        if current < deadline:
            continue
        if await load_passing_evidence(conn, job_id):
            continue
        claimed = await conn.fetchrow(
            """UPDATE pipeline_jobs
                  SET logs = COALESCE(logs, '[]'::jsonb) || jsonb_build_array(jsonb_build_object(
                          'ts', NOW()::text,
                          'event', 'screen_evidence_overdue',
                          'deadline_at', $2::text)),
                      updated_at = NOW()
                WHERE job_id = $1
                  AND NOT (COALESCE(logs, '[]'::jsonb) @> '[{"event":"screen_evidence_overdue"}]'::jsonb)
            RETURNING chat_session_id, project""",
            job_id,
            str(metadata.get("deadline_at")),
        )
        if not claimed:
            continue
        await conn.execute(
            """INSERT INTO task_logs (task_id, log_type, content, phase, metadata)
               VALUES ($1, $2, $3, $4, $5::jsonb)""",
            job_id,
            OVERDUE_LOG_TYPE,
            "deferred screen evidence overdue",
            OVERDUE_PHASE,
            json.dumps({"deadline_at": metadata.get("deadline_at"), "reason": metadata.get("reason")}, ensure_ascii=False),
        )
        alerted.append(job_id)
        if notify is not None:
            try:
                await notify(
                    job_id=job_id,
                    session_id=claimed["chat_session_id"],
                    project=claimed["project"] or "",
                    deadline_at=str(metadata.get("deadline_at")),
                    reason=str(metadata.get("reason") or ""),
                )
            except Exception:
                pass
    return alerted


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
        async with asyncio.timeout(_BROWSER_FLOW_TIMEOUT_SECONDS):
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

            try:
                await page.wait_for_load_state("networkidle", timeout=_DOM_SETTLE_TIMEOUT_MS)
                settle = "networkidle"
            except Exception:
                settle = "timeout"

            deadline = asyncio.get_running_loop().time() + _DOM_ASSERTION_BUDGET_SECONDS
            assertions = []
            for selector in selectors:
                remaining_ms = max(0, int((deadline - asyncio.get_running_loop().time()) * 1000))
                if remaining_ms > 0:
                    try:
                        await page.wait_for_selector(selector, state="attached", timeout=remaining_ms)
                    except Exception:
                        pass
                found = bool(await page.locator(selector).count())
                assertions.append({"selector": selector, "found": found})
            evidence["stages"]["dom_assertion"] = {
                "passed": all(item["found"] for item in assertions),
                "assertions": assertions,
                "tool": "playwright",
                "final_url": str(page.url),
                "settle": settle,
                "budget_seconds": _DOM_ASSERTION_BUDGET_SECONDS,
            }
            await page.close()
            page = None

            capture_result = await tool_capture_screenshot(
                url,
                full_page,
                browser_session_id=browser_session_id,
                browser_work_key=browser_work_key,
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
