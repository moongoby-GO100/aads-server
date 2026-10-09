"""Integrated screen E2E verification and Pipeline Runner evidence gate."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable
from urllib.parse import urljoin, urlparse

import aiohttp

logger = logging.getLogger(__name__)

_SCREEN_MARKERS = (
    "ui ", "ui변경", "ui 변경", "화면", "프론트엔드", "frontend", "front-end",
    "로그인 필요", "login required", "스크린샷", "screenshot", "snapshot", "visual qa",
    "visual-qa", "시각 검수", "캡처",
)
_SCREEN_SUFFIXES = (".html", ".css", ".scss", ".sass", ".less", ".tsx", ".jsx", ".vue", ".svelte")
_SCREEN_PATH_MARKERS = ("/static/", "/templates/", "/frontend/", "/components/", "/pages/", "/app/", "/public/")
# Classification is an allow-list: a file counts as screen work only when it has a markup/style
# extension AND sits in a UI tree. Everything else (.service/.timer/.conf, scripts, data) renders
# nothing, so new non-UI extensions never need to be added to a deny-list.
_SCREEN_ALLOW_SUFFIXES = _SCREEN_SUFFIXES + (".svg",)
# Browser scripts count only under a static tree (a .js next to a .py handler is served to the browser).
_STATIC_SCRIPT_SUFFIXES = (".js", ".mjs")
_I18N_PATH_MARKERS = ("/locales/", "/locale/", "/i18n/", "/messages/", "/lang/", "/translations/")
_NON_RENDERING_SUFFIXES = (
    ".py", ".sql", ".md", ".txt", ".yml", ".yaml", ".toml", ".cfg", ".ini", ".sh", ".env.example", ".local",
    # Drafts/backups/patches are never served to a browser, even when the stem is a UI file (a.tsx.bak).
    ".sql_draft", ".md_draft", ".draft", ".bak", ".orig", ".patch", ".diff",
    # systemd units and daemon config are host configuration, not pages.
    ".service", ".timer", ".conf", ".socket", ".mount", ".target",
)
# Data/manifest files render nothing on their own, so a backend release manifest
# must not demand screen evidence. They count as screen work only inside a UI
# source tree (dashboard config, i18n strings), where the path markers apply.
#
# Raster images follow the same rule: a report evidence screenshot (reports/x/01.png)
# is an artifact, not a UI change, but dashboard public/components assets still render.
_NON_RENDERING_DATA_SUFFIXES = (
    ".json", ".csv", ".tsv",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico",
)
_BROWSER_FLOW_TIMEOUT_SECONDS = 150.0
_DOM_SETTLE_TIMEOUT_MS = 20_000
_DOM_ASSERTION_BUDGET_SECONDS = 20.0


def _norm_path(raw_path: Any) -> str:
    return str(raw_path).lower().replace("\\", "/")


def _renders_nothing(raw_path: str) -> bool:
    """True when the file cannot render UI by itself, so it needs no screen proof."""
    path = _norm_path(raw_path)
    if path.endswith(_NON_RENDERING_SUFFIXES):
        return True
    # Extensionless files (.gitignore, Dockerfile, Makefile) are never parsed as markup/styles by a browser, so exempt them regardless of path markers.
    basename = path.rstrip("/").rsplit("/", 1)[-1]
    if path and not path.endswith("/") and basename and "." not in basename.lstrip("."):
        return True
    if not path.endswith(_NON_RENDERING_DATA_SUFFIXES):
        return False
    return not any(marker in f"/{path}" for marker in _SCREEN_PATH_MARKERS)


def _is_screen_file(raw_path: Any) -> bool:
    """Allow-list test: screen extension (or UI-tree i18n json / static script) inside a UI path."""
    path = _norm_path(raw_path)
    rooted = f"/{path}"
    if path.endswith(_SCREEN_ALLOW_SUFFIXES):
        return any(marker in rooted for marker in _SCREEN_PATH_MARKERS)
    if path.endswith(".json"):
        return any(marker in rooted for marker in _SCREEN_PATH_MARKERS) and any(
            marker in rooted for marker in _I18N_PATH_MARKERS
        )
    if path.endswith(_STATIC_SCRIPT_SUFFIXES):
        return "/static/" in rooted
    return False


def screen_verification_required(instruction: str, changed_files: list[str] | None = None) -> bool:
    """Screen work = at least one changed file is a UI file; instruction keywords apply only without a file list."""
    if changed_files:
        return any(_is_screen_file(path) for path in changed_files)
    text = f" {(instruction or '').lower()} "
    return any(marker in text for marker in _SCREEN_MARKERS)


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


# Legacy 'deferred evidence' rows (before the plan gate) are still readable; nothing writes them any more.
DEFERRED_LOG_TYPE = "info"
DEFERRED_PHASE = "e2e_screen_evidence_deferred"

STATE_PENDING_RELEASE = "pending_release"
STATE_VERIFYING = "verifying"
STATE_PASSED = "passed"
STATE_FAILED = "failed"
STATE_UNVERIFIABLE = "unverifiable"
STATE_NOT_REQUIRED = "not_required"

POST_DEPLOY_WINDOW_HOURS = 24
PUSH_ONLY_WINDOW_DAYS = 7
POST_DEPLOY_MAX_CONCURRENT = 2
VERIFY_STALE_MINUTES = 30
UNVERIFIABLE_RETRY_DELAY_SECONDS = 60.0
DEFAULT_DASHBOARD_BASE_URL = "https://aads.newtalk.kr"
_SCREEN_COLUMNS = ("screen_evidence_state", "e2e_spec")
_screen_columns_cached = False

_PUSH_ONLY_OFF_RE = re.compile(r"push[_-]?only\s*[:=]\s*(?:false|no|0|off)(?![a-z0-9_])")
_PUSH_ONLY_ANY_RE = re.compile(r"(?<![a-z0-9_])push[_-]?only(?![a-z0-9_])")
_DEPLOY_POLICY_RE = re.compile(
    r"(?:^|\n)[ \t>*#-]*(?:deploy(?:ment)?|release)[_ -]?policy[ \t]*[:=][ \t]*"
    r"(?:push[_-]?only|commit[_-]?only|no[_-]?deploy|forbid(?:den)?|deny|denied|none|block(?:ed)?)(?![a-z0-9_])"
)
_DEPLOY_OFF_RE = re.compile(
    r"(?:^|\n)[ \t>*#-]*deploy(?:ment)?[ \t]*[:=][ \t]*(?:false|no|off|0|forbid(?:den)?|deny|denied|none)(?![a-z0-9_])"
)
_COMMIT_ONLY_RE = re.compile(r"(?:커밋|push)\s*까지만")
_DEPLOY_FORBID_RE = re.compile(r"(?:배포|재기동|deploy).{0,12}(?:금지|하지\s*(?:마|말)|말라)")
_E2E_DIRECTIVE_RE = re.compile(r"^[ \t>*#-]*e2e_verify[ \t]*:[ \t]*(.+)$", re.IGNORECASE | re.MULTILINE)
_E2E_URL_RE = re.compile(r"\burl\s*=\s*(\S+)", re.IGNORECASE)
_E2E_SELECTORS_RE = re.compile(r"\bselectors\s*=\s*(.+)$", re.IGNORECASE)


def _as_metadata_dict(metadata: Any) -> dict[str, Any]:
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except json.JSONDecodeError:
            return {}
    return metadata if isinstance(metadata, dict) else {}


async def load_deferred_evidence(conn: Any, job_id: str) -> dict[str, Any] | None:
    """Legacy CEO-approved 'verify after deploy' record for the job, if any (read-only)."""
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


DEFER_REASON_MIN_CHARS = 10


def validate_defer_reason(reason: Any) -> str:
    """deprecated: 2026-10-09 배포 후 화면 검증(_run_post_deploy_screen_verify)으로 대체.

    Kept as a compatibility shim; validation logic is unchanged.
    """
    text = str(reason or "").strip()
    if len(text) < DEFER_REASON_MIN_CHARS:
        raise ValueError("screen_evidence_defer_reason_required")
    return text


async def record_screen_evidence_deferral(
    conn: Any,
    *,
    job_id: str,
    approver: str,
    reason: str,
    now: datetime | None = None,
) -> None:
    """deprecated: 2026-10-09 배포 후 화면 검증(_run_post_deploy_screen_verify)으로 대체.

    No-op shim: never touches the DB, only logs a warning.
    """
    logger.warning(
        "record_screen_evidence_deferral is deprecated (replaced by post-deploy watchdog); no-op job=%s approver=%s",
        job_id, approver or "unknown",
    )
    return None


async def check_overdue_deferred_evidence(
    conn: Any,
    *,
    now: datetime | None = None,
    notify: Callable[..., Awaitable[Any]] | None = None,
) -> list[str]:
    """deprecated: 2026-10-09 배포 후 화면 검증(_run_post_deploy_screen_verify)으로 대체.

    No-op shim: the 60-minute overdue alert stays off; always returns an empty list and never notifies.
    """
    logger.warning("check_overdue_deferred_evidence is deprecated (replaced by post-deploy watchdog); no-op")
    return []


def instruction_is_push_only(instruction: str) -> bool:
    """Structured push-only declarations (mirror of the runner's instruction_forbids_deploy core rules)."""
    lowered = (instruction or "").lower()[:200_000]
    if not lowered:
        return False
    scrubbed = _PUSH_ONLY_OFF_RE.sub(" ", lowered)
    return any(
        pattern.search(scrubbed)
        for pattern in (_PUSH_ONLY_ANY_RE, _DEPLOY_POLICY_RE, _DEPLOY_OFF_RE, _COMMIT_ONLY_RE, _DEPLOY_FORBID_RE)
    )


def _split_selectors(raw: str) -> list[str]:
    parts = re.split(r"\s*\|\s*", raw) if "|" in raw else re.split(r"\s*,\s*", raw)
    return [part.strip().strip("`'\"") for part in parts if part.strip().strip("`'\"")]


def parse_e2e_verify_directive(instruction: str) -> dict[str, Any] | None:
    """`E2E_VERIFY: url=https://host/path selectors=#app | .title` -> plan, or None when absent/invalid."""
    for match in _E2E_DIRECTIVE_RE.finditer(instruction or ""):
        line = match.group(1)
        url_match = _E2E_URL_RE.search(line)
        if not url_match:
            continue
        url = url_match.group(1).strip().strip("`'\"<>,;")
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            continue
        rest = line[: url_match.start()] + " " + line[url_match.end():]
        selectors_match = _E2E_SELECTORS_RE.search(rest)
        selectors = _split_selectors(selectors_match.group(1)) if selectors_match else []
        return {"url": url, "selectors": selectors or ["body"], "source": "instruction"}
    return None


def _dashboard_route(raw_path: str, project: str) -> str | None:
    """`.../src/app/<route>/page.tsx` -> `/<route>`; None for dynamic/private segments or other apps."""
    path = str(raw_path).replace("\\", "/")
    if "aads-dashboard/" not in path and (project or "").upper() != "AADS":
        return None
    marker = "src/app/"
    index = path.find(marker)
    if index < 0:
        return None
    segments = path[index + len(marker):].split("/")
    if not re.fullmatch(r"page\.(?:tsx|jsx|ts|js)", segments[-1]):
        return None
    route_segments = [seg for seg in segments[:-1] if not (seg.startswith("(") and seg.endswith(")"))]
    if any(seg.startswith(("[", "@", "_")) or not seg for seg in route_segments):
        return None
    return "/" + "/".join(route_segments)


def derive_screen_verification_plan(
    instruction: str, changed_files: list[str] | None = None, project: str = ""
) -> dict[str, Any] | None:
    plan = parse_e2e_verify_directive(instruction)
    if plan:
        return plan
    base = (os.getenv("AADS_DASHBOARD_BASE_URL") or DEFAULT_DASHBOARD_BASE_URL).rstrip("/")
    for raw_path in changed_files or []:
        route = _dashboard_route(str(raw_path), project)
        if route is not None:
            return {"url": f"{base}{route}", "selectors": ["body"], "source": f"dashboard_route:{raw_path}"}
    return None


def assert_screen_verification_plan(
    instruction: str, changed_files: list[str] | None = None, project: str = ""
) -> dict[str, Any] | None:
    """Approval-time gate: screen work must carry a post-deploy E2E plan; evidence itself comes after deploy."""
    if not screen_verification_required(instruction, changed_files):
        return None
    plan = derive_screen_verification_plan(instruction, changed_files, project)
    if not plan:
        raise ValueError("screen_e2e_plan_required")
    return plan


async def screen_columns_ready(conn: Any) -> bool:
    """False until the screen_evidence_state/e2e_spec migration is applied (the gate then only validates)."""
    global _screen_columns_cached
    if _screen_columns_cached:
        return True
    count = await conn.fetchval(
        """SELECT COUNT(*) FROM information_schema.columns
            WHERE table_name='pipeline_jobs' AND column_name = ANY($1::text[])""",
        list(_SCREEN_COLUMNS),
    )
    _screen_columns_cached = bool(count) and int(count) >= len(_SCREEN_COLUMNS)
    return _screen_columns_cached



async def apply_screen_approval_gate(
    conn: Any,
    *,
    job_id: str,
    instruction: str,
    changed_files: list[str] | None = None,
    project: str = "",
    defer_screen_evidence: bool = False,
    defer_reason: str = "",
    approver: str = "",
    persist: bool = True,
) -> dict[str, Any]:
    """Approval gate: plan check (or push-only pending_release); never demands evidence.

    defer_* are accepted for API compatibility only and have no effect.
    """
    if defer_screen_evidence:
        logger.info(
            "screen_evidence_defer_ignored job=%s approver=%s reason=%s",
            job_id, approver or "unknown", (defer_reason or "")[:200],
        )
    if not screen_verification_required(instruction, changed_files):
        return {"required": False, "state": None, "plan": None}
    if instruction_is_push_only(instruction):
        plan = derive_screen_verification_plan(instruction, changed_files, project)
        state: str | None = STATE_PENDING_RELEASE
    else:
        plan = assert_screen_verification_plan(instruction, changed_files, project)
        state = None
    if persist and await screen_columns_ready(conn):
        await conn.execute(
            """UPDATE pipeline_jobs
                  SET e2e_spec = $2::jsonb, screen_evidence_state = $3, updated_at = NOW()
                WHERE job_id = $1""",
            job_id,
            json.dumps(plan, ensure_ascii=False) if plan else None,
            state,
        )
    return {"required": True, "state": state, "plan": plan}


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
    """Compatibility entry (read-only): now the plan gate, no longer demands evidence."""
    await apply_screen_approval_gate(
        conn, job_id=job_id, instruction=instruction, changed_files=changed_files,
        defer_screen_evidence=defer_screen_evidence, defer_reason=defer_reason,
        approver=approver, persist=False,
    )


def classify_screen_evidence(evidence: Any, plan: dict[str, Any] | None = None) -> tuple[str, str]:
    """(state, reason). failed only when the page was reached and demonstrably wrong; otherwise unverifiable."""
    if evidence_passes_gate(evidence):
        return STATE_PASSED, "passed"
    stages = evidence.get("stages") if isinstance(evidence, dict) else None
    if not isinstance(stages, dict):
        return STATE_UNVERIFIABLE, "no_evidence"
    preflight = stages.get("preflight") or {}
    status = preflight.get("status_code")
    if isinstance(status, int) and (status >= 500 or status == 404):
        return STATE_FAILED, f"preflight_http_{status}"
    dom = stages.get("dom_assertion")
    if isinstance(dom, dict) and dom.get("passed") is False and preflight.get("success") is True:
        expected_path = urlparse(str((plan or {}).get("url") or "")).path
        final_path = urlparse(str(dom.get("final_url") or "")).path.lower()
        if any(word in final_path for word in ("login", "signin", "sign-in")) and not any(
            word in expected_path.lower() for word in ("login", "signin", "sign-in")
        ):
            return STATE_UNVERIFIABLE, "login_redirect"
        return STATE_FAILED, "dom_assertion_failed"
    return STATE_UNVERIFIABLE, "browser_flow_incomplete"


def summarize_screen_evidence(evidence: Any) -> dict[str, Any]:
    stages = evidence.get("stages") if isinstance(evidence, dict) else None
    if not isinstance(stages, dict):
        return {}
    dom = stages.get("dom_assertion") or {}
    fallback = stages.get("fallback") or {}
    summary: dict[str, Any] = {
        "preflight_status": (stages.get("preflight") or {}).get("status_code"),
        "dom_passed": dom.get("passed"),
        "missing_selectors": [a.get("selector") for a in dom.get("assertions") or [] if not a.get("found")],
        "screenshot": (stages.get("screenshot") or {}).get("url"),
    }
    if fallback:
        summary["fallback"] = {
            "browser_error": fallback.get("browser_error"),
            "api_health": (fallback.get("api_health") or {}).get("status_code"),
            "process_ok": (fallback.get("process") or {}).get("success"),
        }
    return summary


_JOB_COLUMNS = (
    "{a}job_id, {a}project, {a}tenant_id::text AS tenant_id, {a}instruction, "
    "{a}actual_changed_files, {a}e2e_spec, {a}chat_session_id"
)


def _job_from_row(row: Any) -> dict[str, Any]:
    files = row["actual_changed_files"]
    if isinstance(files, str):
        try:
            files = json.loads(files)
        except json.JSONDecodeError:
            files = []
    spec = _as_metadata_dict(row["e2e_spec"])
    return {
        "job_id": row["job_id"],
        "project": row["project"] or "",
        "tenant_id": row["tenant_id"] or "",
        "instruction": row["instruction"] or "",
        "changed_files": list(files or []),
        "plan": spec if spec.get("url") and spec.get("selectors") else None,
        "session_id": row["chat_session_id"],
    }


async def mark_push_only_pending(conn: Any, limit: int = 50) -> int:
    """Done push-only jobs the approval path could not label (runner detected PUSH_ONLY): wait for a release."""
    rows = await conn.fetch(
        f"""SELECT {_JOB_COLUMNS.format(a="")}
              FROM pipeline_jobs
             WHERE status='done' AND phase='push_only_by_directive'
               AND screen_evidence_state IS NULL
               AND completed_at > NOW() - make_interval(days => $2)
             ORDER BY completed_at DESC LIMIT $1""",
        limit,
        PUSH_ONLY_WINDOW_DAYS,
    )
    marked = 0
    for row in rows or []:
        job = _job_from_row(row)
        required = screen_verification_required(job["instruction"], job["changed_files"])
        plan = job["plan"] or derive_screen_verification_plan(job["instruction"], job["changed_files"], job["project"])
        await conn.execute(
            """UPDATE pipeline_jobs
                  SET screen_evidence_state = $2, e2e_spec = COALESCE(e2e_spec, $3::jsonb), updated_at = NOW()
                WHERE job_id = $1 AND screen_evidence_state IS NULL""",
            job["job_id"],
            STATE_PENDING_RELEASE if required else STATE_NOT_REQUIRED,
            json.dumps(plan, ensure_ascii=False) if (required and plan) else None,
        )
        marked += 1
    return marked


async def promote_pending_release(conn: Any, limit: int) -> list[dict[str, Any]]:
    """pending_release -> verifying once a later deploy of the same project has completed."""
    if limit <= 0:
        return []
    rows = await conn.fetch(
        f"""UPDATE pipeline_jobs p
               SET screen_evidence_state = 'verifying', updated_at = NOW()
             WHERE p.job_id IN (
                    SELECT q.job_id FROM pipeline_jobs q
                     WHERE q.screen_evidence_state = 'pending_release'
                       AND q.status = 'done' AND q.completed_at IS NOT NULL
                       AND q.completed_at > NOW() - make_interval(days => $2)
                       AND EXISTS (SELECT 1 FROM pipeline_jobs d
                                    WHERE d.project = q.project AND d.status = 'done'
                                      AND d.deployed_at IS NOT NULL AND d.deployed_at > q.completed_at)
                     ORDER BY q.completed_at LIMIT $1)
               AND p.screen_evidence_state = 'pending_release'
            RETURNING {_JOB_COLUMNS.format(a="p.")}""",
        limit,
        PUSH_ONLY_WINDOW_DAYS,
    )
    return [_job_from_row(row) for row in rows or []]


async def claim_post_deploy_candidates(
    conn: Any, limit: int, exclude: set[str] | None = None
) -> list[dict[str, Any]]:
    """Deployed (<24h) done jobs without a verdict: screen work -> verifying, anything else -> not_required."""
    if limit <= 0:
        return []
    rows = await conn.fetch(
        f"""SELECT {_JOB_COLUMNS.format(a="")}
              FROM pipeline_jobs
             WHERE status = 'done' AND deployed_at IS NOT NULL
               AND deployed_at > NOW() - make_interval(hours => $1)
               AND (screen_evidence_state IS NULL OR screen_evidence_state = 'pending_release')
               AND job_id <> ALL($2::text[])
             ORDER BY deployed_at LIMIT 50""",
        POST_DEPLOY_WINDOW_HOURS,
        sorted(exclude or ()),
    )
    claimed: list[dict[str, Any]] = []
    for row in rows or []:
        job = _job_from_row(row)
        if not screen_verification_required(job["instruction"], job["changed_files"]):
            await conn.execute(
                """UPDATE pipeline_jobs SET screen_evidence_state = 'not_required', updated_at = NOW()
                    WHERE job_id = $1 AND (screen_evidence_state IS NULL OR screen_evidence_state = 'pending_release')""",
                job["job_id"],
            )
            continue
        if len(claimed) >= limit:
            continue
        won = await conn.fetchrow(
            f"""UPDATE pipeline_jobs SET screen_evidence_state = 'verifying', updated_at = NOW()
                 WHERE job_id = $1 AND (screen_evidence_state IS NULL OR screen_evidence_state = 'pending_release')
             RETURNING {_JOB_COLUMNS.format(a="")}""",
            job["job_id"],
        )
        if won:
            claimed.append(_job_from_row(won))
    return claimed


async def reclaim_stale_verifying(conn: Any, limit: int, exclude: set[str] | None = None) -> list[dict[str, Any]]:
    """A verifier that died mid-run leaves 'verifying' behind; hand such rows out again."""
    if limit <= 0:
        return []
    rows = await conn.fetch(
        f"""UPDATE pipeline_jobs
               SET updated_at = NOW()
             WHERE job_id IN (
                    SELECT job_id FROM pipeline_jobs
                     WHERE screen_evidence_state = 'verifying' AND status = 'done'
                       AND updated_at < NOW() - make_interval(mins => $2)
                       AND job_id <> ALL($3::text[])
                     ORDER BY updated_at LIMIT $1)
               AND screen_evidence_state = 'verifying'
            RETURNING {_JOB_COLUMNS.format(a="")}""",
        limit,
        VERIFY_STALE_MINUTES,
        sorted(exclude or ()),
    )
    return [_job_from_row(row) for row in rows or []]


async def _persist_screen_state(pool: Any, job_id: str, state: str, reason: str, summary: dict[str, Any]) -> None:
    """Only the verdict columns change: status stays 'done' (no cascade cancel, no slot held)."""
    async with pool.acquire() as conn:
        await conn.execute(
            """UPDATE pipeline_jobs
                  SET screen_evidence_state = $2,
                      logs = COALESCE(logs, '[]'::jsonb) || jsonb_build_array(jsonb_build_object(
                          'ts', NOW()::text, 'event', 'screen_verify', 'state', $2::text,
                          'reason', $3::text, 'summary', $4::jsonb)),
                      updated_at = NOW()
                WHERE job_id = $1""",
            job_id,
            state,
            reason[:300],
            json.dumps(summary, ensure_ascii=False, default=str),
        )


async def verify_screen_job(
    job: dict[str, Any],
    *,
    pool: Any,
    notify: Callable[..., Awaitable[Any]] | None = None,
    verify: Callable[..., Awaitable[dict[str, Any]]] | None = None,
    retry_delay: float = UNVERIFIABLE_RETRY_DELAY_SECONDS,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
) -> str:
    """Run the post-deploy E2E for one job and record passed/failed/unverifiable (one retry on unverifiable)."""
    verify = verify or run_e2e_verify
    job_id = job["job_id"]
    plan = job.get("plan") or derive_screen_verification_plan(
        job.get("instruction", ""), job.get("changed_files"), job.get("project", "")
    )
    state, reason, evidence = STATE_UNVERIFIABLE, "screen_e2e_plan_missing", None
    try:
        if plan:
            for attempt in (1, 2):
                evidence, error = None, ""
                try:
                    evidence = await verify(
                        job_id=job_id, project=job.get("project", ""), url=plan["url"],
                        tenant_id=job.get("tenant_id", ""), selectors=list(plan["selectors"]),
                    )
                except ValueError as exc:
                    error = str(exc)[:300]
                    state, reason = STATE_UNVERIFIABLE, f"e2e_rejected:{error}"
                    break
                except Exception as exc:
                    error = str(exc)[:300]
                state, reason = (
                    classify_screen_evidence(evidence, plan) if evidence
                    else (STATE_UNVERIFIABLE, f"e2e_error:{error}")
                )
                if state != STATE_UNVERIFIABLE or attempt == 2:
                    break
                await sleep(retry_delay)
    except Exception as exc:
        logger.warning("post_deploy_screen_verify_error job=%s err=%s", job_id, exc)
        state, reason = STATE_UNVERIFIABLE, f"verifier_error:{str(exc)[:200]}"
    summary = summarize_screen_evidence(evidence)
    try:
        await _persist_screen_state(pool, job_id, state, reason, summary)
    except Exception as exc:
        logger.warning("post_deploy_screen_state_persist_error job=%s err=%s", job_id, exc)
        return state
    if state in (STATE_FAILED, STATE_UNVERIFIABLE) and notify is not None:
        try:
            await notify(
                job_id=job_id, session_id=job.get("session_id"), project=job.get("project", ""),
                state=state, reason=reason, plan=plan, summary=summary,
            )
        except Exception as exc:
            logger.warning("post_deploy_screen_notify_error job=%s err=%s", job_id, exc)
    return state


async def run_post_deploy_cycle(
    conn: Any,
    *,
    inflight: set[str],
    verify_job: Callable[[dict[str, Any]], Awaitable[Any]],
    max_concurrent: int = POST_DEPLOY_MAX_CONCURRENT,
) -> list[asyncio.Task]:
    """One watchdog pass: label push-only jobs, pick up to the free slots, run each verifier in the background."""
    if not await screen_columns_ready(conn):
        return []
    await mark_push_only_pending(conn)
    free = max_concurrent - len(inflight)
    jobs: list[dict[str, Any]] = []
    for picker in (
        lambda n: promote_pending_release(conn, n),
        lambda n: claim_post_deploy_candidates(conn, n, inflight),
        lambda n: reclaim_stale_verifying(conn, n, inflight),
    ):
        if free - len(jobs) <= 0:
            break
        jobs.extend(await picker(free - len(jobs)))
    tasks: list[asyncio.Task] = []
    for job in jobs:
        inflight.add(job["job_id"])
        task = asyncio.create_task(_run_guarded(job, inflight, verify_job))
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
        tasks.append(task)
    return tasks


_background_tasks: set[asyncio.Task] = set()


async def _run_guarded(job: dict[str, Any], inflight: set[str], verify_job: Callable[[dict[str, Any]], Awaitable[Any]]) -> None:
    try:
        await verify_job(job)
    except Exception as exc:
        logger.warning("post_deploy_screen_verify_task_error job=%s err=%s", job.get("job_id"), exc)
    finally:
        inflight.discard(job["job_id"])



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
