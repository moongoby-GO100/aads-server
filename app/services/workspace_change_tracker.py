"""
채팅 세션별 workspace 변경 ledger + finalize 서비스.

목표:
- 파일 수정 시 즉시 commit/push를 하지 않고 변경 사실만 기록
- finalize 시점에만 git add/commit/push 수행
- 배포 전 preflight에서 누락 반영 차단
"""
from __future__ import annotations

import logging
import shlex
from collections import defaultdict
from typing import Any, Dict, Iterable, Optional

from app.core.db_pool import get_pool
from app.core.git_lock import git_project_lock

logger = logging.getLogger(__name__)

_STATUS_DIRTY = "dirty"
_STATUS_COMMITTED = "committed"
_STATUS_PUSHED = "pushed"
_STATUS_DEPLOYED = "deployed"


async def ensure_workspace_change_table() -> None:
    """변경 ledger 테이블 보장."""
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_workspace_change_ledger (
                id BIGSERIAL PRIMARY KEY,
                session_id TEXT NOT NULL,
                project TEXT NOT NULL,
                repo TEXT NOT NULL,
                file_path TEXT NOT NULL,
                source_tool TEXT DEFAULT '',
                change_summary TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'dirty',
                last_error TEXT DEFAULT '',
                commit_sha TEXT,
                commit_message TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW(),
                last_modified_at TIMESTAMPTZ DEFAULT NOW(),
                finalized_at TIMESTAMPTZ,
                pushed_at TIMESTAMPTZ,
                deployed_at TIMESTAMPTZ,
                UNIQUE(session_id, project, repo, file_path)
            )
            """
        )
        await conn.execute(
            """
            ALTER TABLE chat_workspace_change_ledger
            ADD COLUMN IF NOT EXISTS deployed_at TIMESTAMPTZ
            """
        )
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_workspace_change_ledger_session_status
            ON chat_workspace_change_ledger (session_id, status, updated_at DESC)
            """
        )
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_workspace_change_ledger_project_repo_status
            ON chat_workspace_change_ledger (project, repo, status, updated_at DESC)
            """
        )


async def record_change(
    *,
    session_id: str,
    project: str,
    repo: str,
    file_path: str,
    change_summary: str,
    source_tool: str,
) -> None:
    """세션별 변경 파일 기록 또는 갱신."""
    sid = (session_id or "").strip()
    if not sid:
        raise ValueError("session_id is required")
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO chat_workspace_change_ledger
                (session_id, project, repo, file_path, source_tool, change_summary,
                 status, last_error, commit_sha, commit_message,
                 last_modified_at, updated_at)
            VALUES
                ($1, $2, $3, $4, $5, $6,
                 $7, '', NULL, NULL,
                 NOW(), NOW())
            ON CONFLICT (session_id, project, repo, file_path) DO UPDATE SET
                source_tool = EXCLUDED.source_tool,
                change_summary = EXCLUDED.change_summary,
                status = $7,
                last_error = '',
                commit_sha = NULL,
                commit_message = NULL,
                last_modified_at = NOW(),
                updated_at = NOW(),
                finalized_at = NULL,
                pushed_at = NULL
            """,
            sid,
            project,
            repo,
            file_path,
            source_tool,
            change_summary[:1000],
            _STATUS_DIRTY,
        )


async def list_changes(
    *,
    session_id: str,
    project: Optional[str] = None,
    repo: Optional[str] = None,
    statuses: Optional[Iterable[str]] = None,
) -> list[dict[str, Any]]:
    """세션별 ledger 조회."""
    sid = (session_id or "").strip()
    if not sid:
        return []
    conditions = ["session_id = $1"]
    args: list[Any] = [sid]
    idx = 2
    if project:
        conditions.append(f"project = ${idx}")
        args.append(project)
        idx += 1
    if repo:
        conditions.append(f"repo = ${idx}")
        args.append(repo)
        idx += 1
    if statuses:
        conditions.append(f"status = ANY(${idx}::text[])")
        args.append(list(statuses))
        idx += 1
    where = " AND ".join(conditions)
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT session_id, project, repo, file_path, source_tool, change_summary,
                   status, last_error, commit_sha, commit_message,
                   created_at, updated_at, last_modified_at, finalized_at, pushed_at, deployed_at
            FROM chat_workspace_change_ledger
            WHERE {where}
            ORDER BY updated_at DESC, project, repo, file_path
            """,
            *args,
        )
    return [dict(r) for r in rows]


async def has_pending_changes(*, session_id: str, project: Optional[str] = None) -> bool:
    """dirty/committed 상태의 미반영 변경 존재 여부."""
    rows = await list_changes(
        session_id=session_id,
        project=project,
        statuses=(_STATUS_DIRTY, _STATUS_COMMITTED),
    )
    return bool(rows)


def _command_has_error(text: str) -> bool:
    lowered = text.lower()
    if "everything up-to-date" in lowered:
        return False
    return (
        "[error]" in lowered
        or "fatal:" in lowered
        or "error:" in lowered
        or "rejected" in lowered
        or "permission denied" in lowered
    )


def _command_no_changes(text: str) -> bool:
    lowered = text.lower()
    return (
        "nothing to commit" in lowered
        or "no changes added to commit" in lowered
        or "nothing added to commit" in lowered
        or "working tree clean" in lowered
    )


def _repo_prefix(project: str, repo: str) -> str:
    if project != "AADS":
        return ""
    repo_dir_map = {
        "aads-server": "/root/aads/aads-server",
        "aads-dashboard": "/root/aads/aads-dashboard",
    }
    repo_dir = repo_dir_map.get(repo, "/root/aads/aads-server")
    return f"cd {repo_dir} && "


def _normalize_repo_path(project: str, repo: str, file_path: str) -> str:
    path = (file_path or "").strip()
    if not path:
        return path
    if project != "AADS":
        return path
    prefixes = []
    if repo == "aads-server":
        prefixes = [
            "/root/aads/aads-server/",
            "/app/",
            "/app/app/",
        ]
    elif repo == "aads-dashboard":
        prefixes = [
            "/root/aads/aads-dashboard/",
        ]
    for prefix in prefixes:
        if path.startswith(prefix):
            return path[len(prefix):]
    return path


def _build_commit_message(session_id: str, repo: str, file_paths: list[str]) -> str:
    sid_short = session_id[:8]
    if len(file_paths) == 1:
        return f"Chat-Finalize[{repo}]: {file_paths[0]} ({sid_short})"
    return f"Chat-Finalize[{repo}]: {len(file_paths)} files ({sid_short})"


async def _run_git_command(project: str, repo: str, command: str) -> str:
    from app.api.ceo_chat_tools import tool_run_remote_command

    full_cmd = f"{_repo_prefix(project, repo)}{command}" if _repo_prefix(project, repo) else command
    result = await tool_run_remote_command(project, full_cmd)
    return result if isinstance(result, str) else str(result)


async def _mark_group(
    *,
    session_id: str,
    project: str,
    repo: str,
    file_paths: list[str],
    ledger_file_paths: Optional[list[str]] = None,
    status: str,
    last_error: str = "",
    commit_sha: Optional[str] = None,
    commit_message: Optional[str] = None,
) -> None:
    match_paths = list(dict.fromkeys([*(file_paths or []), *((ledger_file_paths or []))]))
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE chat_workspace_change_ledger
            SET status = $1,
                last_error = $2,
                commit_sha = $3,
                commit_message = $4,
                finalized_at = CASE WHEN $1 IN ('committed', 'pushed') THEN NOW() ELSE finalized_at END,
                pushed_at = CASE WHEN $1 = 'pushed' THEN NOW() ELSE pushed_at END,
                deployed_at = CASE WHEN $1 = 'deployed' THEN NOW() ELSE deployed_at END,
                updated_at = NOW()
            WHERE session_id = $5
              AND project = $6
              AND repo = $7
              AND file_path = ANY($8::text[])
            """,
            status,
            (last_error or "")[:4000],
            commit_sha,
            commit_message,
            session_id,
            project,
            repo,
            match_paths,
        )


async def _finalize_group(
    *,
    session_id: str,
    project: str,
    repo: str,
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    file_paths = [_normalize_repo_path(project, repo, row["file_path"]) for row in rows]
    file_paths = [path for path in dict.fromkeys(file_paths) if path]
    result: dict[str, Any] = {
        "project": project,
        "repo": repo,
        "files": file_paths,
        "ok": False,
        "status": _STATUS_DIRTY,
        "commit_sha": None,
        "commit_message": "",
        "detail": "",
    }
    if not file_paths:
        result["ok"] = True
        result["status"] = _STATUS_PUSHED
        result["detail"] = "no files"
        return result

    commit_message = _build_commit_message(session_id, repo, file_paths)
    quoted_files = " ".join(shlex.quote(path) for path in file_paths)
    ledger_file_paths = [str(row.get("file_path") or "").strip() for row in rows]
    ledger_file_paths = [path for path in dict.fromkeys(ledger_file_paths) if path]
    commit_sha = None

    try:
        async with git_project_lock(f"{project}:{repo}", timeout=60):
            add_result = await _run_git_command(project, repo, f"git add -- {quoted_files}")
            if _command_has_error(add_result):
                await _mark_group(
                    session_id=session_id,
                    project=project,
                    repo=repo,
                    file_paths=file_paths,
                    ledger_file_paths=ledger_file_paths,
                    status=_STATUS_DIRTY,
                    last_error=add_result,
                )
                result["detail"] = add_result[:500]
                return result

            commit_result = await _run_git_command(
                project,
                repo,
                f"git commit --only -m {shlex.quote(commit_message)} -- {quoted_files}",
            )
            if _command_has_error(commit_result) and not _command_no_changes(commit_result):
                await _mark_group(
                    session_id=session_id,
                    project=project,
                    repo=repo,
                    file_paths=file_paths,
                    ledger_file_paths=ledger_file_paths,
                    status=_STATUS_DIRTY,
                    last_error=commit_result,
                )
                result["detail"] = commit_result[:500]
                return result

            sha_result = await _run_git_command(project, repo, "git rev-parse HEAD")
            commit_sha = sha_result.strip().splitlines()[-1] if sha_result.strip() else None

            push_result = await _run_git_command(project, repo, "git push origin main")
            if _command_has_error(push_result):
                fallback_result = await _run_git_command(project, repo, "git push origin master")
                if _command_has_error(fallback_result):
                    await _mark_group(
                        session_id=session_id,
                        project=project,
                        repo=repo,
                        file_paths=file_paths,
                        ledger_file_paths=ledger_file_paths,
                        status=_STATUS_COMMITTED,
                        last_error=fallback_result,
                        commit_sha=commit_sha,
                        commit_message=commit_message,
                    )
                    result["status"] = _STATUS_COMMITTED
                    result["commit_sha"] = commit_sha
                    result["commit_message"] = commit_message
                    result["detail"] = fallback_result[:500]
                    return result
                push_result = fallback_result

            await _mark_group(
                session_id=session_id,
                project=project,
                repo=repo,
                file_paths=file_paths,
                ledger_file_paths=ledger_file_paths,
                status=_STATUS_PUSHED,
                last_error="",
                commit_sha=commit_sha,
                commit_message=commit_message,
            )
            result["ok"] = True
            result["status"] = _STATUS_PUSHED
            result["commit_sha"] = commit_sha
            result["commit_message"] = commit_message
            result["detail"] = push_result[:500]
            return result
    except TimeoutError as exc:
        detail = str(exc)
        await _mark_group(
            session_id=session_id,
            project=project,
            repo=repo,
            file_paths=file_paths,
            ledger_file_paths=ledger_file_paths,
            status=_STATUS_DIRTY,
            last_error=detail,
        )
        result["detail"] = detail[:500]
        return result
    except Exception as exc:
        detail = str(exc)
        logger.warning("workspace_change_finalize_group_failed project=%s repo=%s err=%s", project, repo, detail)
        await _mark_group(
            session_id=session_id,
            project=project,
            repo=repo,
            file_paths=file_paths,
            ledger_file_paths=ledger_file_paths,
            status=_STATUS_DIRTY,
            last_error=detail,
        )
        result["detail"] = detail[:500]
        return result


async def finalize_session_changes(
    *,
    session_id: str,
    project: Optional[str] = None,
    repo: Optional[str] = None,
    reason: str = "",
) -> dict[str, Any]:
    """세션의 미반영 변경을 repo 단위로 commit/push."""
    rows = await list_changes(
        session_id=session_id,
        project=project,
        repo=repo,
        statuses=(_STATUS_DIRTY, _STATUS_COMMITTED),
    )
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[(row["project"], row["repo"])].append(row)

    groups: list[dict[str, Any]] = []
    ok = True
    for (group_project, group_repo), group_rows in grouped.items():
        group_result = await _finalize_group(
            session_id=session_id,
            project=group_project,
            repo=group_repo,
            rows=group_rows,
        )
        groups.append(group_result)
        ok = ok and group_result.get("ok", False)

    return {
        "ok": ok,
        "session_id": session_id,
        "reason": reason,
        "groups": groups,
        "pending_groups": len(grouped),
    }


async def mark_session_changes_deployed(
    *,
    session_id: str,
    project: str,
    repo: Optional[str] = None,
    deploy_summary: str = "",
) -> dict[str, Any]:
    """배포 성공 후 pushed 상태 변경을 deployed로 승격."""
    sid = (session_id or "").strip()
    if not sid:
        return {"ok": False, "updated": 0, "reason": "missing_session_id"}

    conditions = ["session_id = $1", "project = $2", "status = $3"]
    extra_args: list[Any] = []
    idx = 6
    if repo:
        conditions.append(f"repo = ${idx}")
        extra_args.append(repo)
        idx += 1
    where = " AND ".join(conditions)
    deploy_summary_trimmed = (deploy_summary or "")[:300]

    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            UPDATE chat_workspace_change_ledger
            SET status = $4,
                last_error = '',
                deployed_at = NOW(),
                updated_at = NOW(),
                change_summary = CASE
                    WHEN $5 = '' THEN change_summary
                    ELSE LEFT(COALESCE(change_summary, '') || ' | deployed: ' || $5, 1000)
                END
            WHERE {where}
            RETURNING project, repo, file_path, commit_sha, deployed_at
            """,
            sid,
            project,
            _STATUS_PUSHED,
            _STATUS_DEPLOYED,
            deploy_summary_trimmed,
            *extra_args,
        )

    return {
        "ok": True,
        "updated": len(rows),
        "session_id": sid,
        "project": project,
        "repo": repo,
        "items": [dict(r) for r in rows],
    }
