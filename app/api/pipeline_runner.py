"""
Pipeline Runner API v2 — DB 기반 작업 제출/승인/조회.

보안: 입력 검증(H6), 파라미터화 쿼리(C1), JWT 인증(C2 — main.py 미들웨어)
"""
from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import uuid
from dataclasses import dataclass
from functools import lru_cache
from typing import Optional

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator, model_serializer

from app.auth import TenantRole, get_current_user, tenant_role_allows
from app.core.project_config import PROJECT_MAP
from app.services.goal_binding import parse_goal_binding

router = APIRouter()
logger = structlog.get_logger(__name__)
TenantContext = dict[str, object]


async def _internal_pipeline_context(request: Request, x_monitor_key: Optional[str]) -> TenantContext | None:
    monitor_key = (
        x_monitor_key
        or request.headers.get("x-monitor-key")
        or request.headers.get("X-Monitor-Key")
        or ""
    ).strip()
    if monitor_key != "internal-pipeline-call":
        return None
    request_path = request.url.path or ""
    if (
        not request_path.startswith(("/api/v1/pipeline/", "/pipeline/"))
        and "/pipeline/" not in request_path
    ):
        return None

    from app.core.db_pool import get_pool

    pool = get_pool()
    async with pool.acquire() as conn:
        tenant = await conn.fetchrow(
            """
            SELECT id::text AS id, slug, name, kind, status
              FROM tenants
             WHERE slug = 'internal'
               AND deleted_at IS NULL
             LIMIT 1
            """
        )
    if not tenant:
        raise HTTPException(status_code=503, detail="Internal tenant is not initialized")
    membership = {
        "id": "internal-pipeline-call",
        "tenant_id": tenant["id"],
        "user_id": "system:pipeline-runner",
        "role": TenantRole.OWNER.value,
        "status": "active",
    }
    user = {
        "user_id": "system:pipeline-runner",
        "email": "system@aads.internal",
        "is_admin": True,
        "tenant_id": tenant["id"],
        "current_tenant": dict(tenant),
        "current_membership": membership,
        "tenant_role": TenantRole.OWNER.value,
        "user_role": "system",
        "is_internal_admin": True,
    }
    return {"user": user, "tenant": user["current_tenant"], "membership": membership}


def require_pipeline_tenant_role(minimum: TenantRole):
    async def _dependency(
        request: Request,
        authorization: str = Header(None),
        x_tenant_id: Optional[str] = Header(None, alias="X-Tenant-ID"),
        x_monitor_key: Optional[str] = Header(None, alias="x-monitor-key"),
    ) -> TenantContext:
        context = await _internal_pipeline_context(request, x_monitor_key)
        if context is None:
            current_user = await get_current_user(
                request,
                authorization=authorization,
                x_tenant_id=x_tenant_id,
                x_monitor_key=x_monitor_key,
            )
            context = {
                "user": current_user,
                "tenant": current_user["current_tenant"],
                "membership": current_user["current_membership"],
            }
        role = context.get("membership", {}).get("role")  # type: ignore[union-attr]
        if not tenant_role_allows(role, minimum):
            raise HTTPException(status_code=403, detail=f"{minimum.value} role required")
        return context

    return _dependency


require_tenant_viewer = require_pipeline_tenant_role(TenantRole.VIEWER)
require_tenant_member = require_pipeline_tenant_role(TenantRole.MEMBER)


def _tenant_id(context: TenantContext) -> str:
    return str(context["tenant"]["id"])  # type: ignore[index]

# H6 + M4: 허용 프로젝트 화이트리스트
_VALID_PROJECTS = set(PROJECT_MAP) | {"ACCT"}
_UUID_RE = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
_JOB_ID_RE = re.compile(r'^runner-[0-9a-zA-Z_-]+$')
_ACTIVE_PIPELINE_STATUSES = (
    "queued",
    "claimed",
    "running",
    "awaiting_approval",
    "approved",
    "deploying",
    "rolling_back",
)
_TERMINAL_BLOCKING_STATUSES = (
    "error",
    "rejected",
    "rejected_done",
    "cancelled",
    "build_fail",
    "deploy_failed",
    "review_failed",
    "review_hold",
    "auth_unavailable",
    "auth_recovery_pending",
    "awaiting_user_auth",
    "tool_timeout",
    "dedup_blocked",
    "blocked_dependency",
)
_DISPLAY_STATUS_LABELS = {
    "no_changes": "변경 없음",
    "dedup_blocked": "중복 차단",
    "blocked_dependency": "의존 차단",
    "build_fail": "빌드 실패",
    "deploy_failed": "배포 실패",
    "review_failed": "검수 실패",
    "review_hold": "AI 리뷰 재검수 대기",
    "auth_unavailable": "인증 필요",
    "auth_recovery_pending": "인증 복구 대기",
    "awaiting_user_auth": "사용자 인증 필요",
    "tool_timeout": "도구 타임아웃",
}
_DISPLAY_STATUS_GROUPS = {
    "no_changes": "complete",
    "dedup_blocked": "blocked",
    "blocked_dependency": "blocked",
    "build_fail": "action_required",
    "deploy_failed": "action_required",
    "review_failed": "action_required",
    "review_hold": "action_required",
    "auth_unavailable": "action_required",
    "auth_recovery_pending": "action_required",
    "awaiting_user_auth": "action_required",
    "tool_timeout": "action_required",
}
_DEFAULT_LOCAL_PID_PROJECTS = {"AADS"}
_AUTO_FILE_DEPENDENCY_EVENT = "file_conflict_auto_dependency"
_TARGET_FILE_RE = re.compile(
    r"(?<![A-Za-z0-9@])"
    r"(?:/root/aads/(?:aads-server|aads-dashboard)/)?"
    r"[A-Za-z0-9_.@-]+(?:/[A-Za-z0-9_.@-]+)*"
    r"\.(?:py|tsx|ts|jsx|js|sh|sql|ya?ml|json|md|html|css|toml|ini|conf)"
    r"(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_SPECIAL_TARGET_RE = re.compile(
    r"(?<![A-Za-z0-9@])"
    r"(?:/root/aads/(?:aads-server|aads-dashboard)/)?"
    r"(?:Dockerfile|docker-compose(?:\.[A-Za-z0-9_-]+)?\.ya?ml|package-lock\.json|package\.json|deploy\.sh)"
    r"(?![A-Za-z0-9_.-])",
    re.IGNORECASE,
)
_PATH_TRAILING_CHARS = ".,;:)]}'\"`"
_EXEC_COMMAND_PREFIXES = (
    "bash ",
    "sh ",
    "python3 -m pytest",
    "pytest ",
    "npm ",
    "npx ",
    "curl ",
    "docker ",
)


def _max_concurrent_per_project() -> int:
    """API 표시/잠금 판단용 동시 실행 상한. Shell runner 기본값과 맞춘다."""
    try:
        return max(1, int(os.getenv("MAX_CONCURRENT_PER_PROJECT", "6")))
    except ValueError:
        return 6


def _record_get(row, key: str, default=None):
    """asyncpg.Record 안전 조회. Record는 dict.get을 보장하지 않는다."""
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


async def _pipeline_column_exists(conn, column_name: str) -> bool:
    return bool(await conn.fetchval(
        """
        SELECT EXISTS (
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 'pipeline_jobs'
              AND column_name = $1
        )
        """,
        column_name,
    ))


def _normalize_target_file_path(path: str) -> str:
    """Normalize file references so jobs touching the same file serialize.

    `a/./b`, `a//b`, `a/../b` 는 접어서 같은 파일로 본다. 저장소 밖으로 나가는
    경로(`../x`)는 빈 문자열 — 호출부는 빈 값을 건너뛴다.
    """
    value = (path or "").strip().strip(_PATH_TRAILING_CHARS.replace(".", "")).rstrip(_PATH_TRAILING_CHARS)
    value = value.replace("\\", "/")
    if not value:
        return ""
    repo = ""
    if value.startswith("/root/aads/aads-server/"):
        repo, rel = "server", value.removeprefix("/root/aads/aads-server/")
    elif value.startswith("/root/aads/aads-dashboard/"):
        repo, rel = "dashboard", value.removeprefix("/root/aads/aads-dashboard/")
    else:
        rel = re.sub(r"^\./+", "", value)
    rel = posixpath.normpath(rel) if rel else ""
    if rel == ".":
        rel = ""
    if rel == ".." or rel.startswith(("../", "/")):
        return ""
    if not repo:
        if rel.startswith("aads-server/"):
            repo, rel = "server", rel.removeprefix("aads-server/")
        elif rel.startswith("aads-dashboard/"):
            repo, rel = "dashboard", rel.removeprefix("aads-dashboard/")
        elif rel.startswith(("src/", "public/")) or rel in {"package.json", "package-lock.json"}:
            repo = "dashboard"
        else:
            repo = "server"
    return f"{repo}:{rel}"


def _line_is_exec_command(line: str) -> bool:
    """검증/실행 명령으로 시작하는 줄인지 판정.

    마크다운 인라인 코드(백틱)·목록 기호·쉘 프롬프트 장식은 명령 여부 판정 전에
    벗겨낸다 — 지시서가 `` `bash scripts/run_unit_tests.sh ...` `` 처럼
    백틱으로 감싸는 경우가 흔하다.
    """
    stripped = line.strip().lstrip("`$#->* \t").strip()
    return stripped.startswith(_EXEC_COMMAND_PREFIXES)


def _line_prefix_has_exec_command(prefix: str) -> bool:
    """매치 시작 위치 이전, 같은 줄 부분 문자열에 실행 명령 토큰이 있는지 판정.

    `_line_is_exec_command()` 는 줄이 명령으로 "시작"하는 경우만 잡는다.
    "4. 신규 단위테스트 추가 후 `bash <스크립트> <테스트파일>` 확인." 처럼
    목록 번호·설명 문장 뒤에 명령이 오면 줄 전체는 명령으로 시작하지 않으므로
    놓친다(AADS-RUNNERGUARD-VERIFY-PATH-MIDLINE-P1). 매치 앞의 같은 줄
    부분 문자열만 보고 명령 토큰이 등장하는지 확인해 이 경우를 잡는다 —
    앞선 문장이나 다른 줄에 등장한 경로는 건드리지 않는다.
    """
    cleaned = prefix.lstrip("`$#->* \t")
    return any(token in cleaned for token in _EXEC_COMMAND_PREFIXES)


def _scan_instruction_paths(text: str, skip_lines: frozenset[int] = frozenset()) -> set[str]:
    """지시문 본문에서 경로처럼 생긴 토큰을 모은다(실행 명령 인자는 제외)."""
    files: set[str] = set()
    lines = text.splitlines()
    matches = list(_TARGET_FILE_RE.finditer(text)) + list(_SPECIAL_TARGET_RE.finditer(text))
    for match in matches:
        line_idx = text.count("\n", 0, match.start())
        if line_idx in skip_lines:
            continue
        line = lines[line_idx] if 0 <= line_idx < len(lines) else ""
        if _line_is_exec_command(line):
            continue
        line_start = text.rfind("\n", 0, match.start()) + 1
        prefix_on_line = text[line_start:match.start()]
        if _line_prefix_has_exec_command(prefix_on_line):
            continue
        normalized = _normalize_target_file_path(match.group(0))
        if normalized:
            files.add(normalized)
    return files


_SCOPE_MARKER_RE = re.compile(
    r"^[\s>*`#-]*\**(TARGET_FILES|READ_ONLY_FILES|REFERENCE_FILES|FORBIDDEN_FILES)\**\s*[:=]\s*(.*)$"
)
_SCOPE_BULLET_RE = re.compile(r"^\s*[-*]\s+(.+)$")
_SCOPE_NONE_TOKENS = frozenset({"none", "n/a", "na", "-", "없음", "(none)"})
_SCOPE_TOKEN_RE = re.compile(
    r"^(?:/root/aads/aads-(?:server|dashboard)/|\./)?"
    r"[A-Za-z0-9_.@-]+(?:/[A-Za-z0-9_.@-]+)*/?$"
)
_COMMON_RECORD_FILE_RE = re.compile(
    r"(?:^|[:/])(?:[A-Za-z0-9_-]*HANDOVER|CHANGELOG[A-Za-z0-9_.-]*)\.md$",
    re.IGNORECASE,
)
_WRITE_SCOPE_KEYS = {
    "TARGET_FILES": "write",
    "READ_ONLY_FILES": "read",
    "REFERENCE_FILES": "read",
    "FORBIDDEN_FILES": "forbidden",
}


@dataclass(frozen=True)
class _WriteScope:
    """지시서가 선언한 실제 수정 범위.

    explicit=True 이면 `write` 는 TARGET_FILES(+본문이 언급한 공통 기록 파일)뿐이고
    본문에서 읽기 참조로 언급된 경로는 `undeclared` 로만 남는다. explicit=False 는
    기존 보수적 판정 — 본문의 모든 비명령 경로가 `write`.
    """

    explicit: bool
    write: frozenset[str]
    read: frozenset[str] = frozenset()
    forbidden: frozenset[str] = frozenset()
    undeclared: frozenset[str] = frozenset()
    error: str = ""


def _is_dir_scope(path: str) -> bool:
    return path.endswith("/") or path.endswith(":")


def _is_common_record_file(path: str) -> bool:
    return bool(_COMMON_RECORD_FILE_RE.search(path))


def _scope_paths_overlap(a: str, b: str) -> bool:
    if a == b:
        return True
    if _is_dir_scope(a) and b.startswith(a):
        return True
    return _is_dir_scope(b) and a.startswith(b)


def _scope_overlap(a: set[str] | frozenset[str], b: set[str] | frozenset[str]) -> list[str]:
    """같은 파일 또는 디렉터리 포함 관계인 쌍을 더 좁은 쪽 경로로 돌려준다."""
    out: set[str] = set()
    for x in a:
        for y in b:
            if _scope_paths_overlap(x, y):
                if _is_dir_scope(x) and _is_dir_scope(y):
                    out.add(max(x, y, key=len))
                else:
                    out.add(y if _is_dir_scope(x) else x)
    return sorted(out)


def _parse_scope_token(token: str) -> str | None:
    """마커 값 한 토큰 → 정규화 경로. 파일은 `repo:rel`, 디렉터리는 끝에 `/`. 잘못되면 None."""
    value = token.strip().strip("`'\"").rstrip(",;")
    value = re.sub(r":\d+(?:-\d+)?$", "", value)
    if not value or not _SCOPE_TOKEN_RE.match(value):
        return None
    is_dir = value.endswith("/")
    core = value.rstrip("/")
    last = core.rsplit("/", 1)[-1]
    if not is_dir:
        if "." in last or last in {"Dockerfile", "Makefile"}:
            pass
        elif "/" in core:
            is_dir = True
        else:
            return None
    normalized = _normalize_target_file_path(core)
    if not normalized:
        return None
    if is_dir:
        return normalized if normalized.endswith(":") else f"{normalized}/"
    return normalized


def _scope_marker_tokens(value: str, *, block: bool) -> list[str]:
    value = re.sub(r"\([^)]*\)", " ", value)
    if block:
        value = value.strip().strip("`").split(None, 1)[0] if value.strip() else ""
        return [value] if value else []
    return [t for t in re.split(r"[\s,;]+", value.replace("`", " ")) if t]


def _parse_write_scope(instruction: str) -> _WriteScope:
    """지시서의 TARGET_FILES / READ_ONLY_FILES / REFERENCE_FILES / FORBIDDEN_FILES 를 해석한다.

    - TARGET_FILES 가 없거나 비어 있으면 explicit=False — 기존처럼 본문 경로 전부가 쓰기.
    - 마커 값이 잘못됐거나(경로 아님) TARGET_FILES 와 FORBIDDEN_FILES 가 모순이면 같은
      보수적 판정으로 되돌리고 `error` 에 사유를 남긴다 — 선언을 믿을 수 없을 때
      읽기로 추정해 직렬화를 풀지 않는다.
    - 읽기 선언에 같은 경로가 있어도 TARGET_FILES 에 있으면 쓰기가 이긴다.
    - 본문이 언급한 공통 기록 파일(HANDOVER/CHANGELOG)은 읽기/금지로 선언되지 않았다면
      explicit 모드에서도 쓰기로 유지한다.
    """
    text = instruction or ""
    lines = text.splitlines()
    declared: dict[str, set[str]] = {"write": set(), "read": set(), "forbidden": set()}
    marker_lines: set[int] = set()
    error = ""
    idx = 0
    while idx < len(lines):
        m = _SCOPE_MARKER_RE.match(lines[idx])
        if not m:
            idx += 1
            continue
        kind = _WRITE_SCOPE_KEYS[m.group(1)]
        marker_lines.add(idx)
        inline = m.group(2).strip()
        raw_tokens: list[str] = []
        if inline:
            raw_tokens = _scope_marker_tokens(inline, block=False)
        else:
            while idx + 1 < len(lines):
                b = _SCOPE_BULLET_RE.match(lines[idx + 1])
                if not b:
                    break
                idx += 1
                marker_lines.add(idx)
                raw_tokens.extend(_scope_marker_tokens(b.group(1), block=True))
        idx += 1
        for raw in raw_tokens:
            if raw.strip("`'\"").lower() in _SCOPE_NONE_TOKENS:
                continue
            parsed = _parse_scope_token(raw)
            if parsed is None:
                error = error or f"{m.group(1)} 값이 경로가 아님: {raw[:60]}"
                continue
            declared[kind].add(parsed)

    write, read, forbidden = declared["write"], declared["read"], declared["forbidden"]
    if not error:
        for w in write:
            if any(w == f or (_is_dir_scope(f) and w.startswith(f)) for f in forbidden):
                error = f"TARGET_FILES 와 FORBIDDEN_FILES 가 모순: {w}"
                break

    legacy = _scan_instruction_paths(text)
    if error or not write:
        return _WriteScope(explicit=False, write=frozenset(legacy), error=error)

    prose = _scan_instruction_paths(text, frozenset(marker_lines))
    explicit_read_or_forbidden = read | forbidden

    def _declared_non_write(path: str) -> bool:
        return any(
            path == d or (_is_dir_scope(d) and path.startswith(d))
            for d in explicit_read_or_forbidden
        )

    common = {p for p in prose if _is_common_record_file(p) and not _declared_non_write(p)}
    undeclared = {
        p for p in prose
        if p not in common
        and not any(_scope_paths_overlap(p, w) for w in write)
        and not _declared_non_write(p)
    }
    return _WriteScope(
        explicit=True,
        write=frozenset(write | common),
        read=frozenset(read),
        forbidden=frozenset(forbidden),
        undeclared=frozenset(undeclared),
    )


def _extract_target_files(instruction: str) -> set[str]:
    """Extract the files a runner instruction will modify (conflict scope).

    TARGET_FILES 가 명시되면 그 범위(+공통 기록 파일)만 쓰기로 본다. 없으면 기존처럼
    본문의 모든 경로를 쓰기로 본다 — 검증/실행 명령 인자는 어느 쪽이든 제외한다
    (AADS-RUNNERGUARD-VERIFY-PATH-FALSEPOSITIVE-R2, ...-MIDLINE-P1). 상세는
    `_parse_write_scope`.
    """
    return set(_parse_write_scope(instruction).write)


def _log_write_scope(scope: _WriteScope, *, job_id: str, project: str) -> None:
    if scope.error:
        logger.warning(
            "pipeline_runner.write_scope_malformed",
            job_id=job_id, project=project, error=scope.error,
        )
    elif scope.explicit and scope.undeclared:
        logger.info(
            "pipeline_runner.write_scope_undeclared_paths",
            job_id=job_id, project=project,
            write=sorted(scope.write), undeclared=sorted(scope.undeclared),
        )


def _batch_scope_owners(
    item_files: set[str] | frozenset[str],
    batch_file_owner: dict[str, str],
) -> list[tuple[str, str]]:
    """배치 앞선 항목 중 item_files 와 겹치는 (겹친 경로, 선행 job_id), 경로순."""
    pairs: dict[str, str] = {}
    for path in item_files:
        for owner_path, owner_job in batch_file_owner.items():
            if _scope_paths_overlap(path, owner_path):
                narrow = _scope_overlap({path}, {owner_path})[0]
                pairs.setdefault(narrow, owner_job)
    return sorted(pairs.items())


async def _find_active_file_conflict(
    conn,
    *,
    project: str,
    target_files: set[str],
    tenant_id: str,
    incoming_instruction: str = "",
    incoming_goal_id: str = "",
    incoming_milestone_id: str = "",
    ignore_job_ids: set[str] | None = None,
) -> dict | None:
    """Return an active job touching one of target_files.

    A same-goal job may only depend on the same or an earlier milestone.  The
    previous newest-first lookup could make a recovered V11-1 depend on an
    already queued V11-7 merely because both mentioned the same file.  Return
    that case as an explicit inversion so callers fail closed instead of
    persisting a backwards edge.
    """
    if not target_files:
        return None
    ignored = ignore_job_ids or set()
    incoming_order = await _resolve_milestone_order(
        conn,
        project=project,
        tenant_id=tenant_id,
        instruction=incoming_instruction,
        goal_id=incoming_goal_id,
        milestone_id=incoming_milestone_id,
    )
    rows = await conn.fetch(
        """
        SELECT job_id, instruction, status, phase
        FROM pipeline_jobs
        WHERE project = $1
          AND tenant_id = $2::uuid
          AND status = ANY($3::text[])
        ORDER BY created_at DESC
        LIMIT 100
        """,
        project,
        tenant_id,
        list(_ACTIVE_PIPELINE_STATUSES),
    )
    inverted_conflict = None
    for row in rows:
        existing_job_id = row["job_id"]
        if existing_job_id in ignored:
            continue
        existing_files = _extract_target_files(row["instruction"] or "")
        overlap = _scope_overlap(target_files, existing_files)
        if overlap:
            existing_order = await _resolve_milestone_order(
                conn,
                project=project,
                tenant_id=tenant_id,
                instruction=row["instruction"] or "",
            )
            if _dependency_order_is_inverted(incoming_order, existing_order):
                inverted_conflict = {
                    "job_id": existing_job_id,
                    "status": row["status"],
                    "phase": row["phase"],
                    "overlap": overlap,
                    "dependency_inversion": True,
                    "incoming_sequence": incoming_order[2],
                    "parent_sequence": existing_order[2],
                }
                continue
            return {
                "job_id": existing_job_id,
                "status": row["status"],
                "phase": row["phase"],
                "overlap": overlap,
            }
    if inverted_conflict:
        return inverted_conflict
    # Cross-session: chat-direct dirty 파일 충돌 확인
    ledger_rows = await conn.fetch(
        """
        SELECT session_id, file_path, source_tool, owner, task_id, updated_at
        FROM chat_workspace_change_ledger
        WHERE project = $1 AND status = 'dirty'
          AND updated_at > NOW() - INTERVAL '24 hours'
        """,
        project,
    )
    for lrow in ledger_rows:
        ledger_file = lrow["file_path"]
        if ledger_file in target_files:
            return {
                "job_id": f"chat-direct:{lrow['session_id'][:8]}",
                "status": "chat_direct_dirty",
                "phase": "editing",
                "overlap": [ledger_file],
                "source": "chat_workspace_change_ledger",
                "owner": lrow["owner"],
                "task_id": lrow["task_id"],
            }
    return None


async def _resolve_milestone_order(
    conn,
    *,
    project: str,
    tenant_id: str,
    instruction: str = "",
    goal_id: str = "",
    milestone_id: str = "",
) -> tuple[str, str, int] | None:
    """Resolve tenant-scoped canonical order from API fields/directive metadata.

    Do not require ``milestones.project == runner project``.  A cross-project
    goal may deliberately use the AADS runner to change shared orchestration
    code while the goal itself belongs to GO100 (the V11 recovery is one such
    case).  Tenant + goal + milestone identity remains the authorization and
    ordering boundary.
    """
    binding = parse_goal_binding(
        instruction,
        goal_id=goal_id or None,
        milestone_id=milestone_id or None,
    )
    if not binding.goal_id or not binding.milestone_id:
        return None
    row = await conn.fetchrow(
        """
        SELECT goal_id::text AS goal_id, id::text AS milestone_id, sequence_order
        FROM milestones
        WHERE id = $1::uuid
          AND goal_id = $2::uuid
          AND tenant_id = $3::uuid
        """,
        binding.milestone_id,
        binding.goal_id,
        tenant_id,
    )
    if not row:
        return None
    return str(row["goal_id"]), str(row["milestone_id"]), int(row["sequence_order"])


def _dependency_order_is_inverted(
    child: tuple[str, str, int] | None,
    parent: tuple[str, str, int] | None,
) -> bool:
    """True when a same-goal child points to a strictly later milestone."""
    return bool(child and parent and child[0] == parent[0] and parent[2] > child[2])


def _dependency_inversion_detail(child_job_id: str, conflict: dict) -> str:
    return (
        "dependency_inversion: refusing later milestone parent "
        f"{conflict['job_id']} (sequence={conflict['parent_sequence']}) for "
        f"{child_job_id} (sequence={conflict['incoming_sequence']})"
    )


def _local_pid_projects() -> set[str]:
    """Projects whose runner_pid belongs to this API host.

    KIS/GO100/SF/NTV2 runners execute on remote servers, so checking their
    runner_pid against this host's /proc would create false stale positives.
    """
    raw = os.getenv("PIPELINE_RUNNER_LOCAL_PID_PROJECTS", "AADS")
    projects = {item.strip().upper() for item in raw.split(",") if item.strip()}
    return projects or set(_DEFAULT_LOCAL_PID_PROJECTS)


def _is_local_runner_project(project: str | None) -> bool:
    return (project or "").upper() in _local_pid_projects()


@lru_cache(maxsize=1)
def _runner_pid_namespace_visible() -> bool:
    """러너 호스트의 PID를 이 프로세스에서 볼 수 있는지 판정한다.

    API가 자체 PID namespace를 가진 컨테이너에서 돌면 호스트 러너 PID는
    /proc 에 없다. 그 상태로 생존 판정을 하면 살아 있는 작업이 전부
    process_died 로 강제 종결된다.
    """
    if not os.path.exists("/.dockerenv"):
        return True
    try:
        with open("/proc/1/comm", encoding="utf-8") as handle:
            return handle.read().strip() in ("systemd", "init")
    except OSError:
        return False


def _local_pid_alive(pid) -> bool | None:
    if not pid:
        return None
    if not _runner_pid_namespace_visible():
        return None
    try:
        return os.path.exists(f"/proc/{int(pid)}")
    except (TypeError, ValueError):
        return False


def _compute_instruction_hash(project: str, instruction: str) -> str:
    return hashlib.sha256(f"{project}:{instruction}".encode()).hexdigest()[:16]


async def _lock_instruction_hash(conn, instruction_hash: str) -> None:
    """동일 instruction_hash 제출을 트랜잭션 단위로 직렬화한다."""
    await conn.execute(
        "SELECT pg_advisory_xact_lock(hashtext($1)::bigint)",
        f"pipeline_jobs:{instruction_hash}",
    )


def _parallel_scope(parallel_group: str | None) -> str:
    return (parallel_group or "").strip()


async def _find_active_duplicate(conn, project: str, instruction_hash: str, parallel_group: str = "", tenant_id: str = ""):
    scope = _parallel_scope(parallel_group)
    return await conn.fetchrow(
        """
        SELECT job_id, status, phase, parallel_group
        FROM pipeline_jobs
        WHERE project = $1
          AND instruction_hash = $2
          AND COALESCE(parallel_group, '') = $3
          AND tenant_id = $4::uuid
          AND status = ANY($5::text[])
        ORDER BY
          CASE status
            WHEN 'running' THEN 0
            WHEN 'claimed' THEN 1
            WHEN 'awaiting_approval' THEN 2
            WHEN 'approved' THEN 3
            WHEN 'deploying' THEN 4
            WHEN 'rolling_back' THEN 5
            WHEN 'queued' THEN 6
            ELSE 7
          END,
          created_at ASC
        LIMIT 1
        """,
        project,
        instruction_hash,
        scope,
        tenant_id,
        list(_ACTIVE_PIPELINE_STATUSES),
    )


async def _record_dedup_blocked(conn, *, job_id: str, req: "JobSubmitRequest",
                                instruction_hash: str, existing, tenant_id: str) -> str:
    detail = (
        f"dedup_blocked: existing job {existing['job_id']} "
        f"is {existing['status']}/{existing['phase']}"
    )
    await conn.execute(
        """
        INSERT INTO pipeline_jobs
          (job_id, project, instruction, instruction_hash, chat_session_id,
           status, phase, max_cycles, size, parallel_group, depends_on,
           error_detail, review_feedback, logs, created_at, updated_at, tenant_id)
        VALUES ($1, $2, $3, $4, $5,
                'cancelled', 'dedup_blocked', $6, $7, $8, $9,
                $10, $11,
                jsonb_build_array(jsonb_build_object(
                  'ts', NOW()::text,
                  'event', 'dedup_blocked',
                  'existing_job_id', $12::text,
                  'existing_status', $13::text,
                  'existing_phase', $14::text,
                  'parallel_scope', $15::text,
                  'auto_retryable', false
                )),
                NOW(), NOW(), $16::uuid)
        """,
        job_id,
        req.project,
        req.instruction,
        instruction_hash,
        req.session_id,
        req.max_cycles,
        req.size,
        req.parallel_group or None,
        req.depends_on or None,
        detail,
        f"[Runner Guard] {detail}; auto_retryable=false",
        existing["job_id"],
        existing["status"],
        existing["phase"],
        _parallel_scope(req.parallel_group),
        tenant_id,
    )
    logger.info("pipeline_runner.submit_dedup_blocked",
                blocked_job_id=job_id,
                existing_job_id=existing["job_id"],
                instruction_hash=instruction_hash,
                parallel_scope=_parallel_scope(req.parallel_group))
    return detail


async def _record_blocked_dependency(conn, *, job_id: str, req: "JobSubmitRequest",
                                     instruction_hash: str, dep_status: str, tenant_id: str) -> str:
    detail = f"blocked_dependency: parent {req.depends_on} is {dep_status}"
    await conn.execute(
        """
        INSERT INTO pipeline_jobs
          (job_id, project, instruction, instruction_hash, chat_session_id,
           status, phase, max_cycles, size, parallel_group, depends_on,
           error_detail, review_feedback, logs, created_at, updated_at, tenant_id)
        VALUES ($1, $2, $3, $4, $5,
                'cancelled', 'blocked_dependency', $6, $7, $8, $9,
                $10, $11,
                jsonb_build_array(jsonb_build_object(
                  'ts', NOW()::text,
                  'event', 'blocked_dependency',
                  'depends_on', $12::text,
                  'upstream_status', $13::text,
                  'auto_retryable', false
                )),
                NOW(), NOW(), $14::uuid)
        """,
        job_id,
        req.project,
        req.instruction,
        instruction_hash,
        req.session_id,
        req.max_cycles,
        req.size,
        req.parallel_group or None,
        req.depends_on or None,
        detail,
        f"[Runner Guard] {detail}; auto_retryable=false",
        req.depends_on,
        dep_status,
        tenant_id,
    )
    logger.info("pipeline_runner.submit_blocked_dependency",
                blocked_job_id=job_id,
                depends_on=req.depends_on,
                upstream_status=dep_status)
    return detail


async def _get_model_for_size(conn, size: str) -> str:
    """작업 규모 → DB 설정/리뷰 라우팅 순서 기반 1순위 모델 조회."""
    cycle = await _get_model_cycle_for_size(conn, size)
    if cycle:
        return cycle[0]
    # DB 조회 실패 시에도 Python runner의 전-size 기본 계약과 일치시킨다.
    return "claude-sonnet-5-5"


def _model_spec_from_routing(provider: str, model_id: str) -> str:
    """model_routing_preferences row를 runner가 실행 가능한 model spec으로 변환."""
    provider_name = (provider or "").strip().lower()
    model_name = (model_id or "").strip()
    if not model_name:
        return ""
    if provider_name in {"codex", "openai"} and model_name.startswith("gpt-"):
        return f"codex:{model_name}"
    if provider_name == "anthropic":
        return model_name
    if provider_name in {"gemini", "google", "deepseek", "kimi", "minimax", "qwen", "groq", "openrouter", "litellm"}:
        return f"litellm:{model_name}"
    if ":" in model_name:
        return model_name
    return f"{provider_name}:{model_name}" if provider_name else model_name


async def _get_model_cycle_for_size(conn, size: str) -> list[str]:
    """size 설정 → AI_REVIEW 설정 → runner_llm 라우팅 순으로 폴백 체인을 만든다."""
    import json as _json_model
    from app.services.model_registry import filter_executable_models

    _size = (size or "M").upper()
    candidates: list[str] = []

    async def _append_config(config_size: str) -> None:
        row = await conn.fetchrow(
            "SELECT models FROM runner_model_config WHERE size = $1",
            config_size,
        )
        if not row or not row["models"]:
            return
        raw = row["models"]
        models = _json_model.loads(raw) if isinstance(raw, str) else raw
        candidates.extend(str(model).strip() for model in (models or []) if str(model).strip())

    await _append_config(_size)
    if _size != "AI_REVIEW":
        await _append_config("AI_REVIEW")

    routing_rows = await conn.fetch(
        """
        SELECT route_key, provider, model_id
        FROM model_routing_preferences
        WHERE route_key = 'runner_llm'
          AND is_enabled = TRUE
        ORDER BY is_default DESC,
                 display_order ASC,
                 provider ASC,
                 model_id ASC
        """
    )
    candidates.extend(
        _model_spec_from_routing(row["provider"], row["model_id"])
        for row in routing_rows
    )

    seen: set[str] = set()
    deduped: list[str] = []
    for model in candidates:
        normalized = str(model or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(normalized)
    return await filter_executable_models(deduped)



def _parse_size_from_instruction(instruction: str) -> str:
    """instruction 텍스트에서 규모 파싱 (AADS-206B 폴백)."""
    m = re.search(r'(?:규모|SIZE)[:\s=]*\s*(XL|XS|[SML])\b', instruction, re.IGNORECASE)
    return m.group(1).upper() if m else ""


def _estimate_size(instruction: str) -> str:
    """instruction 복잡도 자동 추정 (P1-2 AADS-229)."""
    text = instruction.lower()
    length = len(instruction)
    complex_kw = ["리팩토링", "마이그레이션", "아키텍처", "전체", "모든 파일",
                  "refactor", "migration", "architecture", "all files",
                  "다중 서버", "multi-server", "전수", "대규모"]
    simple_kw = ["오타", "typo", "주석", "comment", "버전", "version",
                 "설정 변경", "config", "로그", "log level", "1줄", "한 줄"]
    cx = sum(1 for kw in complex_kw if kw in text)
    sx = sum(1 for kw in simple_kw if kw in text)
    fr = len(__import__("re").findall(r'[\w/]+\.(?:py|ts|tsx|js|sh|sql|yml|yaml)', text))
    if sx >= 2 or (length < 200 and cx == 0 and fr <= 1):
        return "S"
    if cx >= 3 or fr >= 10 or length > 5000:
        return "XL"
    if cx >= 2 or fr >= 5 or length > 3000:
        return "L"
    return "M"


_VALID_JOB_SIZES = {"XS", "S", "M", "L", "XL"}


def _normalize_job_size(size: str | None) -> str:
    value = (size or "M").strip().upper()
    return value if value in _VALID_JOB_SIZES else "M"


def _resolve_job_size(size: str | None, instruction: str, *, size_explicit: bool) -> str:
    """Resolve runner size without downgrading the admin default M."""
    if size_explicit:
        return _normalize_job_size(size)
    return _parse_size_from_instruction(instruction) or "M"


class JobSubmitRequest(BaseModel):
    project: str = Field(..., description="프로젝트 코드")
    instruction: str = Field(..., max_length=50000, description="Claude Code에 전달할 지시")
    session_id: str = Field(..., description="채팅 세션 ID (필수 — 완료 보고 대상)")
    max_cycles: int = Field(3, ge=1, le=10, description="최대 검수 사이클")
    size: str = Field("M", description="작업 규모 (XS/S/M/L/XL) — 모델 자동 선택")
    worker_model: str = Field("", description="직접 모델 지정 (빈 문자열이면 size 기반 자동 선택)")
    worker_model_reason: str = Field("", max_length=500, description="직접 모델 지정 사유")
    parallel_group: str = Field("", description="병렬 실행 그룹 — 같은 그룹 내 작업은 동시 실행")
    depends_on: str = Field("", description="의존 작업 job_id ��� 해당 작업 완료 후에만 실행")
    # 목표 연결은 **명시할 때만** 이뤄진다. 비우면 어떤 목표에도 붙지 않는다(하위호환).
    # 지시서에 `GOAL_ID: <uuid>` / `MILESTONE_ID: <uuid>` 를 넣어도 동일하게 동작한다.
    goal_id: str = Field("", description="연결할 목표 UUID (선택) — 미지정 시 목표 연결 없음")
    milestone_id: str = Field("", description="연결할 마일스톤 UUID (선택, goal_id 와 함께)")

    @field_validator('goal_id', 'milestone_id')
    @classmethod
    def validate_goal_uuid(cls, v):
        if v and not _UUID_RE.match(v):
            raise ValueError("goal_id/milestone_id는 UUID 형식이어야 합니다")
        return v

    @field_validator('project')
    @classmethod
    def validate_project(cls, v):
        if v not in _VALID_PROJECTS:
            raise ValueError(f"허용 프로젝트: {', '.join(sorted(_VALID_PROJECTS))}")
        return v

    @field_validator('session_id')
    @classmethod
    def validate_session_id(cls, v):
        if not v or not _UUID_RE.match(v):
            raise ValueError("session_id는 필수이며 UUID 형식이어야 합니다")
        return v


class JobSubmitResponse(BaseModel):
    job_id: str
    status: str
    message: str
    canonical_gate: Optional[dict] = Field(None, description="정본 미등록 경고(그림자 모드, 선택)")

    @model_serializer(mode="wrap")
    def _omit_empty_canonical_gate(self, handler):
        # 데코레이터를 건드리지 않고 기존 응답 shape 를 유지한다: 경고가 없으면 키 자체를 뺀다.
        data = handler(self)
        if data.get("canonical_gate") is None:
            data.pop("canonical_gate", None)
        return data


def _normalize_worker_model_override(worker_model: str, reason: str) -> tuple[str, str]:
    """Require an explicit reason before persisting a worker_model override."""
    model = (worker_model or "").strip()
    override_reason = (reason or "").strip()
    if not model:
        return "", ""
    if not override_reason:
        logger.warning("pipeline_runner.worker_model_ignored_no_reason", worker_model=model)
        return "", ""
    return model, override_reason


class JobApproveRequest(BaseModel):
    action: str = Field(..., description="approve 또는 reject")
    feedback: str = Field("", max_length=2000, description="피드백")
    defer_screen_evidence: bool = Field(
        False, description="CEO 명시 승인 시에만: 화면 증거를 배포 후 검증으로 미룬다(기본 fail-closed)"
    )
    defer_reason: str = Field("", max_length=2000, description="defer 사유(10자 이상)")

    @field_validator('action')
    @classmethod
    def validate_action(cls, v):
        if v not in ("approve", "reject"):
            raise ValueError("action은 approve 또는 reject만 가능")
        return v


class PipelineReviewAdjudicateRequest(BaseModel):
    """Bound input from the originating chat session after reviewer exhaustion."""

    caller_session_id: str = Field(..., description="Tool executor-bound chat session UUID")
    expected_commit_sha: str = Field(..., min_length=7, max_length=64)
    expected_diff_sha256: str = Field(..., min_length=64, max_length=64)
    verdict: str = Field(..., description="APPROVE, REJECT, or UNKNOWN")
    findings: str = Field("", max_length=4000)

    @field_validator("caller_session_id")
    @classmethod
    def validate_caller_session_id(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not _UUID_RE.match(normalized):
            raise ValueError("caller_session_id must be a UUID")
        return normalized

    @field_validator("expected_commit_sha")
    @classmethod
    def validate_commit_sha(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not re.fullmatch(r"[0-9a-f]{7,64}", normalized):
            raise ValueError("expected_commit_sha must be a hexadecimal git SHA")
        return normalized

    @field_validator("expected_diff_sha256")
    @classmethod
    def validate_diff_sha256(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", normalized):
            raise ValueError("expected_diff_sha256 must be a SHA-256 digest")
        return normalized

    @field_validator("verdict")
    @classmethod
    def validate_verdict(cls, value: str) -> str:
        normalized = value.strip().upper()
        if normalized not in {"APPROVE", "REJECT", "UNKNOWN"}:
            raise ValueError("verdict must be APPROVE, REJECT, or UNKNOWN")
        return normalized


async def check_project_lock(conn, project: str, exclude_job_id: str | None = None, parallel_group: str = "", tenant_id: str = "") -> bool:
    """프로젝트에 실행 중인(running/claimed) 작업이 상한에 도달했는지 확인. True면 잠김.
    AADS-211: parallel_group이 지정되면 같은 그룹 내 작업은 동시 실행 허용."""
    max_concurrent = _max_concurrent_per_project()
    # parallel_group이 있으면 같은 그룹이 아닌 작업만 lock으로 간주
    if parallel_group:
        row = await conn.fetchrow(
            "SELECT count(*) as cnt FROM pipeline_jobs "
            "WHERE project = $1 AND tenant_id = $2::uuid AND status IN ('running', 'claimed') "
            "AND (parallel_group IS NULL OR parallel_group != $3)",
            project, tenant_id, parallel_group,
        )
        return (row["cnt"] or 0) >= max_concurrent
    if exclude_job_id:
        row = await conn.fetchrow(
            "SELECT count(*) as cnt FROM pipeline_jobs "
            "WHERE project = $1 AND tenant_id = $2::uuid AND status IN ('running', 'claimed') AND job_id != $3",
            project, tenant_id, exclude_job_id,
        )
    else:
        row = await conn.fetchrow(
            "SELECT count(*) as cnt FROM pipeline_jobs "
            "WHERE project = $1 AND tenant_id = $2::uuid AND status IN ('running', 'claimed')",
            project, tenant_id,
        )
    return (row["cnt"] or 0) >= max_concurrent


async def cascade_cleanup_orphans(conn, failed_job_id: str) -> int:
    """기존 공개 계약대로 정리된 고아 작업 수를 반환한다."""
    return len(await _cascade_cleanup_orphans_with_ids(conn, failed_job_id))


def _has_auto_file_dependency(logs: object) -> bool:
    """Whether ``depends_on`` only serializes a same-file write.

    ``depends_on`` is deliberately reused for the DB-level wait primitive, so
    its origin must be read from the durable submission event before deciding
    whether a failed parent makes the child meaningless.
    """
    if not isinstance(logs, list):
        return False
    return any(
        isinstance(entry, dict)
        and entry.get("event") == _AUTO_FILE_DEPENDENCY_EVENT
        for entry in logs
    )


async def _release_auto_file_dependency(conn, *, job_id: str, parent_id: str,
                                        parent_status: str, parent_error: str) -> None:
    """Release a file-lock wait after its parent terminates and wake the queue."""
    await conn.execute(
        """
        UPDATE pipeline_jobs
           SET depends_on = NULL,
               review_feedback = COALESCE(review_feedback, '') || $3,
               logs = COALESCE(logs, '[]'::jsonb) || jsonb_build_array(
                   jsonb_build_object(
                       'ts', NOW()::text,
                       'event', 'file_conflict_dependency_requeued',
                       'parent_job_id', $2,
                       'parent_status', $4,
                       'parent_error', $5
                   )
               ),
               updated_at = NOW()
         WHERE job_id = $1 AND status = 'queued'
        """,
        job_id,
        parent_id,
        f"\n[Runner Guard] file-lock parent {parent_id} ended {parent_status}; "
        "dependency released and job requeued at queue head",
        parent_status,
        (parent_error or "unknown")[:1000],
    )
    await conn.execute("SELECT pg_notify('pipeline_new_job', $1)", job_id)


async def _cancel_explicit_orphan(conn, *, job_id: str, parent_id: str,
                                  parent_status: str, parent_error: str) -> bool:
    """Cancel an explicit dependency and emit a durable, consumable alert."""
    detail = (
        f"orphaned_dependency: parent {parent_id} {parent_status}; "
        f"failure_reason={parent_error or 'unknown'}"
    )[:2000]
    updated = await conn.fetchrow(
        """
        UPDATE pipeline_jobs
           SET status = 'cancelled', phase = 'blocked_dependency',
               error_detail = $2,
               review_feedback = COALESCE(review_feedback, '') || $3,
               logs = COALESCE(logs, '[]'::jsonb) || jsonb_build_array(
                   jsonb_build_object(
                       'ts', NOW()::text,
                       'event', 'orphaned_dependency_alert',
                       'parent_job_id', $4,
                       'parent_status', $5,
                       'parent_failure_reason', $6,
                       'notification_required', true
                   )
               ),
               completed_at = NOW(), updated_at = NOW()
         WHERE job_id = $1 AND status = 'queued'
         RETURNING job_id
        """,
        job_id,
        detail,
        f"\n[Runner Guard] {detail}; notification queued",
        parent_id,
        parent_status,
        (parent_error or "unknown")[:1000],
    )
    if updated:
        # Dedicated NOTIFY lets the runner/ops listener surface this terminal
        # state instead of silently leaving it in a task panel.
        await conn.execute("SELECT pg_notify('pipeline_orphaned_dependency', $1)", detail)
    return bool(updated)


async def _cascade_cleanup_orphans_with_ids(conn, failed_job_id: str) -> list[str]:
    """실패한 작업에 의존하는 모든 queued 작업을 재귀적으로 blocked 처리.
    P1-A: 고아 방지 — 의존 트리 전체를 한 번에 정리.

    반환값은 정리된 job_id 목록이다(과거에는 건수였다). 호출부가 이 작업들의
    목표 링크도 cancelled/blocked_dependency 로 재조정해야 하기 때문이다.
    """
    cleaned: list[str] = []
    to_process = [failed_job_id]
    while to_process:
        current_id = to_process.pop(0)
        parent = await conn.fetchrow(
            "SELECT status, error_detail FROM pipeline_jobs WHERE job_id = $1", current_id,
        )
        parent_status = _record_get(parent, "status", "failed")
        parent_error = _record_get(parent, "error_detail", "unknown")
        children = await conn.fetch(
            "SELECT job_id, logs FROM pipeline_jobs WHERE depends_on = $1 AND status = 'queued'",
            current_id,
        )
        for child in children:
            child_id = child["job_id"]
            if _has_auto_file_dependency(_record_get(child, "logs", [])):
                await _release_auto_file_dependency(
                    conn, job_id=child_id, parent_id=current_id,
                    parent_status=parent_status, parent_error=parent_error,
                )
                logger.info("pipeline_runner.file_lock_dependency_requeued",
                            job_id=child_id, parent=current_id)
                continue
            if await _cancel_explicit_orphan(
                conn, job_id=child_id, parent_id=current_id,
                parent_status=parent_status, parent_error=parent_error,
            ):
                cleaned.append(child_id)
                to_process.append(child_id)
                logger.info("pipeline_runner.orphan_cascade_cleaned",
                            orphan_job_id=child_id, parent=current_id)
    if cleaned:
        logger.info("pipeline_runner.orphan_cascade_total", count=len(cleaned), root=failed_job_id)
    return cleaned


_SUPERSEDES_RE = re.compile(r"^\s*(?:SUPERSEDES|AUTO_REWORK_OF)\s*:\s*(.+)$", re.M)
_RUNNER_JOB_ID_RE = re.compile(r"runner-[0-9a-f]{8}")
_ORPHAN_RELINK_MAX = 10
_ORPHAN_RELINK_WINDOW_HOURS = 24


def parse_superseded_job_ids(instruction: str) -> list[str]:
    """지시서의 SUPERSEDES/AUTO_REWORK_OF 행에서 대체 대상 작업 id 를 뽑는다."""
    found: list[str] = []
    for line in _SUPERSEDES_RE.findall(instruction or ""):
        for jid in _RUNNER_JOB_ID_RE.findall(line):
            if jid not in found:
                found.append(jid)
    return found


async def _relink_superseded_orphans(conn, *, job_id: str, project: str,
                                     instruction: str, tenant_id: str) -> list[str]:
    """승계 작업이 제출되면 선행 작업 실패로 취소됐던 하위작업을 다시 연결한다.

    왜 필요한가. 선행 작업이 error 로 끝나면 ``_cascade_cleanup_orphans_with_ids``
    가 그에 의존한 queued 작업을 cancelled/blocked_dependency 로 닫는다. 그
    선행 작업을 대체하는 재작업이 뒤에 성공해도 닫힌 하위작업을 다시 여는
    경로가 없어, 목표 진행이 그 지점에서 영구히 멈춘다 — 2026-09-29
    runner-2ae121bc 가 배포 게이트에서 실패하자 runner-72c2a09d 가 그렇게
    사라졌다.

    되살리는 범위는 좁게 둔다. 이 작업이 대체한다고 명시한 선행 작업의
    하위작업이고, blocked_dependency 로 닫혔고, 최근 24시간 안이고, 최대
    10건이다. 사람이 취소한 작업(phase 가 blocked_dependency 가 아님)과 이
    작업이 대체 대상으로 적은 작업 자신은 건드리지 않는다.
    """
    superseded = parse_superseded_job_ids(instruction)
    if not superseded:
        return []
    skip = sorted(set(superseded) | {job_id})
    rows = await conn.fetch(
        """
        UPDATE pipeline_jobs
           SET depends_on = $1::text,
               status = 'queued', phase = 'queued',
               error_detail = NULL, completed_at = NULL,
               review_feedback = COALESCE(review_feedback, '') || $2::text,
               logs = COALESCE(logs, '[]'::jsonb) || jsonb_build_array(
                   jsonb_build_object(
                       'ts', NOW()::text,
                       'event', 'superseded_dependency_relinked',
                       'from_parent', depends_on,
                       'to_parent', $1::text
                   )
               ),
               updated_at = NOW()
         WHERE job_id IN (
                   SELECT job_id FROM pipeline_jobs
                    WHERE tenant_id = $3::uuid
                      AND project = $4
                      AND depends_on = ANY($5::text[])
                      AND NOT (job_id = ANY($6::text[]))
                      AND status = 'cancelled'
                      AND phase = 'blocked_dependency'
                      AND updated_at > NOW() - make_interval(hours => $7::int)
                    ORDER BY created_at
                    LIMIT $8::int
               )
        RETURNING job_id
        """,
        job_id,
        f"\n[Runner Guard] 선행 작업 실패로 취소됐던 작업을 승계 작업 {job_id} 에 다시 연결했습니다.",
        tenant_id,
        project,
        superseded,
        skip,
        _ORPHAN_RELINK_WINDOW_HOURS,
        _ORPHAN_RELINK_MAX,
    )
    relinked = [r["job_id"] for r in rows]
    for child_id in relinked:
        await conn.execute("SELECT pg_notify('pipeline_new_job', $1)", child_id)
    if relinked:
        logger.info("pipeline_runner.superseded_orphans_relinked",
                    job_id=job_id, project=project, relinked=relinked)
    return relinked


async def promote_next_queued(conn, project: str) -> str | None:
    """프로젝트 Lock 해제 후 다음 queued 작업 확인.
    AADS-211: depends_on이 설정된 작업은 의존 작업이 done일 때만 승격.
    P1-A: 의존 작업 실패 시 자동 고아 처리."""
    rows = await conn.fetch(
        "SELECT job_id, depends_on, parallel_group, logs, instruction FROM pipeline_jobs "
        "WHERE project = $1 AND status = 'queued' "
        "ORDER BY CASE WHEN instruction ~* '(^|[[:space:]])PRIORITY:[[:space:]]*P0' "
        "THEN 0 ELSE 1 END, CASE WHEN logs @> '[{\"event\": \"file_conflict_dependency_requeued\"}]'::jsonb "
        "THEN 0 ELSE 1 END, COALESCE(priority, 0) DESC, created_at ASC LIMIT 10",
        project,
    )
    for row in rows:
        dep = row["depends_on"]
        if dep:
            # 의존 작업 상태 확인
            dep_row = await conn.fetchrow(
                "SELECT status, error_detail FROM pipeline_jobs WHERE job_id = $1", dep,
            )
            if dep_row and dep_row["status"] in ("error", "rejected", "rejected_done", "cancelled"):
                parent_error = _record_get(dep_row, "error_detail", "unknown")
                if _has_auto_file_dependency(_record_get(row, "logs", [])):
                    await _release_auto_file_dependency(
                        conn, job_id=row["job_id"], parent_id=dep,
                        parent_status=dep_row["status"], parent_error=parent_error,
                    )
                    return row["job_id"]
                # Only a directive's explicit dependency remains an orphan.
                await _cancel_explicit_orphan(
                    conn, job_id=row["job_id"], parent_id=dep,
                    parent_status=dep_row["status"], parent_error=parent_error,
                )
                try:
                    from app.services.pipeline_runner_service import _reconcile_job_goal_links
                    await _reconcile_job_goal_links(row["job_id"])
                except Exception as exc:  # noqa: BLE001 — 승격 흐름을 막지 않는다
                    logger.warning("pipeline_runner.goal_state_update_fail",
                                   job_id=row["job_id"], error=str(exc))
                logger.info("pipeline_runner.orphan_auto_cleaned",
                            job_id=row["job_id"], parent=dep, parent_status=dep_row["status"])
                continue
            if not dep_row or dep_row["status"] != "done":
                logger.debug("pipeline_runner.dep_not_ready",
                             job_id=row["job_id"], depends_on=dep,
                             dep_status=dep_row["status"] if dep_row else "not_found")
                continue  # 의존 작업 미완료 → 스킵
        logger.info("pipeline_runner.lock_released_next_ready",
                     next_job_id=row["job_id"], project=project)
        return row["job_id"]
    return None


def _runner_display_status(
    status: str,
    phase: str | None,
    error_detail: str | None,
    auth_recovery_state: str | None = None,
) -> dict[str, object]:
    """UI가 terminal-but-not-error 상태를 빨간 실패로만 표시하지 않도록 분류한다."""
    phase = phase or ""
    error_detail = error_detail or ""
    candidates = (auth_recovery_state or "", phase, status, error_detail.split(":", 1)[0])
    for candidate in candidates:
        if candidate in _DISPLAY_STATUS_LABELS:
            display = {
                "display_status": candidate,
                "status_label": _DISPLAY_STATUS_LABELS[candidate],
                "status_group": _DISPLAY_STATUS_GROUPS[candidate],
                "auto_retryable": candidate in {"tool_timeout", "auth_recovery_pending"},
            }
            if candidate == "review_hold":
                display["approval_available"] = False
                display["action_hint"] = (
                    "AI 리뷰 인프라 재검수 대기 중입니다. CEO 승인 버튼은 "
                    "검수를 통과해 승인 대기로 전환된 뒤 나타납니다."
                )
            return display
    if status == "cancelled":
        return {"display_status": "cancelled", "status_label": "종결",
                "status_group": "blocked", "auto_retryable": False}
    group = "active" if status in ("queued", "claimed", "running", "deploying", "rolling_back") else "unknown"
    if status == "awaiting_approval":
        group = "action_required"
    elif status in ("done", "approved", "rejected_done"):
        group = "complete"
    elif status in ("error", "rejected"):
        group = "action_required"
    return {"display_status": status, "status_label": status,
            "status_group": group, "auto_retryable": False,
            "approval_available": status == "awaiting_approval"}


async def _runner_health_probe(conn, row) -> dict | None:
    status = _record_get(row, "status") or ""
    if status not in ("running", "claimed"):
        return None
    job_id = _record_get(row, "job_id") or ""
    project = _record_get(row, "project") or ""
    logs_row = await conn.fetchrow(
        "SELECT EXISTS(SELECT 1 FROM task_logs WHERE task_id = $1 LIMIT 1) AS has_logs",
        job_id,
    )
    has_logs = bool(logs_row and logs_row["has_logs"])
    pid = _record_get(row, "runner_pid")
    proc_alive = None
    proc_scope = "no_pid"
    if pid and _is_local_runner_project(project):
        proc_alive = _local_pid_alive(pid)
        proc_scope = "local_proc"
    elif pid:
        proc_scope = "remote_proc_not_checked_by_api"

    reasons = []
    if not has_logs:
        reasons.append("empty_task_logs")
    if proc_alive is False:
        reasons.append("dead_local_pid")
    if not reasons:
        return None
    return {
        "task_logs": "present" if has_logs else "empty",
        "runner_pid": pid,
        "proc_alive": proc_alive,
        "proc_scope": proc_scope,
        "suspect_stale": proc_alive is False,
        "reasons": reasons,
        "systemd": "not_checked_by_api",
    }


async def _cleanup_dead_local_runner_processes(conn, project: str, min_age_seconds: int = 120) -> int:
    """Clear dead local runner rows before submit/dedup decisions.

    Only local projects are mutated here. Remote-project runner PIDs are owned
    by their runner host and must be cleaned by the remote watchdog.
    """
    if not _is_local_runner_project(project):
        return 0
    rows = await conn.fetch(
        """
        SELECT job_id, runner_pid
        FROM pipeline_jobs
        WHERE project = $1
          AND status IN ('running', 'claimed')
          AND runner_pid IS NOT NULL
          AND updated_at < NOW() - ($2::int * INTERVAL '1 second')
        """,
        project,
        min_age_seconds,
    )
    cleaned = 0
    for row in rows:
        pid = _record_get(row, "runner_pid")
        if _local_pid_alive(pid) is not False:
            continue
        result = await conn.execute(
            """
            UPDATE pipeline_jobs
            SET status = 'error',
                phase = 'error',
                error_detail = 'process_died',
                runner_pid = NULL,
                review_feedback = COALESCE(review_feedback, '') || $2,
                updated_at = NOW()
            WHERE job_id = $1
              AND status IN ('running', 'claimed')
            """,
            row["job_id"],
            f"\n[API stale guard] Local runner process PID={pid} is not alive; marked error before dedup/lock.",
        )
        if result and result != "UPDATE 0":
            cleaned += 1
            logger.warning(
                "pipeline_runner.local_dead_pid_cleaned",
                job_id=row["job_id"],
                runner_pid=pid,
            )
    return cleaned


async def _persist_job_goal_context(
    pool, job_id: str, goal_id: str, milestone_id: str | None = None,
) -> None:
    """확정된 목표 컨텍스트를 pipeline_jobs 에 보존한다 (best-effort).

    migration 166 이전 이미지에서는 컬럼이 없어 실패할 수 있으므로 삼킨다 —
    실제 연결은 goal_task_links 가 담당하고, 이 컬럼은 출처 조회/재조정용이다.
    """
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE pipeline_jobs SET goal_id = $2::uuid, milestone_id = $3::uuid, updated_at = NOW() "
                "WHERE job_id = $1",
                job_id, goal_id, milestone_id,
            )
    except Exception as exc:  # noqa: BLE001 — 컬럼 부재/경합은 비치명적
        logger.debug("pipeline_runner.goal_context_persist_skipped", job_id=job_id, error=str(exc))


def _enforce_owner_resolved_gate(instruction: str, project: str) -> None:
    """Reject implementation submission before a roadmap slice has converged."""
    from app.services.pipeline_runner_service import _unresolved_owner_slices

    unresolved = _unresolved_owner_slices(instruction, project)
    if not unresolved:
        return
    summary = ", ".join(
        f"{item['slice_id']}={item['status']}" for item in unresolved
    )
    raise HTTPException(
        status_code=409,
        detail={
            "code": "owner_resolution_required",
            "message": (
                "analyze/converge가 끝나지 않은 정본 슬라이스는 구현 러너에 제출할 수 없습니다: "
                f"{summary}"
            ),
            "unresolved": unresolved,
        },
    )


async def _run_mockup_gate(
    context: TenantContext, *, phase: str, project: str, task_id: str, instruction: str, goal_id: str | None,
) -> dict:
    """Verify the approved mockup bundle named in the instruction. Unrelated tasks return without touching the DB.

    The gate runs in its own short transaction so a denial's audit row is committed even though the caller then
    answers 409. It only reads and records; it never stops or restarts any running job.
    """
    from app.api.canonical_documents import _authorize, _scope
    from app.core.db_pool import get_pool
    from app.services import mockup_gate_rules as rules
    from app.services import mockup_review_service as mockup

    info = rules.classify(instruction)
    if not info["applicable"]:
        return {"applicable": False, "allowed": True, "mode": info["mode"], "phase": phase, "reasons": []}
    project_key = project.upper()
    try:
        actor = _scope(context)[1]
        async with get_pool().acquire() as conn, conn.transaction():
            async def authorize() -> None:
                await _authorize(conn, context, project_key, "write")

            result = await mockup.gate_check(
                conn, phase=phase, tenant=_tenant_id(context), project=project_key, task_id=task_id,
                instruction=instruction, goal_id=goal_id, actor=actor, authorize=authorize)
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001 - pool exhausted / DB down
        logger.warning("mockup_gate_unavailable", phase=phase, task_id=task_id, exc_info=True)
        return {"applicable": True, "mode": info["mode"], "phase": phase, "ui_task": info["ui_task"],
                "declared": info["declared"], **rules.unavailable_decision(info)}
    if result["allowed"] and result.get("shadow"):
        logger.warning("mockup_gate_shadow", phase=phase, task_id=task_id, reasons=result["reasons"])
    return result


def _mockup_denial(result: dict) -> HTTPException:
    return HTTPException(status_code=409, detail={"code": "mockup_approval_required", **result})


class MockupGateRequest(BaseModel):
    phase: str = Field("pre_execution", pattern="^(pre_execution|checkpoint)$")


@router.post("/pipeline/jobs/{job_id}/mockup-gate", tags=["pipeline-runner"])
async def mockup_gate_for_job(
    job_id: str,
    body: MockupGateRequest | None = None,
    context: TenantContext = Depends(require_tenant_member),
):
    """Worker-side gate: re-verify the same approval bundle right before the worker starts (or at a checkpoint).

    Answers 200 {"allowed": true|false}; a worker that gets allowed=false must not start the job. The endpoint
    only reads the job row, so calling it can never change or stop a running job.
    """
    if not _JOB_ID_RE.match(job_id):
        raise HTTPException(status_code=422, detail="invalid job_id")
    from app.core.db_pool import get_pool
    async with get_pool().acquire() as conn:
        job = await conn.fetchrow(
            "SELECT project, instruction FROM pipeline_jobs WHERE job_id = $1 AND tenant_id = $2::uuid",
            job_id, _tenant_id(context))
    if not job:
        raise HTTPException(status_code=404, detail="job_not_found")
    from app.services import mockup_gate_rules as rules
    return await _run_mockup_gate(
        context, phase=(body.phase if body else "pre_execution"), project=job["project"], task_id=job_id,
        instruction=job["instruction"] or "", goal_id=rules.declared_goal_id(job["instruction"] or ""))


@router.post("/pipeline/jobs", response_model=JobSubmitResponse, tags=["pipeline-runner"])
async def submit_job(
    req: JobSubmitRequest,
    context: TenantContext = Depends(require_tenant_member),
):
    """작업 제출 — 같은 프로젝트에 running 작업이 있으면 queued 대기, 없으면 즉시 running."""
    _enforce_owner_resolved_gate(req.instruction, req.project)
    from app.core.db_pool import get_pool
    pool = get_pool()

    job_id = f"runner-{uuid.uuid4().hex[:8]}"
    session_id = req.session_id  # 필수 필드 — validator에서 이미 검증됨
    from app.services import mockup_gate_rules as _mockup_rules
    mockup_gate = await _run_mockup_gate(
        context, phase="submit", project=req.project, task_id=job_id, instruction=req.instruction,
        goal_id=req.goal_id or _mockup_rules.declared_goal_id(req.instruction))
    if not mockup_gate["allowed"]:
        raise _mockup_denial(mockup_gate)
    instruction_hash = _compute_instruction_hash(req.project, req.instruction)
    write_scope = _parse_write_scope(req.instruction)
    target_files = set(write_scope.write)
    _log_write_scope(write_scope, job_id=job_id, project=req.project)
    auto_depends_on = ""
    auto_dependency_reason = ""
    relinked_orphans: list[str] = []

    try:
        async with pool.acquire() as conn:
            # 트랜잭션으로 lock 체크 + INSERT 원자성 보장
            async with conn.transaction():
                tenant_id = _tenant_id(context)
                session_tenant = await conn.fetchval(
                    "SELECT tenant_id::text FROM chat_sessions WHERE id = $1::uuid AND tenant_id = $2::uuid",
                    session_id,
                    tenant_id,
                )
                if not session_tenant:
                    raise HTTPException(status_code=404, detail="세션을 찾을 수 없습니다")
                await _lock_instruction_hash(conn, instruction_hash)
                await _cleanup_dead_local_runner_processes(conn, req.project)
                # AADS-239: 중복 재사용 — 기존 작업 활용 (죽이기 → 재사용)
                # Step 1: 동일 hash + 활성 상태 → 기존 작업 정보 반환
                existing = await _find_active_duplicate(
                    conn,
                    req.project,
                    instruction_hash,
                    req.parallel_group,
                    tenant_id=tenant_id,
                )
                if existing:
                    detail = await _record_dedup_blocked(
                        conn,
                        job_id=job_id,
                        req=req,
                        instruction_hash=instruction_hash,
                        existing=existing,
                        tenant_id=tenant_id,
                    )
                    return JobSubmitResponse(
                        job_id=job_id,
                        status="dedup_blocked",
                        message=f"{detail}. 기존 작업을 계속 진행합니다.",
                    )
                # Step 2: 동일 hash + error + 2시간 내 → 기존 작업 queued로 리셋하여 재시도
                failed = await conn.fetchrow(
                    """
                    SELECT job_id FROM pipeline_jobs
                    WHERE instruction_hash = $1
                      AND tenant_id = $2::uuid
                      AND status = 'error'
                      AND created_at > NOW() - INTERVAL '2 hours'
                    ORDER BY created_at DESC LIMIT 1
                    FOR UPDATE
                    """,
                    instruction_hash,
                    tenant_id,
                )
                if failed:
                    await conn.execute(
                        "UPDATE pipeline_jobs SET status = 'queued', phase = 'queued', "
                        "error_detail = NULL, runner_pid = NULL, updated_at = NOW() "
                        "WHERE job_id = $1 AND tenant_id = $2::uuid",
                        failed["job_id"],
                        tenant_id,
                    )
                    await conn.execute("SELECT pg_notify('pipeline_new_job', $1)", failed["job_id"])
                    return JobSubmitResponse(
                        job_id=failed["job_id"],
                        status="retrying",
                        message=f"이전 실패 작업을 재시도합니다: {failed['job_id']}",
                    )
                locked = await check_project_lock(conn, req.project, parallel_group=req.parallel_group, tenant_id=tenant_id)
                worker_model, worker_model_reason = _normalize_worker_model_override(
                    req.worker_model,
                    req.worker_model_reason,
                )
                size = _resolve_job_size(
                    req.size,
                    req.instruction,
                    size_explicit="size" in req.model_fields_set,
                )
                # AADS-211: worker_model 직접 지정 시 model만 직접값 사용
                if worker_model:
                    model = worker_model
                else:
                    model = await _get_model_for_size(conn, size)
                # AADS-211: depends_on 유효성 검사
                if req.depends_on:
                    dep_row = await conn.fetchrow(
                        "SELECT job_id, status, instruction FROM pipeline_jobs "
                        "WHERE job_id = $1 AND tenant_id = $2::uuid",
                        req.depends_on,
                        tenant_id,
                    )
                    if not dep_row:
                        detail = await _record_blocked_dependency(
                            conn,
                            job_id=job_id,
                            req=req,
                            instruction_hash=instruction_hash,
                            dep_status="missing",
                            tenant_id=tenant_id,
                        )
                        return JobSubmitResponse(
                            job_id=job_id,
                            status="blocked_dependency",
                            message=detail,
                        )
                    # P1-B: 의존 작업이 이미 실패 상태이면 즉시 거부
                    if dep_row["status"] in _TERMINAL_BLOCKING_STATUSES:
                        detail = await _record_blocked_dependency(
                            conn,
                            job_id=job_id,
                            req=req,
                            instruction_hash=instruction_hash,
                            dep_status=dep_row["status"],
                            tenant_id=tenant_id,
                        )
                        return JobSubmitResponse(
                            job_id=job_id,
                            status="blocked_dependency",
                            message=detail,
                        )
                    child_order = await _resolve_milestone_order(
                        conn,
                        project=req.project,
                        tenant_id=tenant_id,
                        instruction=req.instruction,
                        goal_id=req.goal_id,
                        milestone_id=req.milestone_id,
                    )
                    parent_order = await _resolve_milestone_order(
                        conn,
                        project=req.project,
                        tenant_id=tenant_id,
                        instruction=dep_row["instruction"] or "",
                    )
                    if _dependency_order_is_inverted(child_order, parent_order):
                        conflict = {
                            "job_id": req.depends_on,
                            "incoming_sequence": child_order[2],
                            "parent_sequence": parent_order[2],
                        }
                        detail = _dependency_inversion_detail(job_id, conflict)
                        logger.warning(
                            "pipeline_runner.dependency_inversion_blocked",
                            job_id=job_id,
                            project=req.project,
                            depends_on=req.depends_on,
                            child_sequence=child_order[2],
                            parent_sequence=parent_order[2],
                        )
                        raise HTTPException(status_code=409, detail=detail)
                else:
                    conflict = await _find_active_file_conflict(
                        conn,
                        project=req.project,
                        target_files=target_files,
                        tenant_id=tenant_id,
                        incoming_instruction=req.instruction,
                        incoming_goal_id=req.goal_id,
                        incoming_milestone_id=req.milestone_id,
                    )
                    if conflict:
                        if conflict.get("dependency_inversion"):
                            detail = _dependency_inversion_detail(job_id, conflict)
                            logger.warning(
                                "pipeline_runner.auto_dependency_inversion_blocked",
                                job_id=job_id,
                                project=req.project,
                                depends_on=conflict["job_id"],
                                child_sequence=conflict["incoming_sequence"],
                                parent_sequence=conflict["parent_sequence"],
                                overlap=conflict["overlap"],
                            )
                            raise HTTPException(status_code=409, detail=detail)
                        auto_depends_on = conflict["job_id"]
                        overlap = ", ".join(conflict["overlap"])
                        auto_dependency_reason = (
                            f"[Runner Guard] 동일 파일 충돌 감지: {overlap}; "
                            f"{auto_depends_on} 완료 후 자동 실행"
                        )
                        logger.info(
                            "pipeline_runner.file_conflict_auto_dependency",
                            job_id=job_id,
                            project=req.project,
                            depends_on=auto_depends_on,
                            overlap=conflict["overlap"],
                        )
                effective_depends_on = req.depends_on or auto_depends_on or None
                await conn.execute(
                    """
                    INSERT INTO pipeline_jobs
                      (job_id, project, instruction, instruction_hash, chat_session_id,
                       status, phase, max_cycles, model, size,
                       worker_model, model_override_reason, parallel_group, depends_on,
                       review_feedback, logs, created_at, updated_at, tenant_id)
                    VALUES ($1, $2, $3, $4, $5, 'queued', 'queued', $6, $7, $8,
                            $9, $10, $11, $12::text,
                            $13::text,
                            CASE WHEN $13::text = '' THEN '[]'::jsonb ELSE jsonb_build_array(jsonb_build_object(
                              'ts', NOW()::text,
                              'event', 'file_conflict_auto_dependency',
                              'depends_on', $12::text
                            )) END,
                            NOW(), NOW(), $14::uuid)
                    """,
                    job_id, req.project, req.instruction, instruction_hash,
                    session_id, req.max_cycles, model, size,
                    worker_model or None, worker_model_reason or None,
                    req.parallel_group or None, effective_depends_on,
                    auto_dependency_reason,
                    tenant_id,
                )
                # P2-2: LISTEN/NOTIFY — 이벤트 드리븐 (asyncpg 소비자용)
                await conn.execute("SELECT pg_notify('pipeline_new_job', $1)", job_id)
                relinked_orphans = await _relink_superseded_orphans(
                    conn,
                    job_id=job_id,
                    project=req.project,
                    instruction=req.instruction,
                    tenant_id=tenant_id,
                )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("pipeline_runner.submit_fail", error=str(e))
        raise HTTPException(status_code=500, detail="작업 저장 실패")

    if locked:
        logger.info("pipeline_runner.job_queued_locked", job_id=job_id, project=req.project)
        msg = "프로젝트에 실행 중인 작업이 있어 대기열에 추가되었습니다. 현재 작업 완료 후 자동 실행됩니다."
    else:
        logger.info("pipeline_runner.job_submitted", job_id=job_id, project=req.project)
        msg = "작업이 대기열에 추가되었습니다. Runner가 곧 실행합니다."
    if auto_depends_on:
        msg += f" 동일 파일 충돌을 감지해 {auto_depends_on} 완료 후 실행되도록 자동 의존성을 부여했습니다."
    if relinked_orphans:
        msg += (
            f" 선행 작업 실패로 취소됐던 {len(relinked_orphans)}건을 이 작업에 다시 연결했습니다: "
            + ", ".join(relinked_orphans)
            + "."
        )
    if req.worker_model and not req.worker_model_reason:
        msg += " 직접 모델 지정은 사유가 없어 저장하지 않았고, 어드민 러너 모델 설정값을 사용합니다."

    # Goal link (best-effort): **명시적으로 지정된** 목표에만 연결한다.
    # goal_id/milestone_id 필드 또는 지시서의 GOAL_ID/MILESTONE_ID 메타데이터가 없으면
    # 어떤 목표에도 붙이지 않는다 (이전의 "프로젝트 첫 active 목표" 자동연결 폐기).
    try:
        from app.services.pipeline_runner_service import _link_job_to_goal_explicit
        goal_link = await _link_job_to_goal_explicit(
            job_id, req.project,
            instruction=req.instruction,
            goal_id=req.goal_id or None,
            milestone_id=req.milestone_id or None,
        )
        if goal_link:
            await _persist_job_goal_context(pool, job_id, goal_link.goal_id, goal_link.milestone_id)
            msg += f" 목표 {goal_link.goal_id} 에 연결되었습니다."
    except Exception as exc:
        goal_link = None
        logger.warning("pipeline_runner.goal_link_fail", job_id=job_id, error=str(exc))

    # 정본 게이트(그림자): 목표가 확정된 제출만 판정·기록한다. 제출 결과는 바꾸지 않는다.
    gate = None
    if goal_link:
        from app.services.canonical_gate import check_runner_submit
        gate = await check_runner_submit(
            goal_id=goal_link.goal_id, tenant_id=_tenant_id(context),
            project=req.project, job_id=job_id,
        )
    return JobSubmitResponse(job_id=job_id, status="queued", message=msg, canonical_gate=gate)


# ── 끝난 작업은 큐 테이블에 남지 않는다 (AADS-RUNNER-ARCHIVE-VISIBILITY) ──
# `pipeline_cleanup` 이 1시간마다 종료 작업(done/error/cancelled/rejected/
# rejected_done)을 `pipeline_jobs_archive` 로 옮긴다
# (app/services/pipeline_cleanup.py — 큐 테이블을 작게 유지하려는 원래 목적).
#
# 그런데 이 API 는 큐 테이블만 읽었다. 그래서 한두 시간 지나면
# **자기가 낸 작업이 통째로 사라진 것처럼** 보였다.
#
# 2026-09-16 실측: 세션 b749ff17 이 낸 5건(runner-168bac82 외)이 18:26 에 전부
# 아카이브로 옮겨졌다. `chat_session_id` 는 아카이브 안에 멀쩡히 남아 있었는데도
# `pipeline_runner_status` 는 0건을 냈고, 세션 연결이 끊긴 것처럼 읽혔다.
# **링크가 끊긴 게 아니라 보는 테이블이 하나 모자랐다.**
#
# 아카이브 행은 `row_data`(jsonb)에 원본 컬럼을 담고 있다. 다만 용량 때문에
# `git_diff`·`logs`·`result_output` 은 빼고 저장하므로 그 세 개는 복원되지 않는다.
_ARCHIVE_OMITTED_FIELDS = ("git_diff", "logs", "result_output")


async def _archive_table_exists(conn) -> bool:
    return bool(await conn.fetchval(
        "SELECT to_regclass('public.pipeline_jobs_archive') IS NOT NULL"
    ))


def _archived_job_item(data: dict, *, detail: bool) -> dict:
    """아카이브 row_data 를 API 응답 모양으로 되돌린다.

    타임스탬프는 jsonb 안에서 이미 ISO 문자열이므로 그대로 쓴다.
    """
    status = data.get("status") or ""
    phase = data.get("phase") or ""
    error_detail = data.get("error_detail")
    instruction = data.get("instruction") or ""
    item = {
        "job_id": data.get("job_id"),
        "project": data.get("project"),
        "instruction": instruction if detail else instruction[:200],
        "status": status,
        "phase": phase,
        "cycle": data.get("cycle"),
        "error_detail": error_detail,
        "created_at": data.get("created_at"),
        "updated_at": data.get("updated_at"),
        "started_at": data.get("started_at"),
        "depends_on": data.get("depends_on"),
        "chat_session_id": data.get("chat_session_id"),
        "model": data.get("model") or "",
        "worker_model": data.get("worker_model") or "",
        "actual_model": data.get("actual_model") or "",
        "size": data.get("size") or "M",
        "auth_recovery_state": data.get("auth_recovery_state") or "",
        "auth_recovery_metadata": data.get("auth_recovery_metadata") or {},
        # 조회한 쪽이 "왜 diff 가 없지" 로 다시 헤매지 않도록 출처를 밝힌다.
        "archived": True,
        "archive_note": (
            "종료 후 아카이브로 이관된 작업이다. "
            f"용량 때문에 {', '.join(_ARCHIVE_OMITTED_FIELDS)} 는 보관하지 않는다 — "
            "diff 는 커밋에서 복원하라."
        ),
        **_runner_display_status(status, phase, error_detail,
                                 data.get("auth_recovery_state")),
    }
    if detail:
        item.update({
            "max_cycles": data.get("max_cycles"),
            "result_output": "",
            "git_diff": "",
            "review_feedback": data.get("review_feedback"),
            "commit_hash": data.get("commit_hash"),
            "actual_changed_files": data.get("actual_changed_files") or [],
        })
    return item


async def _fetch_archived_job(conn, job_id: str, tenant_id: str) -> dict | None:
    if not await _archive_table_exists(conn):
        return None
    data = await conn.fetchval(
        """
        SELECT row_data FROM pipeline_jobs_archive
        WHERE job_id = $1 AND row_data->>'tenant_id' = $2
        """,
        job_id, tenant_id,
    )
    if not data:
        return None
    if isinstance(data, str):
        import json as _json
        data = _json.loads(data)
    return _archived_job_item(data, detail=True)


async def _fetch_archived_jobs(
    conn, *, tenant_id: str, status: str | None, project: str | None,
    session_id: str | None, limit: int, exclude_ids: set[str],
) -> list[dict]:
    """큐에서 빠진 작업을 목록에 채워 넣는다.

    세션 필터가 있을 때가 특히 중요하다 — 그때가 바로 "내 작업 어디 갔나" 를
    묻는 상황이고, 큐에는 최근 1시간 것만 남아 있다.
    """
    import json as _json
    if limit <= 0 or not await _archive_table_exists(conn):
        return []
    conditions = ["row_data->>'tenant_id' = $1"]
    params: list = [tenant_id]
    idx = 2
    if status:
        conditions.append(f"status = ${idx}")
        params.append(status)
        idx += 1
    if project:
        conditions.append(f"project = ${idx}")
        params.append(project)
        idx += 1
    if session_id:
        conditions.append(f"row_data->>'chat_session_id' = ${idx}")
        params.append(session_id)
        idx += 1
    rows = await conn.fetch(
        f"""
        SELECT row_data FROM pipeline_jobs_archive
        WHERE {' AND '.join(conditions)}
        ORDER BY created_at DESC
        LIMIT ${idx}
        """,
        *params, limit + len(exclude_ids),
    )
    out: list[dict] = []
    for r in rows:
        data = r["row_data"]
        if isinstance(data, str):
            data = _json.loads(data)
        if data.get("job_id") in exclude_ids:
            continue
        out.append(_archived_job_item(data, detail=False))
        if len(out) >= limit:
            break
    return out


@router.get("/pipeline/jobs", tags=["pipeline-runner"])
async def list_jobs(
    status: Optional[str] = Query(None, max_length=30),
    project: Optional[str] = Query(None, max_length=10),
    session_id: Optional[str] = Query(None, max_length=36),
    limit: int = Query(20, ge=1, le=100),
    context: TenantContext = Depends(require_tenant_viewer),
):
    """작업 목록 조회."""
    from app.core.db_pool import get_pool
    pool = get_pool()

    conditions = ["tenant_id = $1::uuid"]
    params = [_tenant_id(context)]
    idx = 2

    if status:
        conditions.append(f"status = ${idx}")
        params.append(status)
        idx += 1
    if project:
        if project not in _VALID_PROJECTS:
            raise HTTPException(status_code=400, detail="유효하지 않은 프로젝트")
        conditions.append(f"project = ${idx}")
        params.append(project)
        idx += 1
    if session_id:
        conditions.append(f"chat_session_id = ${idx}")
        params.append(session_id)
        idx += 1

    where = "WHERE " + " AND ".join(conditions) if conditions else ""

    async with pool.acquire() as conn:
        has_auth_state = await _pipeline_column_exists(conn, "auth_recovery_state")
        has_auth_metadata = await _pipeline_column_exists(conn, "auth_recovery_metadata")
        auth_state_expr = "auth_recovery_state" if has_auth_state else "NULL::text"
        auth_metadata_expr = "auth_recovery_metadata" if has_auth_metadata else "NULL::jsonb"
        rows = await conn.fetch(
            f"""
            SELECT job_id, project, instruction, status, phase, cycle,
                   error_detail, created_at, updated_at,
                   started_at, depends_on, chat_session_id, model, worker_model,
                   actual_model, size, runner_pid,
                   {auth_state_expr} AS auth_recovery_state,
                   {auth_metadata_expr} AS auth_recovery_metadata
            FROM pipeline_jobs
            {where}
            ORDER BY created_at DESC
            LIMIT ${idx}
            """,
            *params, limit,
        )

    results = []
    async with pool.acquire() as conn:
        for r in rows:
            item = {
                "job_id": r["job_id"],
                "project": r["project"],
                "instruction": r["instruction"][:200],
                "status": r["status"],
                "phase": r["phase"],
                "cycle": r["cycle"],
                "error_detail": _record_get(r, "error_detail"),
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
                "started_at": r["started_at"].isoformat() if _record_get(r, "started_at") else None,
                "depends_on": _record_get(r, "depends_on"),
                "model": _record_get(r, "model") or "",
                "worker_model": _record_get(r, "worker_model") or "",
                "actual_model": _record_get(r, "actual_model") or "",
                "size": _record_get(r, "size") or "M",
                "auth_recovery_state": _record_get(r, "auth_recovery_state") or "",
                "auth_recovery_metadata": _record_get(r, "auth_recovery_metadata") or {},
                **_runner_display_status(
                    r["status"],
                    r["phase"],
                    _record_get(r, "error_detail"),
                    _record_get(r, "auth_recovery_state"),
                ),
            }
            health_probe = await _runner_health_probe(conn, r)
            if health_probe:
                item["health_probe"] = health_probe
            results.append(item)

        # 큐가 모자라면 아카이브에서 채운다 — 끝난 작업은 1시간 뒤 큐에서 빠진다.
        if len(results) < limit:
            results.extend(await _fetch_archived_jobs(
                conn,
                tenant_id=_tenant_id(context),
                status=status,
                project=project,
                session_id=session_id,
                limit=limit - len(results),
                exclude_ids={item["job_id"] for item in results},
            ))
    return results


# project → 실행 서버 매핑. runner_host 가 아직 비어 있는 과거 행을 위한 폴백이다.
# 2026-09-12 기준 담당이 겹치지 않아 역산이 성립한다(contabo116=AADS,
# contabo14=GO100, cafe24_114=SF/NTV2/NAS). 담당이 바뀌면 이 매핑이 조용히
# 틀려지므로, 러너가 기록한 runner_host 가 있으면 항상 그쪽을 우선한다.
_PROJECT_HOST_FALLBACK = {
    "AADS": "contabo116",
    "GO100": "contabo14",
    "KIS": "contabo14",
    "SF": "cafe24_114",
    "NTV2": "cafe24_114",
    "NAS": "cafe24_114",
}


@router.get("/pipeline/runner/status", tags=["pipeline-runner"])
async def runner_status(
    window_hours: int = Query(1, ge=1, le=24),
    context: TenantContext = Depends(require_tenant_viewer),
):
    """서버별 러너 작업 현황.

    화면이 "어느 서버가 막혔나"를 즉시 보여주기 위한 집계다. 2026-09-12 에
    GO100 러너가 인증 실패로 6시간 동안 37건을 실패시켰는데, 현황을 보여주는
    곳이 없어 아무도 알아차리지 못했다.

    서버 가동 여부는 원격 systemctl 호출 없이 pipeline_runner_hosts 의
    하트비트로 판단한다. 원격 호출은 느리고 한 대가 응답하지 않으면 화면
    전체가 멈춘다.
    """
    from app.core.db_pool import get_pool

    pool = get_pool()
    tenant = _tenant_id(context)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT COALESCE(NULLIF(runner_host, ''), '') AS runner_host,
                   project,
                   status,
                   COUNT(*)::int AS cnt,
                   MAX(EXTRACT(EPOCH FROM (NOW() - COALESCE(started_at, created_at)))::int)
                       FILTER (WHERE status IN ('running', 'claimed')) AS oldest_sec
            FROM pipeline_jobs
            WHERE tenant_id = $1::uuid
              AND (status IN ('running', 'claimed')
                   OR updated_at > NOW() - ($2::int * INTERVAL '1 hour'))
            GROUP BY 1, 2, 3
            """,
            tenant,
            window_hours,
        )
        hosts = await conn.fetch(
            "SELECT host, projects, engine_mode, max_concurrent, "
            "EXTRACT(EPOCH FROM (NOW() - last_seen_at))::int AS seen_ago "
            "FROM pipeline_runner_hosts"
        )

    agg: dict = {}

    def _bucket(host: str) -> dict:
        return agg.setdefault(host, {
            "host": host, "running": 0, "done": 0, "error": 0,
            "cancelled": 0, "other": 0, "oldest_running_sec": 0,
            "projects": set(), "alive": None, "seen_ago_sec": None,
        })

    for row in rows:
        host = row["runner_host"] or _PROJECT_HOST_FALLBACK.get(row["project"], "unknown")
        bucket = _bucket(host)
        bucket["projects"].add(row["project"])
        status = row["status"]
        count = int(row["cnt"] or 0)
        if status in ("running", "claimed"):
            bucket["running"] += count
            bucket["oldest_running_sec"] = max(
                bucket["oldest_running_sec"], int(row["oldest_sec"] or 0))
        elif status == "done":
            bucket["done"] += count
        elif status in ("error", "failed"):
            bucket["error"] += count
        elif status == "cancelled":
            bucket["cancelled"] += count
        else:
            bucket["other"] += count

    for row in hosts:
        bucket = _bucket(row["host"])
        seen = int(row["seen_ago"] or 0)
        bucket["seen_ago_sec"] = seen
        # 하트비트 주기보다 넉넉히 잡는다. 한 주기 놓쳤다고 죽었다고 보면 안 된다.
        bucket["alive"] = seen < 600
        if row["projects"]:
            bucket["projects"].update(
                p.strip() for p in str(row["projects"]).split(",") if p.strip())
        bucket["engine_mode"] = row["engine_mode"] or ""
        bucket["max_concurrent"] = row["max_concurrent"]

    servers = []
    for bucket in agg.values():
        bucket["projects"] = sorted(bucket["projects"])
        servers.append(bucket)
    servers.sort(key=lambda b: (-b["error"], -b["running"], b["host"]))

    return {
        "window_hours": window_hours,
        "servers": servers,
        "totals": {
            "hosts": len(servers),
            "running": sum(b["running"] for b in servers),
            "error": sum(b["error"] for b in servers),
            "done": sum(b["done"] for b in servers),
            "cancelled": sum(b["cancelled"] for b in servers),
        },
    }


@router.get("/pipeline/runner/model-stats", tags=["pipeline-runner"])
async def get_runner_model_stats(
    days: int = Query(30, ge=1, le=180),
    project: Optional[str] = Query(None, max_length=10),
    context: TenantContext = Depends(require_tenant_viewer),
):
    """모델별 러너 작업 속도/완료율 통계."""
    if project and project not in _VALID_PROJECTS:
        raise HTTPException(status_code=400, detail="유효하지 않은 프로젝트")

    from app.core.db_pool import get_pool

    pool = get_pool()
    tenant_id = _tenant_id(context)
    conditions = ["tenant_id = $1::uuid", "created_at >= NOW() - ($2::int * INTERVAL '1 day')"]
    params: list[object] = [tenant_id, days]
    idx = 3
    event_project_filter = ""
    if project:
        conditions.append(f"project = ${idx}")
        event_project_filter = f"AND project = ${idx}"
        params.append(project)
        idx += 1
    where = " AND ".join(conditions)

    async with pool.acquire() as conn:
        has_completed_at = await _pipeline_column_exists(conn, "completed_at")
        finish_expr = "COALESCE(completed_at, updated_at)" if has_completed_at else "updated_at"
        rows = await conn.fetch(
            f"""
            SELECT
                project,
                COALESCE(NULLIF(actual_model, ''), NULLIF(model, ''), 'unknown') AS model_key,
                COALESCE(NULLIF(size, ''), 'M') AS size,
                COUNT(*)::int AS total_jobs,
                COUNT(*) FILTER (WHERE status = 'done')::int AS done_jobs,
                COUNT(*) FILTER (WHERE status = 'awaiting_approval')::int AS awaiting_approval_jobs,
                COUNT(*) FILTER (WHERE status = 'rejected_done')::int AS rejected_done_jobs,
                COUNT(*) FILTER (WHERE status = 'review_hold')::int AS review_hold_jobs,
                COUNT(*) FILTER (WHERE status = 'error')::int AS error_jobs,
                COUNT(*) FILTER (WHERE status IN ('queued','claimed','running','approved','deploying'))::int AS active_jobs,
                ROUND(100.0 * COUNT(*) FILTER (WHERE status = 'done') / NULLIF(COUNT(*), 0), 1) AS done_rate_pct,
                ROUND(100.0 * COUNT(*) FILTER (WHERE status IN ('done','awaiting_approval')) / NULLIF(COUNT(*), 0), 1) AS work_success_rate_pct,
                ROUND(AVG(EXTRACT(EPOCH FROM ({finish_expr} - COALESCE(started_at, created_at))))::numeric, 1) AS avg_seconds,
                ROUND(percentile_cont(0.5) WITHIN GROUP (
                    ORDER BY EXTRACT(EPOCH FROM ({finish_expr} - COALESCE(started_at, created_at)))
                )::numeric, 1) AS p50_seconds,
                ROUND(percentile_cont(0.9) WITHIN GROUP (
                    ORDER BY EXTRACT(EPOCH FROM ({finish_expr} - COALESCE(started_at, created_at)))
                )::numeric, 1) AS p90_seconds,
                MAX({finish_expr}) AS last_observed_at
            FROM pipeline_jobs
            WHERE {where}
              AND COALESCE(started_at, created_at) IS NOT NULL
            GROUP BY project, model_key, size
            ORDER BY total_jobs DESC, project ASC, model_key ASC, size ASC
            LIMIT 100
            """,
            *params,
        )
        event_rows = await conn.fetch(
            f"""
            SELECT
                project,
                COALESCE(NULLIF(actual_model, ''), NULLIF(model, ''), 'unknown') AS model_key,
                COALESCE(NULLIF(size, ''), 'M') AS size,
                COUNT(*) FILTER (WHERE event_type = 'model_attempt_started')::int AS attempts,
                COUNT(*) FILTER (
                    WHERE event_type = 'model_attempt_completed'
                      AND metadata->>'success' = 'true'
                )::int AS successful_attempts,
                ROUND(AVG(duration_ms) FILTER (
                    WHERE event_type = 'model_attempt_completed'
                      AND duration_ms IS NOT NULL
                )::numeric / 1000.0, 1) AS avg_attempt_seconds
            FROM pipeline_runner_events
            WHERE tenant_id = $1::uuid
              AND observed_at >= NOW() - ($2::int * INTERVAL '1 day')
              {event_project_filter}
            GROUP BY project, model_key, size
            ORDER BY attempts DESC, project ASC, model_key ASC, size ASC
            LIMIT 100
            """,
            *params,
        ) if await conn.fetchval("SELECT to_regclass('public.pipeline_runner_events') IS NOT NULL") else []

    attempt_by_model = {
        (row["project"], row["model_key"], row["size"]): dict(row)
        for row in event_rows
    }
    stats = []
    for row in rows:
        item = dict(row)
        last_seen = item.get("last_observed_at")
        if last_seen:
            item["last_observed_at"] = last_seen.isoformat()
        event_key = (item["project"], item["model_key"], item["size"])
        event_stats = attempt_by_model.get(event_key, {})
        item["attempts"] = event_stats.get("attempts", 0)
        item["successful_attempts"] = event_stats.get("successful_attempts", 0)
        item["avg_attempt_seconds"] = event_stats.get("avg_attempt_seconds")
        stats.append(item)
    return {"days": days, "project": project or "all", "stats": stats}


@router.get("/pipeline/failures/classification", tags=["pipeline-runner"])
async def get_failure_classification(
    days: int = Query(7, ge=1, le=180),
    project: Optional[str] = Query(None, max_length=10),
    context: TenantContext = Depends(require_tenant_viewer),
):
    """러너 실패(error/cancelled)를 3분류로 집계한다 (M1 실패 원인 계측).

    instruction_defect(지시서 결함) / infra(리뷰·CLI 인프라) / gate_block(정상 게이트 차단).
    분류 규칙 원본은 app/services/pipeline_failure_taxonomy.py 이고,
    이 API 는 migrations/182 가 만든 pipeline_failure_classification_v1 뷰만 읽는다.
    """
    if project and project not in _VALID_PROJECTS:
        raise HTTPException(status_code=400, detail="유효하지 않은 프로젝트")

    from app.core.db_pool import get_pool
    from app.services.pipeline_failure_taxonomy import (
        CLASS_LABELS_KO,
        CLASS_UNCLASSIFIED,
        VIEW_NAME,
    )

    pool = get_pool()
    tenant_id = _tenant_id(context)
    conditions = ["tenant_id = $1::uuid", "created_at >= NOW() - ($2::int * INTERVAL '1 day')"]
    params: list[object] = [tenant_id, days]
    if project:
        conditions.append("project = $3")
        params.append(project)
    where = " AND ".join(conditions)

    async with pool.acquire() as conn:
        view_exists = await conn.fetchval(
            "SELECT to_regclass($1) IS NOT NULL", f"public.{VIEW_NAME}"
        )
        if not view_exists:
            raise HTTPException(
                status_code=503,
                detail=f"{VIEW_NAME} 뷰가 없습니다 — migrations/182 적용이 필요합니다",
            )
        class_rows = await conn.fetch(
            f"""
            SELECT
                failure_class,
                COUNT(*)::int AS jobs,
                COUNT(*) FILTER (WHERE status = 'error')::int AS error_jobs,
                COUNT(*) FILTER (WHERE status = 'cancelled')::int AS cancelled_jobs,
                MAX(created_at) AS last_seen_at
            FROM {VIEW_NAME}
            WHERE {where}
            GROUP BY failure_class
            ORDER BY jobs DESC
            """,
            *params,
        )
        subtype_rows = await conn.fetch(
            f"""
            SELECT
                failure_class,
                failure_subtype,
                signal_source,
                COUNT(*)::int AS jobs,
                MAX(created_at) AS last_seen_at,
                (array_agg(signal_sample ORDER BY created_at DESC))[1] AS sample
            FROM {VIEW_NAME}
            WHERE {where}
            GROUP BY failure_class, failure_subtype, signal_source
            ORDER BY jobs DESC
            LIMIT 60
            """,
            *params,
        )
        project_rows = await conn.fetch(
            f"""
            SELECT
                project,
                failure_class,
                COUNT(*)::int AS jobs
            FROM {VIEW_NAME}
            WHERE {where}
            GROUP BY project, failure_class
            """,
            *params,
        )

    total = sum(row["jobs"] for row in class_rows)

    def _pct(value: int) -> float:
        return round(100.0 * value / total, 2) if total else 0.0

    classes = [
        {
            "failure_class": row["failure_class"],
            "label_ko": CLASS_LABELS_KO.get(row["failure_class"], row["failure_class"]),
            "jobs": row["jobs"],
            "pct": _pct(row["jobs"]),
            "error_jobs": row["error_jobs"],
            "cancelled_jobs": row["cancelled_jobs"],
            "last_seen_at": row["last_seen_at"].isoformat() if row["last_seen_at"] else None,
        }
        for row in class_rows
    ]
    subtypes = [
        {
            "failure_class": row["failure_class"],
            "failure_subtype": row["failure_subtype"],
            "signal_source": row["signal_source"],
            "jobs": row["jobs"],
            "pct": _pct(row["jobs"]),
            "last_seen_at": row["last_seen_at"].isoformat() if row["last_seen_at"] else None,
            "sample": row["sample"],
        }
        for row in subtype_rows
    ]

    by_project: dict[str, dict[str, object]] = {}
    for row in project_rows:
        key = row["project"] or "unknown"
        bucket = by_project.setdefault(key, {"project": key, "total": 0})
        bucket[row["failure_class"]] = row["jobs"]
        bucket["total"] = int(bucket["total"]) + row["jobs"]

    unclassified = next(
        (row["jobs"] for row in class_rows if row["failure_class"] == CLASS_UNCLASSIFIED), 0
    )
    return {
        "days": days,
        "project": project or "all",
        "total_failures": total,
        "unclassified_jobs": unclassified,
        "unclassified_pct": _pct(unclassified),
        "classes": classes,
        "subtypes": subtypes,
        "by_project": sorted(
            by_project.values(), key=lambda item: int(item["total"]), reverse=True
        ),
    }


@router.get("/pipeline/jobs/{job_id}", tags=["pipeline-runner"])
async def get_job(
    job_id: str,
    context: TenantContext = Depends(require_tenant_viewer),
):
    """작업 상세 조회."""
    if not _JOB_ID_RE.match(job_id) and not job_id.startswith("pc-"):
        raise HTTPException(status_code=400, detail="유효하지 않은 job_id 형식")

    from app.core.db_pool import get_pool
    pool = get_pool()

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM pipeline_jobs WHERE job_id = $1 AND tenant_id = $2::uuid",
            job_id,
            _tenant_id(context),
        )

    if not row:
        # 큐에 없으면 아카이브를 본다. 없어진 게 아니라 옮겨진 것이다.
        async with pool.acquire() as conn:
            archived = await _fetch_archived_job(conn, job_id, _tenant_id(context))
        if archived:
            return archived
        raise HTTPException(status_code=404, detail="작업을 찾을 수 없습니다")

    result = {
        "job_id": row["job_id"],
        "project": row["project"],
        "instruction": row["instruction"],
        "status": row["status"],
        "phase": row["phase"],
        "cycle": row["cycle"],
        "max_cycles": row["max_cycles"],
        "result_output": row["result_output"],
        "git_diff": (row["git_diff"] or "")[:5000],
        "review_feedback": row["review_feedback"],
        "error_detail": _record_get(row, "error_detail"),
        "model": _record_get(row, "model") or "",
        "worker_model": _record_get(row, "worker_model") or "",
        "actual_model": _record_get(row, "actual_model") or "",
        "actual_changed_files": _record_get(row, "actual_changed_files") or [],
        "size": _record_get(row, "size") or "M",
        "auth_recovery_state": _record_get(row, "auth_recovery_state") or "",
        "auth_recovery_metadata": _record_get(row, "auth_recovery_metadata") or {},
        **_runner_display_status(
            row["status"],
            row["phase"],
            _record_get(row, "error_detail"),
            _record_get(row, "auth_recovery_state"),
        ),
        "started_at": row["started_at"].isoformat() if _record_get(row, "started_at") else None,
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
    }
    async with pool.acquire() as conn:
        health_probe = await _runner_health_probe(conn, row)
        runner_events = []
        if await conn.fetchval("SELECT to_regclass('public.pipeline_runner_events') IS NOT NULL"):
            event_rows = await conn.fetch(
                """
                SELECT
                    event_type,
                    status,
                    phase,
                    COALESCE(NULLIF(actual_model, ''), NULLIF(model, ''), 'unknown') AS model_key,
                    COALESCE(NULLIF(size, ''), 'M') AS size,
                    duration_ms,
                    metadata,
                    observed_at
                FROM pipeline_runner_events
                WHERE job_id = $1 AND tenant_id = $2::uuid
                ORDER BY observed_at ASC, id ASC
                LIMIT 300
                """,
                job_id,
                _tenant_id(context),
            )
            for event in event_rows:
                observed_at = event["observed_at"]
                metadata = event["metadata"] or {}
                runner_events.append(
                    {
                        "event_type": event["event_type"],
                        "status": event["status"],
                        "phase": event["phase"],
                        "model": event["model_key"],
                        "size": event["size"],
                        "duration_ms": event["duration_ms"],
                        "metadata": metadata if isinstance(metadata, dict) else {},
                        "observed_at": observed_at.isoformat() if observed_at else None,
                    }
                )
    if health_probe:
        result["health_probe"] = health_probe
    result["runner_events"] = runner_events
    return result


async def _record_terminal_failure_candidate(row: object) -> None:
    """Index runner failures without changing the terminal transition."""
    status = str(row["status"] or "")  # type: ignore[index]
    phase = str(row["phase"] or "")  # type: ignore[index]
    is_code_failure = status == "error" and phase in {"error", "review_failed"}
    is_review_infra_failure = status == "review_hold" and phase == "review_hold"
    if not (is_code_failure or is_review_infra_failure):
        return

    job_id = str(row["job_id"] or "")  # type: ignore[index]
    project = str(row["project"] or "AADS")  # type: ignore[index]
    error_detail = str(row["error_detail"] or "")  # type: ignore[index]
    review_feedback = str(row["review_feedback"] or "")[-500:]  # type: ignore[index]
    probe = (
        f"error_detail: {error_detail}\n"
        f"review_feedback: {review_feedback}\n"
        f"job_id: {job_id}\nproject: {project}"
    )
    try:
        from app.services.error_book import bump_recurrence, match_error, record_candidate

        known = await match_error(probe)
        if known:
            for hit in known:
                await bump_recurrence(hit["error_key"])
        else:
            await record_candidate(
                probe,
                source=f"runner:{project}:{phase}",
                project=project,
                metadata={"job_id": job_id, "phase": phase, "status": status},
            )
    except Exception as exc:
        # 오류 사전 장애가 러너의 종료·승격 흐름을 바꾸면 안 된다.
        logger.debug(
            "pipeline_runner.error_book_hook_failed",
            job_id=job_id,
            error=str(exc)[:120],
        )


@router.post("/pipeline/jobs/{job_id}/notify", tags=["pipeline-runner"])
async def notify_completion(job_id: str):
    """Runner가 작업 완료 시 호출 — 채팅AI에 자동 반응 트리거."""
    if not _JOB_ID_RE.match(job_id) and not job_id.startswith("pc-"):
        raise HTTPException(status_code=400, detail="유효하지 않은 job_id")

    # FIX-3: 터미널 상태 체크 — 이미 완료된 작업은 중복 처리 방지
    from app.core.db_pool import get_pool
    pool = get_pool()

    async with pool.acquire() as conn:
        terminal_row = await conn.fetchrow(
            "SELECT status FROM pipeline_jobs WHERE job_id = $1", job_id
        )
    if not terminal_row:
        return {
            "status": "skipped",
            "reason": "not_found",
        }

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT job_id, project, status, phase, chat_session_id, error_detail, review_feedback, "
            "substring(result_output from 1 for 500) as output_preview, "
            "substring(instruction from 1 for 200) as instruction_preview "
            "FROM pipeline_jobs WHERE job_id = $1", job_id
        )

    if not row:
        raise HTTPException(status_code=404, detail="작업을 찾을 수 없습니다")

    status = row["status"]
    project = row["project"]

    await _record_terminal_failure_candidate(row)

    # 작업 완료/에러 시 같은 프로젝트의 다음 queued 작업을 자동 승격
    promoted_job_id = None
    orphaned_job_ids: list[str] = []
    if status in ("done", "error", "rejected", "rejected_done"):
        try:
            async with pool.acquire() as conn:
                # P1-A: 실패 시 재귀 고아 정리 후 승격
                if status in ("error", "rejected", "rejected_done"):
                    orphaned_job_ids = await _cascade_cleanup_orphans_with_ids(conn, job_id)
                promoted_job_id = await promote_next_queued(conn, project)
        except Exception as e:
            logger.warning("pipeline_runner.promote_fail", project=project, error=str(e))
        # 성공/실패 **모든** 종료 상태를 목표 그래프에 반영한다.
        # 이전에는 done 만 반영해서 error/rejected/cancelled 작업의 링크가
        # queued 로 남고 마일스톤이 영원히 미완료로 보였다.
        try:
            from app.services.pipeline_runner_service import _update_linked_goal_state_with_phase
            await _update_linked_goal_state_with_phase(job_id, status, row["phase"])
        except Exception as exc:
            logger.warning("pipeline_runner.goal_state_update_fail", job_id=job_id, error=str(exc))
        # 의존성 때문에 cancelled/blocked_dependency 로 정리된 작업들도 함께 반영.
        for orphan_job_id in orphaned_job_ids:
            try:
                from app.services.pipeline_runner_service import _reconcile_job_goal_links
                await _reconcile_job_goal_links(orphan_job_id)
            except Exception as exc:
                logger.warning(
                    "pipeline_runner.goal_state_update_fail", job_id=orphan_job_id, error=str(exc),
                )

    # 코드 리뷰 반려와 배포 게이트 실패는 같은 라운드 상한·opt-out 을 적용한다.
    from app.services.pipeline_auto_rework import is_request_changes_failure
    if is_request_changes_failure(status, row["phase"] or "", row["error_detail"] or ""):
        try:
            from app.services.pipeline_auto_rework import maybe_submit_auto_rework

            async with pool.acquire() as conn:
                rework = await maybe_submit_auto_rework(conn, job_id)
            if rework and rework.get("job_id") and not rework.get("skipped"):
                from app.services.pipeline_runner_service import _link_job_to_goal_explicit

                linked = await _link_job_to_goal_explicit(
                    rework["job_id"], rework["project"],
                    instruction=rework["instruction"],
                    goal_id=str(rework["goal_id"]) if rework.get("goal_id") else None,
                    milestone_id=str(rework["milestone_id"]) if rework.get("milestone_id") else None,
                )
                if linked:
                    await _persist_job_goal_context(
                        pool, rework["job_id"], linked.goal_id, linked.milestone_id,
                    )
                if not promoted_job_id:
                    async with pool.acquire() as conn:
                        promoted_job_id = await promote_next_queued(conn, project)
            elif rework:
                logger.info("pipeline_runner.auto_rework_skipped", job_id=job_id, **{
                    k: v for k, v in rework.items() if k in ("skipped", "rounds_max")
                })
        except Exception as exc:  # 재작업 실패가 종료 처리를 되돌리면 안 된다
            logger.warning("pipeline_runner.auto_rework_fail", job_id=job_id, error=str(exc)[:200])

    # AADS-STALE-TRIGGER-SUPPRESS-P1: terminal jobs must never re-enter the
    # approval/review notification path.  Keep the terminal-side effects above
    # (queue promotion and goal reconciliation), but stop before any chat task
    # or notification claim can be created.
    from app.services.pipeline_runner_service import TERMINAL_JOB_STATUSES

    if status in TERMINAL_JOB_STATUSES:
        logger.info("pipeline_runner.notify_terminal_suppressed", job_id=job_id, status=status)
        return {
            "status": "skipped",
            "reason": f"terminal status: {status}",
            "promoted_job_id": promoted_job_id,
        }

    session_id = row["chat_session_id"]
    if not session_id or not _UUID_RE.match(session_id):
        return {"status": "skipped", "reason": "session_id 없음", "promoted_job_id": promoted_job_id}
    instruction = row["instruction_preview"] or ""
    output = row["output_preview"] or ""

    if status == "awaiting_approval":
        async with pool.acquire() as conn:
            notify_claimed = await conn.fetchrow(
                """
                UPDATE pipeline_jobs
                SET logs = COALESCE(logs, '[]'::jsonb) || jsonb_build_array(
                    jsonb_build_object(
                        'ts', NOW()::text,
                        'event', 'notify_ai',
                        'status', 'awaiting_approval',
                        'source', 'pipeline_notify'
                    )
                )
                WHERE job_id = $1
                  AND status = 'awaiting_approval'
                  AND NOT EXISTS (
                    SELECT 1
                    FROM jsonb_array_elements(COALESCE(logs, '[]'::jsonb)) AS log
                    WHERE log->>'event' = 'notify_ai'
                      AND log->>'status' = 'awaiting_approval'
                  )
                RETURNING job_id
                """,
                job_id,
            )
        if not notify_claimed:
            return {
                "status": "skipped",
                "reason": "awaiting_approval already notified",
                "session_id": session_id,
                "promoted_job_id": promoted_job_id,
            }
        # Keep the visible runner state in the task panel, while delivering an
        # internal action trigger to the owning chat AI. trigger_ai_reaction()
        # defers durably when a CEO response is already running and starts
        # immediately when the session is idle. The notify_ai claim above keeps
        # the same awaiting-approval transition at-most-once.
        msg = (
            f"[시스템] Pipeline Runner 작업이 AI 검수 대기 상태입니다.\n\n"
            f"**Job**: {job_id}\n**프로젝트**: {project}\n"
            f"**원 지시**: {instruction[:200]}\n"
            f"**실행 결과**: {output[:300]}\n\n"
            "작업 패널의 diff·테스트·변경 파일·승인 메타데이터를 실제 도구로 검수하고, "
            "이상이 없으면 승인 도구를 호출하십시오. 문제가 있으면 구체적인 근거로 반려하고 "
            "안전한 후속 조치를 이어서 수행하십시오. 진행 중인 CEO 응답이나 추가 지시는 중단하지 마십시오."
        )
    # 아래 done/error/else 분기는 종결 가드(TERMINAL_JOB_STATUSES) 때문에 현재는 도달하지 않지만 보존한다.
    elif status == "done":
        msg = (f"[시스템] Pipeline Runner 작업 배포 완료\n\n"
               f"**Job**: {job_id}\n**프로젝트**: {project}\n"
               f"**결과**:\n{output[:300]}\n\n"
               f"**배포 검증 5단계 필수 수행:**\n"
               f"1. 컨테이너 상태 확인 (docker ps로 healthy 확인)\n"
               f"2. 변경 파일 반영 확인 (read_remote_file로 핵심 수정 라인 확인)\n"
               f"3. API 헬스체크 (health_check 또는 curl)\n"
               f"4. DB 데이터 정합성 (query_database로 관련 수치 실측 확인)\n"
               f"5. 프론트엔드 변경 시 UI 확인 (browser_snapshot 또는 capture_screenshot)\n"
               f"각 단계를 도구로 실제 확인한 후 결과를 CEO에게 보고하세요. 도구 호출 없이 '정상 완료' 보고 금지.")
    elif status == "error":
        error_detail = _record_get(row, "error_detail") or "unknown"
        msg = (f"[시스템] Pipeline Runner 작업 실패\n\n"
               f"**Job**: {job_id}\n**프로젝트**: {project}\n"
               f"**에러 분류**: {error_detail}\n"
               f"**에러**:\n{output[:300]}\n\n"
               f"원인을 진단하고 조치하세요.")
    else:
        msg = f"[시스템] Pipeline Runner 작업 상태 변경: {job_id} → {status}"

    try:
        from app.services.chat_service import trigger_ai_reaction
        from app.services.ohvis_task_manager import create_task as _ohvis_create
        import asyncio
        logger.info("pipeline_runner.trigger_sent", job_id=job_id, session_id=session_id, status=status)

        async def _trigger_with_ohvis():
            # The job can become terminal after the notification claim but
            # before this background task runs.  Re-read it immediately before
            # delivery so a stale approval/review trigger is never sent.
            async with pool.acquire() as conn:
                current_status = await conn.fetchval(
                    "SELECT status FROM pipeline_jobs WHERE job_id = $1", job_id
                )
                if current_status in TERMINAL_JOB_STATUSES:
                    await conn.execute(
                        """
                        UPDATE pipeline_jobs
                        SET logs = COALESCE(logs, '[]'::jsonb) || jsonb_build_array(
                            jsonb_build_object(
                                'ts', NOW()::text,
                                'event', 'notify_ai_suppressed',
                                'status', $2::text,
                                'source', 'pipeline_notify_terminal_guard'
                            )
                        )
                        WHERE job_id = $1
                        """,
                        job_id,
                        current_status,
                    )
                    logger.info(
                        "pipeline_runner.notify_terminal_suppressed",
                        job_id=job_id,
                        status=current_status,
                    )
                    return
            _otid = None
            try:
                _otid = await _ohvis_create(
                    session_id=session_id,
                    title=f"Runner {status}: {job_id}",
                    task_type="runner",
                    runner_job_id=job_id,
                )
            except Exception as _oe:
                logger.warning("ohvis_create_before_trigger: %s", _oe)
            await trigger_ai_reaction(session_id, msg, ohvis_task_id=_otid)

        asyncio.create_task(_trigger_with_ohvis())
        return {"status": "triggered", "session_id": session_id, "promoted_job_id": promoted_job_id}
    except Exception as e:
        logger.warning(f"notify_trigger_failed: {e}")
        return {"status": "error", "detail": str(e), "promoted_job_id": promoted_job_id}


def _is_deploy_only_instruction(instruction: str) -> bool:
    lines = (instruction or "").splitlines()[:20]
    return "DEPLOY_ONLY: true" in "\n".join(lines)


@router.post("/pipeline/jobs/{job_id}/approve", tags=["pipeline-runner"])
async def approve_or_reject(
    job_id: str,
    req: JobApproveRequest,
    context: TenantContext = Depends(require_tenant_member),
):
    """작업 승인/거부 — Runner가 감지하여 배포 또는 롤백."""
    if not _JOB_ID_RE.match(job_id) and not job_id.startswith("pc-"):
        raise HTTPException(status_code=400, detail="유효하지 않은 job_id 형식")

    from app.core.db_pool import get_pool
    pool = get_pool()
    tenant_id = _tenant_id(context)

    async with pool.acquire() as conn:
        async with conn.transaction():
            has_commit_hash = await _pipeline_column_exists(conn, "commit_hash")
            has_actual_files = await _pipeline_column_exists(conn, "actual_changed_files")
            has_approved_at = await _pipeline_column_exists(conn, "approved_at")
            has_rejected_at = await _pipeline_column_exists(conn, "rejected_at")
            commit_hash_expr = "commit_hash" if has_commit_hash else "NULL::text"
            actual_files_expr = "actual_changed_files" if has_actual_files else "'[]'::jsonb"
            decision_ts_clause = ""
            if req.action == "approve" and has_approved_at:
                decision_ts_clause = "approved_at = NOW(),"
            elif req.action == "reject" and has_rejected_at:
                decision_ts_clause = "rejected_at = NOW(),"
            row = await conn.fetchrow(
                f"""
                SELECT job_id, project, status, phase, git_diff, instruction,
                       {commit_hash_expr} AS commit_hash,
                       {actual_files_expr} AS actual_changed_files
                FROM pipeline_jobs
                WHERE job_id = $1 AND tenant_id = $2::uuid
                FOR UPDATE
                """,
                job_id,
                tenant_id,
            )
            if not row or row["status"] != "awaiting_approval":
                raise HTTPException(status_code=400, detail="승인 대기 상태가 아닙니다")

            latest_review = None
            if req.action == "approve":
                from app.services.e2e_verify import assert_screen_evidence_gate
                gate_files = row["actual_changed_files"] or []
                if isinstance(gate_files, str):
                    try:
                        gate_files = json.loads(gate_files)
                    except json.JSONDecodeError:
                        gate_files = []
                try:
                    await assert_screen_evidence_gate(
                        conn,
                        job_id=job_id,
                        instruction=row["instruction"] or "",
                        changed_files=list(gate_files),
                        defer_screen_evidence=req.defer_screen_evidence,
                        defer_reason=req.defer_reason,
                        approver=str(context.get("user", {}).get("user_id") or tenant_id),  # type: ignore[union-attr]
                    )
                except ValueError as exc:
                    raise HTTPException(status_code=409, detail=str(exc)) from exc
                deploy_only = _is_deploy_only_instruction(row["instruction"])
                git_diff = row["git_diff"] or ""
                commit_hash = (row["commit_hash"] or "").strip()
                changed_files = row["actual_changed_files"] or []
                if not deploy_only and "diff --git " not in git_diff:
                    raise HTTPException(status_code=409, detail="승인 차단: 유효한 git diff가 없습니다")
                if not deploy_only and not re.match(r"^[0-9a-f]{40}$", commit_hash):
                    raise HTTPException(status_code=409, detail="승인 차단: 승인용 commit_hash가 없습니다")
                if not deploy_only and not changed_files:
                    raise HTTPException(status_code=409, detail="승인 차단: 실제 변경 파일 목록이 없습니다")
                latest_review = await conn.fetchrow(
                    """
                    SELECT verdict, score, flag_category, needs_retry
                    FROM code_reviews
                    WHERE job_id = $1
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    job_id,
                )
                if not deploy_only:
                    if not latest_review:
                        raise HTTPException(status_code=409, detail="승인 차단: AI 리뷰 결과가 없습니다")
                    if latest_review["verdict"] != "APPROVE":
                        detail = (
                            f"승인 차단: AI 리뷰 미통과 "
                            f"({latest_review['verdict']}, score={latest_review['score']})"
                        )
                        if latest_review["flag_category"]:
                            detail += f", category={latest_review['flag_category']}"
                        raise HTTPException(status_code=409, detail=detail)

            result = await conn.execute(
                f"""
                UPDATE pipeline_jobs
                SET status = $2,
                    review_feedback = COALESCE(review_feedback, '') || E'\n[CEO] ' || $3,
                    logs = COALESCE(logs, '[]'::jsonb) || jsonb_build_array(jsonb_build_object(
                        'ts', NOW()::text,
                        'event', 'approval_decision',
                        'action', $4::text,
                        'actor', $5::text,
                        'review_verdict', $6::text,
                        'review_score', $7::text
                    )),
                    {decision_ts_clause}
                    updated_at = NOW()
                WHERE job_id = $1 AND tenant_id = $8::uuid AND status = 'awaiting_approval'
                """,
                job_id,
                "approved" if req.action == "approve" else "rejected",
                req.feedback or req.action,
                req.action,
                str(context.get("user", {}).get("user_id") or "unknown"),  # type: ignore[union-attr]
                latest_review["verdict"] if latest_review else None,
                str(latest_review["score"]) if latest_review else None,
                tenant_id,
            )
            if await conn.fetchval("SELECT to_regclass('public.pipeline_runner_events') IS NOT NULL"):
                await conn.execute(
                    """
                    INSERT INTO pipeline_runner_events
                      (job_id, tenant_id, project, event_type, status, phase, model, actual_model, size, metadata)
                    SELECT job_id, tenant_id, project,
                           'approval_decision',
                           $2,
                           $2,
                           NULLIF(model, ''),
                           NULLIF(actual_model, ''),
                           NULLIF(size, ''),
                           jsonb_build_object(
                               'action', $3::text,
                               'actor', $4::text,
                               'review_verdict', $5::text,
                               'review_score', $6::text
                           )
                    FROM pipeline_jobs
                    WHERE job_id = $1 AND tenant_id = $7::uuid
                    """,
                    job_id,
                    "approved" if req.action == "approve" else "rejected",
                    req.action,
                    str(context.get("user", {}).get("user_id") or "unknown"),  # type: ignore[union-attr]
                    latest_review["verdict"] if latest_review else None,
                    str(latest_review["score"]) if latest_review else None,
                    tenant_id,
                )

    affected = int(result.split()[-1]) if result else 0
    if affected == 0:
        raise HTTPException(status_code=409, detail="승인 처리 중 상태가 변경되었습니다")

    # CEO 승인/거부도 durable 상태 write 다 — 목표 링크에 즉시 반영한다.
    # (approved → completed, rejected → failed 로 정규화)
    try:
        from app.services.pipeline_runner_service import _update_linked_goal_state
        await _update_linked_goal_state(
            job_id, "approved" if req.action == "approve" else "rejected",
        )
    except Exception as exc:
        logger.warning("pipeline_runner.goal_state_update_fail", job_id=job_id, error=str(exc))

    action_kr = "승인됨" if req.action == "approve" else "거부됨"
    logger.info("pipeline_runner.job_action", job_id=job_id, action=req.action)

    # autonomy_stats 기록 (자율성 데이터 축적)
    try:
        from app.services.autonomy_gate import record_task_result
        async with pool.acquire() as conn:
            job_row = await conn.fetchrow(
                "SELECT project FROM pipeline_jobs WHERE job_id = $1 AND tenant_id = $2::uuid",
                job_id,
                tenant_id,
            )
            if job_row:
                if req.action == "approve":
                    await record_task_result(
                        conn,
                        task_type="pipeline_runner",
                        task_id=job_id,
                        judge_verdict="pass",
                        user_modified=False,
                        project_id=job_row["project"],
                    )
                else:
                    await record_task_result(
                        conn,
                        task_type="pipeline_runner",
                        task_id=job_id,
                        judge_verdict="fail",
                        user_modified=True,
                        project_id=job_row["project"],
                    )
    except Exception as e:
        if req.action == "approve":
            logger.warning(f"autonomy_record_on_approve_failed: {e}")
        else:
            logger.warning(f"autonomy_record_on_reject_failed: {e}")

    return {"job_id": job_id, "action": req.action, "message": f"작업이 {action_kr}"}


@router.post("/pipeline/jobs/{job_id}/retry-review", tags=["pipeline-runner"])
async def retry_review(
    job_id: str,
    context: TenantContext = Depends(require_tenant_member),
):
    """review_hold 상태인 작업을 재검수. 통과 시 awaiting_approval로 전이."""
    if not _JOB_ID_RE.match(job_id):
        raise HTTPException(status_code=400, detail="유효하지 않은 job_id 형식")

    from app.core.db_pool import get_pool
    from app.services.code_reviewer import review_code_diff

    pool = get_pool()
    tenant_id = _tenant_id(context)

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT job_id, project, status, git_diff, instruction,
                   chat_session_id, review_flag_category, error_detail
            FROM pipeline_jobs
            WHERE job_id = $1 AND tenant_id = $2::uuid
            """,
            job_id,
            tenant_id,
        )
        if not row:
            raise HTTPException(status_code=404, detail="작업을 찾을 수 없습니다")
        if row["status"] != "review_hold":
            raise HTTPException(
                status_code=400,
                detail=f"review_hold 상태가 아닙니다 (현재: {row['status']})",
            )

        git_diff = row["git_diff"] or ""
        if not git_diff.strip():
            raise HTTPException(status_code=409, detail="재검수 차단: 저장된 git diff가 없습니다")

        verdict = await review_code_diff(
            project=row["project"],
            job_id=job_id,
            diff=git_diff,
            instruction=row["instruction"] or "",
        )

        if verdict.verdict == "APPROVE":
            await conn.execute(
                """
                UPDATE pipeline_jobs
                SET status = 'awaiting_approval',
                    phase = 'awaiting_approval',
                    review_verdict = $2,
                    review_score = $3,
                    review_flag_category = NULL,
                    review_needs_retry = FALSE,
                    review_feedback = COALESCE(review_feedback, '') || E'\n[재검수] PASS — ' || $4,
                    error_detail = NULL,
                    updated_at = NOW()
                WHERE job_id = $1
                """,
                job_id,
                verdict.verdict,
                verdict.score,
                verdict.feedback.get("summary", "재검수 통과"),
            )
            return {
                "job_id": job_id,
                "result": "approved",
                "verdict": verdict.verdict,
                "score": verdict.score,
                "message": "재검수 통과 — awaiting_approval 전이 완료",
            }
        else:
            category = verdict.flag_category or "UNKNOWN"
            summary = verdict.feedback.get("summary", "재검수 실패")
            await conn.execute(
                """
                UPDATE pipeline_jobs
                SET review_verdict = $2,
                    review_score = $3,
                    review_flag_category = $4,
                    review_feedback = COALESCE(review_feedback, '') || E'\n[재검수 재실패] ' || $5,
                    updated_at = NOW()
                WHERE job_id = $1
                """,
                job_id,
                verdict.verdict,
                verdict.score,
                category,
                f"{verdict.verdict} (score={verdict.score}) — {summary}",
            )
            return {
                "job_id": job_id,
                "result": "still_held",
                "verdict": verdict.verdict,
                "score": verdict.score,
                "flag_category": category,
                "message": f"재검수 미통과 — review_hold 유지 ({category})",
            }


@router.post("/pipeline/jobs/{job_id}/adjudicate-review", tags=["pipeline-runner"])
async def adjudicate_review_from_origin_session(
    job_id: str,
    req: PipelineReviewAdjudicateRequest,
    context: TenantContext = Depends(require_tenant_member),
):
    """Accept a bounded, read-only review verdict from the originating session.

    This is not deployment approval. APPROVE only moves a verified artifact to
    ``awaiting_approval``; push/deploy remain behind the existing approval gate.
    """
    if not _JOB_ID_RE.match(job_id):
        raise HTTPException(status_code=400, detail="유효하지 않은 job_id 형식")

    from app.core.db_pool import get_pool

    pool = get_pool()
    caller_tenant_id = _tenant_id(context)
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                SELECT j.job_id, j.project, j.status, j.chat_session_id,
                       j.tenant_id::text AS tenant_id, j.commit_hash,
                       j.git_diff, j.review_flag_category,
                       COALESCE(j.review_retry_count, 0) AS review_retry_count,
                       s.tenant_id::text AS session_tenant_id
                  FROM pipeline_jobs j
                  JOIN chat_sessions s ON s.id::text = j.chat_session_id
                 WHERE j.job_id = $1
                 FOR UPDATE OF j
                """,
                job_id,
            )
            if not row:
                raise HTTPException(status_code=404, detail="작업을 찾을 수 없습니다")
            if row["status"] != "review_hold":
                raise HTTPException(status_code=409, detail="review_hold 상태가 아닙니다")
            if str(row["chat_session_id"] or "").lower() != req.caller_session_id:
                raise HTTPException(status_code=403, detail="원 세션만 검수 판정을 제출할 수 있습니다")
            if (
                not row["tenant_id"]
                or row["tenant_id"] != row["session_tenant_id"]
                or row["tenant_id"] != caller_tenant_id
            ):
                raise HTTPException(status_code=409, detail="작업과 원 세션의 tenant가 일치하지 않습니다")
            if row["review_flag_category"] not in {
                "REVIEW_API_UNAVAILABLE",
                "REVIEW_MODEL_NO_RESPONSE",
                "REVIEW_PARSER_FAILURE",
                "REVIEW_TIMEOUT",
            }:
                raise HTTPException(status_code=409, detail="리뷰 인프라 장애 작업만 원 세션 폴백이 가능합니다")

            try:
                retry_threshold = max(
                    1,
                    int(os.getenv("REVIEW_ORIGIN_ADJUDICATION_RETRY_THRESHOLD", "3")),
                )
            except ValueError:
                retry_threshold = 3
            if int(row["review_retry_count"] or 0) < retry_threshold:
                raise HTTPException(status_code=409, detail="자동 재검수 상한에 도달하지 않았습니다")

            commit_sha = str(row["commit_hash"] or "").lower()
            diff_text = row["git_diff"] or ""
            diff_sha256 = hashlib.sha256(diff_text.encode("utf-8")).hexdigest()
            if commit_sha != req.expected_commit_sha or diff_sha256 != req.expected_diff_sha256:
                raise HTTPException(status_code=409, detail="검수 대상 SHA 또는 diff hash가 변경되었습니다")

            findings = req.findings.strip() or "원 세션 read-only 판정"
            if req.verdict == "APPROVE":
                result = await conn.execute(
                    """
                    UPDATE pipeline_jobs
                       SET status='awaiting_approval', phase='awaiting_approval',
                           review_verdict='APPROVE', review_score=NULL,
                           review_flag_category=NULL, review_needs_retry=FALSE,
                           review_request_id=NULL, error_detail=NULL,
                           review_feedback=COALESCE(review_feedback,'') || E'\n[원 세션 판정] APPROVE — ' || $2,
                           updated_at=NOW()
                     WHERE job_id=$1 AND status='review_hold' AND commit_hash=$3
                    """,
                    job_id,
                    findings,
                    commit_sha,
                )
                next_status, next_phase = "awaiting_approval", "awaiting_approval"
            elif req.verdict == "REJECT":
                result = await conn.execute(
                    """
                    UPDATE pipeline_jobs
                       SET status='error', phase='review_failed',
                           review_verdict='REQUEST_CHANGES', review_score=NULL,
                           review_flag_category='CODE_QUALITY', review_needs_retry=FALSE,
                           review_request_id=NULL,
                           error_detail='review_failed: source=origin_session_adjudicator',
                           review_feedback=COALESCE(review_feedback,'') || E'\n[원 세션 판정] REJECT — ' || $2,
                           updated_at=NOW(), completed_at=NOW()
                     WHERE job_id=$1 AND status='review_hold' AND commit_hash=$3
                    """,
                    job_id,
                    findings,
                    commit_sha,
                )
                next_status, next_phase = "error", "review_failed"
            else:
                result = await conn.execute(
                    """
                    UPDATE pipeline_jobs
                       SET review_verdict='UNKNOWN', review_score=NULL,
                           review_needs_retry=TRUE, review_request_id=NULL,
                           review_retry_count=GREATEST($2 - 1, 0),
                           review_retry_last_at=NOW(),
                           error_detail='review_adjudication_unknown',
                           review_feedback=COALESCE(review_feedback,'') || E'\n[원 세션 판정] UNKNOWN — 다음 백오프 후 자동 재검수: ' || $3,
                           updated_at=NOW()
                     WHERE job_id=$1 AND status='review_hold' AND commit_hash=$4
                    """,
                    job_id,
                    retry_threshold,
                    findings,
                    commit_sha,
                )
                next_status, next_phase = "review_hold", "review_hold"

            if not result.endswith(" 1"):
                raise HTTPException(status_code=409, detail="판정 저장 중 작업 상태가 변경되었습니다")
            if req.verdict == "APPROVE":
                # 승인 게이트(approve)는 code_reviews 최신 행만 본다.
                # 원 세션 판정을 pipeline_jobs 에만 기록하면 게이트가 인프라 장애 FLAG 를
                # 계속 읽어 awaiting_approval 인데도 승인이 영구 차단된다.
                # 2026-09-21 runner-173c6907 실사례 — 장부 이원화 해소.
                await conn.execute(
                    """
                    INSERT INTO code_reviews
                        (job_id, project, verdict, score, feedback, diff_size,
                         model_used, cost, flag_category, failure_stage, needs_retry)
                    VALUES ($1, $2, 'APPROVE', 1.0, $3::jsonb, $4,
                            'origin_session_adjudicator', 0, NULL, NULL, FALSE)
                    """,
                    job_id,
                    row["project"],
                    json.dumps(
                        {
                            "summary": findings,
                            "source": "origin_session_adjudicator",
                            "commit_sha": commit_sha,
                        },
                        ensure_ascii=False,
                    ),
                    len(diff_text),
                )
            await conn.execute(
                """
                INSERT INTO pipeline_runner_events
                    (job_id, tenant_id, project, event_type, status, phase, metadata)
                VALUES ($1, $2::uuid, $3, 'origin_review_adjudicated', $4, $5,
                        jsonb_build_object('caller_session_id',$6::text,
                                           'commit_sha',$7::text,
                                           'diff_sha256',$8::text,
                                           'verdict',$9::text,
                                           'findings',$10::text))
                """,
                job_id,
                row["tenant_id"],
                row["project"],
                next_status,
                next_phase,
                req.caller_session_id,
                commit_sha,
                diff_sha256,
                req.verdict,
                findings,
            )

    return {
        "job_id": job_id,
        "verdict": req.verdict,
        "status": next_status,
        "phase": next_phase,
        "commit_sha": commit_sha,
        "diff_sha256": diff_sha256,
        "message": "원 세션 판정이 중앙 상태 머신에 기록되었습니다",
    }


# ─── AADS-211: 배치 제출 — 복수 작업을 의존성 그래프로 한번에 제출 ────────────

class BatchJobItem(BaseModel):
    """배치 내 개별 작업 정의."""
    key: str = Field(..., description="배치 내 작업 식별자 (예: 'A', 'B', 'C')")
    instruction: str = Field(..., max_length=50000)
    size: str = Field("M")
    worker_model: str = Field("")
    worker_model_reason: str = Field("", max_length=500)
    depends_on_key: str = Field("", description="이 배치 내 다른 작업의 key (자동으로 job_id 매핑)")


class BatchSubmitRequest(BaseModel):
    project: str = Field(...)
    session_id: str = Field(...)
    jobs: list[BatchJobItem] = Field(..., min_length=1, max_length=20)
    parallel_group: str = Field("", description="전체 배치에 적용할 병렬 그룹")
    max_cycles: int = Field(3, ge=1, le=10)

    @field_validator('project')
    @classmethod
    def validate_project(cls, v):
        if v not in _VALID_PROJECTS:
            raise ValueError(f"허용 프로젝트: {', '.join(sorted(_VALID_PROJECTS))}")
        return v

    @field_validator('session_id')
    @classmethod
    def validate_session_id(cls, v):
        if not v or not _UUID_RE.match(v):
            raise ValueError("session_id는 필수이며 UUID 형식이어야 합니다")
        return v


def _validate_batch_dependency_graph(jobs: list[BatchJobItem]) -> None:
    """Reject ambiguous keys, missing parents, self edges, and cycles before DB writes."""
    keys = [item.key for item in jobs]
    if len(keys) != len(set(keys)):
        raise HTTPException(status_code=422, detail="배치 작업 key는 중복될 수 없습니다")
    key_set = set(keys)
    parents = {item.key: item.depends_on_key for item in jobs if item.depends_on_key}
    for child, parent in parents.items():
        if parent not in key_set:
            raise HTTPException(status_code=422, detail=f"depends_on_key를 찾을 수 없습니다: {parent}")
        if child == parent:
            raise HTTPException(status_code=422, detail=f"작업이 자기 자신을 의존할 수 없습니다: {child}")
    for start in keys:
        seen: set[str] = set()
        current = start
        while current in parents:
            if current in seen:
                raise HTTPException(status_code=422, detail=f"배치 의존성 순환을 감지했습니다: {start}")
            seen.add(current)
            current = parents[current]


@router.post("/pipeline/jobs/batch", tags=["pipeline-runner"])
async def submit_batch(
    req: BatchSubmitRequest,
    context: TenantContext = Depends(require_tenant_member),
):
    """복수 작업을 의존성 그래프로 한번에 제출.
    AADS-211: 채팅 AI(오케스트레이터)가 작업을 쪼갠 뒤 호출."""
    from app.core.db_pool import get_pool
    pool = get_pool()

    _validate_batch_dependency_graph(req.jobs)
    for item in req.jobs:
        _enforce_owner_resolved_gate(item.instruction, req.project)

    # 자동 parallel_group 생성 (미지정 시)
    pg = req.parallel_group or f"batch-{uuid.uuid4().hex[:8]}"

    # key → job_id 매핑 테이블
    key_to_job_id: dict[str, str] = {}
    for item in req.jobs:
        key_to_job_id[item.key] = f"runner-{uuid.uuid4().hex[:8]}"

    results = []
    gate_targets: list[tuple[str, str]] = []
    batch_file_owner: dict[str, str] = {}
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                tenant_id = _tenant_id(context)
                session_tenant = await conn.fetchval(
                    "SELECT tenant_id::text FROM chat_sessions WHERE id = $1::uuid AND tenant_id = $2::uuid",
                    req.session_id,
                    tenant_id,
                )
                if not session_tenant:
                    raise HTTPException(status_code=404, detail="세션을 찾을 수 없습니다")
                batch_orders = {
                    item.key: await _resolve_milestone_order(
                        conn,
                        project=req.project,
                        tenant_id=tenant_id,
                        instruction=item.instruction,
                    )
                    for item in req.jobs
                }
                for item in req.jobs:
                    job_id = key_to_job_id[item.key]
                    depends_on = key_to_job_id.get(item.depends_on_key) if item.depends_on_key else None
                    item_scope = _parse_write_scope(item.instruction)
                    item_target_files = set(item_scope.write)
                    _log_write_scope(item_scope, job_id=job_id, project=req.project)
                    auto_dependency_reason = ""

                    if item.depends_on_key and _dependency_order_is_inverted(
                        batch_orders[item.key], batch_orders[item.depends_on_key],
                    ):
                        child_order = batch_orders[item.key]
                        parent_order = batch_orders[item.depends_on_key]
                        conflict = {
                            "job_id": depends_on,
                            "incoming_sequence": child_order[2],
                            "parent_sequence": parent_order[2],
                        }
                        raise HTTPException(
                            status_code=409,
                            detail=_dependency_inversion_detail(job_id, conflict),
                        )

                    worker_model, worker_model_reason = _normalize_worker_model_override(
                        item.worker_model,
                        item.worker_model_reason,
                    )
                    size = _resolve_job_size(
                        item.size,
                        item.instruction,
                        size_explicit="size" in item.model_fields_set,
                    )
                    if worker_model:
                        model = worker_model
                    else:
                        model = await _get_model_for_size(conn, size)

                    instruction_hash = _compute_instruction_hash(req.project, item.instruction)
                    await _lock_instruction_hash(conn, instruction_hash)

                    # AADS-239: 멱등성 체크 (submit_job과 동일 로직)
                    # Step 1: 동일 hash + 동일 parallel_group 활성 상태 → blocked 기록
                    existing = await _find_active_duplicate(conn, req.project, instruction_hash, pg, tenant_id=tenant_id)
                    if existing:
                        detail = (
                            f"dedup_blocked: existing job {existing['job_id']} "
                            f"is {existing['status']}/{existing['phase']}"
                        )
                        await conn.execute(
                            """
                            INSERT INTO pipeline_jobs
                              (job_id, project, instruction, instruction_hash, chat_session_id,
                               status, phase, max_cycles, model, size, worker_model,
                               model_override_reason, parallel_group, depends_on,
                               error_detail, review_feedback, logs, created_at, updated_at, tenant_id)
                            VALUES ($1, $2, $3, $4, $5,
                                    'cancelled', 'dedup_blocked', $6, $7, $8, $9,
                                    $10, $11, $12,
                                    $13, $14,
                                    jsonb_build_array(jsonb_build_object(
                                      'ts', NOW()::text,
                                      'event', 'dedup_blocked',
                                      'existing_job_id', $15,
                                      'parallel_scope', $16,
                                      'auto_retryable', false
                                    )),
                                    NOW(), NOW(), $17::uuid)
                            """,
                            job_id,
                            req.project,
                            item.instruction,
                            instruction_hash,
                            req.session_id,
                            req.max_cycles,
                            model,
                            size,
                            worker_model or None,
                            worker_model_reason or None,
                            pg,
                            depends_on,
                            detail,
                            f"[Runner Guard] {detail}; auto_retryable=false",
                            existing["job_id"],
                            _parallel_scope(pg),
                            tenant_id,
                        )
                        results.append({
                            "key": item.key,
                            "job_id": job_id,
                            "model": model,
                            "depends_on": depends_on,
                            "skipped": True,
                            "status": "dedup_blocked",
                            "reason": detail,
                        })
                        continue

                    # Step 2: 동일 hash + error + 2시간 내 → queued 리셋 후 재시도
                    failed = await conn.fetchrow(
                        """
                        SELECT job_id FROM pipeline_jobs
                        WHERE instruction_hash = $1
                          AND status = 'error'
                          AND tenant_id = $2::uuid
                          AND created_at > NOW() - INTERVAL '2 hours'
                        ORDER BY created_at DESC LIMIT 1
                        FOR UPDATE
                        """,
                        instruction_hash,
                        tenant_id,
                    )
                    if failed:
                        await conn.execute(
                            "UPDATE pipeline_jobs SET status = 'queued', phase = 'queued', "
                            "error_detail = NULL, runner_pid = NULL, updated_at = NOW() "
                            "WHERE job_id = $1 AND tenant_id = $2::uuid",
                            failed["job_id"],
                            tenant_id,
                        )
                        key_to_job_id[item.key] = failed["job_id"]
                        await conn.execute("SELECT pg_notify('pipeline_new_job', $1)", failed["job_id"])
                        results.append({
                            "key": item.key,
                            "job_id": failed["job_id"],
                            "model": model,
                            "depends_on": depends_on,
                            "retrying": True,
                        })
                        continue

                    if not depends_on:
                        internal_owners = _batch_scope_owners(item_target_files, batch_file_owner)
                        internal_conflicts = [path for path, _owner in internal_owners]
                        if internal_conflicts:
                            depends_on = internal_owners[0][1]
                            parent_key = next(
                                key for key, mapped_job_id in key_to_job_id.items()
                                if mapped_job_id == depends_on
                            )
                            if _dependency_order_is_inverted(
                                batch_orders[item.key], batch_orders[parent_key],
                            ):
                                child_order = batch_orders[item.key]
                                parent_order = batch_orders[parent_key]
                                conflict = {
                                    "job_id": depends_on,
                                    "incoming_sequence": child_order[2],
                                    "parent_sequence": parent_order[2],
                                }
                                raise HTTPException(
                                    status_code=409,
                                    detail=_dependency_inversion_detail(job_id, conflict),
                                )
                            auto_dependency_reason = (
                                "[Runner Guard] 배치 내 동일 파일 충돌 감지: "
                                f"{', '.join(internal_conflicts)}; {depends_on} 완료 후 자동 실행"
                            )
                        else:
                            conflict = await _find_active_file_conflict(
                                conn,
                                project=req.project,
                                target_files=item_target_files,
                                tenant_id=tenant_id,
                                incoming_instruction=item.instruction,
                                ignore_job_ids=set(key_to_job_id.values()),
                            )
                            if conflict:
                                if conflict.get("dependency_inversion"):
                                    raise HTTPException(
                                        status_code=409,
                                        detail=_dependency_inversion_detail(job_id, conflict),
                                    )
                                depends_on = conflict["job_id"]
                                auto_dependency_reason = (
                                    "[Runner Guard] 활성 작업과 동일 파일 충돌 감지: "
                                    f"{', '.join(conflict['overlap'])}; {depends_on} 완료 후 자동 실행"
                                )

                    await conn.execute(
                        """
                        INSERT INTO pipeline_jobs
                          (job_id, project, instruction, instruction_hash, chat_session_id,
                           status, phase, max_cycles, model, size,
                           worker_model, model_override_reason, parallel_group, depends_on,
                           review_feedback, logs, created_at, updated_at, tenant_id)
                        VALUES ($1, $2, $3, $4, $5, 'queued', 'queued', $6, $7, $8,
                                $9, $10, $11, $12::text,
                                $13::text,
                                CASE WHEN $13::text = '' THEN '[]'::jsonb ELSE jsonb_build_array(jsonb_build_object(
                                  'ts', NOW()::text,
                                  'event', 'file_conflict_auto_dependency',
                                  'depends_on', $12::text
                                )) END,
                                NOW(), NOW(), $14::uuid)
                        """,
                        job_id, req.project, item.instruction, instruction_hash,
                        req.session_id, req.max_cycles, model, size,
                        worker_model or None, worker_model_reason or None, pg, depends_on,
                        auto_dependency_reason,
                        tenant_id,
                    )
                    # P2-2: LISTEN/NOTIFY
                    await conn.execute("SELECT pg_notify('pipeline_new_job', $1)", job_id)

                    try:
                        from app.services.pipeline_runner_service import _link_job_to_goal_explicit
                        # 배치도 동일 규칙 — 지시서 GOAL_ID/MILESTONE_ID 메타데이터가
                        # 있을 때만 연결하고, 없으면 어떤 목표에도 붙이지 않는다.
                        batch_goal_link = await _link_job_to_goal_explicit(
                            job_id, req.project, instruction=item.instruction,
                        )
                        if batch_goal_link:
                            gate_targets.append((job_id, batch_goal_link.goal_id))
                    except Exception as exc:
                        logger.warning("pipeline_runner.goal_link_fail", job_id=job_id, error=str(exc))
                    for path in item_target_files:
                        batch_file_owner.setdefault(path, job_id)

                    results.append({
                        "key": item.key,
                        "job_id": job_id,
                        "model": model,
                        "depends_on": depends_on,
                        "auto_dependency": bool(auto_dependency_reason),
                        "target_files": sorted(item_target_files),
                        "write_scope": "explicit" if item_scope.explicit else "legacy",
                    })

    except HTTPException:
        raise
    except HTTPException:
        raise
    except Exception as e:
        logger.error("pipeline_runner.batch_submit_fail", error=str(e))
        raise HTTPException(status_code=500, detail="배치 저장 실패")

    logger.info("pipeline_runner.batch_submitted",
                 project=req.project, count=len(results), parallel_group=pg)

    # 정본 게이트(그림자): 커밋된 뒤 판정·기록만 한다. 실패해도 응답은 그대로다.
    if gate_targets:
        import asyncio

        from app.services.canonical_gate import check_runner_submit
        by_job = {r["job_id"]: r for r in results}
        # 동시에 돌려 배치 전체 추가 지연을 잡 수와 무관하게 게이트 상한(300ms) 안에 둔다.
        gates = await asyncio.gather(*(
            check_runner_submit(
                goal_id=gate_goal_id, tenant_id=_tenant_id(context),
                project=req.project, job_id=gate_job_id,
            )
            for gate_job_id, gate_goal_id in gate_targets
        ))
        for (gate_job_id, _goal_id), gate in zip(gate_targets, gates):
            if gate and gate_job_id in by_job:
                by_job[gate_job_id]["canonical_gate"] = gate

    return {
        "parallel_group": pg,
        "jobs": results,
        "message": f"{len(results)}개 작업이 제출되었습니다. 의존성에 따라 순차/병렬 실행됩니다.",
    }


@router.get("/pipeline/lock-status", tags=["pipeline-runner"])
async def lock_status(
    project: str = Query(..., max_length=10),
    context: TenantContext = Depends(require_tenant_viewer),
):
    """프로젝트별 동시실행 Lock 상태 조회. Shell runner가 claim 전 호출."""
    if project not in _VALID_PROJECTS:
        raise HTTPException(status_code=400, detail="유효하지 않은 프로젝트")

    from app.core.db_pool import get_pool
    pool = get_pool()

    async with pool.acquire() as conn:
        running_row = await conn.fetchrow(
            "SELECT count(*) as cnt FROM pipeline_jobs "
            "WHERE project = $1 AND tenant_id = $2::uuid AND status IN ('running', 'claimed')",
            project,
            _tenant_id(context),
        )
        locked = await check_project_lock(conn, project, tenant_id=_tenant_id(context))
        queued_row = await conn.fetchrow(
            "SELECT count(*) as cnt FROM pipeline_jobs "
            "WHERE project = $1 AND tenant_id = $2::uuid AND status = 'queued'",
            project,
            _tenant_id(context),
        )

    return {
        "project": project,
        "locked": locked,
        "running_count": running_row["cnt"] if running_row else 0,
        "max_concurrent_per_project": _max_concurrent_per_project(),
        "queued_count": queued_row["cnt"] if queued_row else 0,
    }


# ── Runner Model Config (AADS-241) ──────────────────────────────────

class _RunnerModelItem(BaseModel):
    """size별 모델 우선순위."""
    size: str = Field(..., pattern=r"^(XS|S|M|L|XL|AI_REVIEW)$")
    models: list[str] = Field(..., min_length=1)


class _RunnerModelConfigUpdate(BaseModel):
    """CEO 대시보드에서 러너 모델 설정 업데이트."""
    configs: list[_RunnerModelItem]


@router.get("/settings/runner-models")
async def get_runner_model_config():
    """size별 러너 모델 우선순위와 실제 자동 폴백 체인 조회."""
    import json as _json_get
    from app.core.db_pool import get_pool
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT size, models, updated_at, updated_by "
            "FROM runner_model_config ORDER BY size"
        )
        effective_by_size = {
            size: await _get_model_cycle_for_size(conn, size)
            for size in ["XS", "S", "M", "L", "XL", "AI_REVIEW"]
        }
    configs = []
    for r in rows:
        # asyncpg JSONB → str일 수 있으므로 안전하게 파싱
        raw = r["models"]
        if isinstance(raw, str):
            models = _json_get.loads(raw)
        elif isinstance(raw, list):
            models = raw
        else:
            models = list(raw) if raw else []
        configs.append({
            "size": r["size"],
            "models": models,
            "effective_models": effective_by_size.get(r["size"], models),
            "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
            "updated_by": r["updated_by"],
        })
    return {"configs": configs, "effective_by_size": effective_by_size}


@router.put("/settings/runner-models")
async def update_runner_model_config(req: _RunnerModelConfigUpdate):
    """size별 러너 모델 우선순위 업데이트. CEO 대시보드에서 호출."""
    import json as _json
    from app.core.db_pool import get_pool
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            for item in req.configs:
                await conn.execute(
                    "INSERT INTO runner_model_config (size, models, updated_at, updated_by) "
                    "VALUES ($1, $2::jsonb, NOW(), 'CEO') "
                    "ON CONFLICT (size) DO UPDATE "
                    "SET models = EXCLUDED.models, updated_at = NOW(), updated_by = 'CEO'",
                    item.size.upper(),
                    _json.dumps(item.models),
                )
    logger.info("runner_model_config_updated", count=len(req.configs))
    return {"status": "ok", "message": f"{len(req.configs)}개 size 모델 설정 업데이트 완료"}
