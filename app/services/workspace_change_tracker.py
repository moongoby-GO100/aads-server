"""
채팅 세션별 workspace 변경 ledger + finalize 서비스.

목표:
- 파일 수정 시 즉시 commit/push를 하지 않고 변경 사실만 기록
- finalize 시점에만 git add/commit/push 수행
- 배포 전 preflight에서 누락 반영 차단
"""
from __future__ import annotations

import logging
import asyncio
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
_STATUS_RECONCILED_CLEAN = "reconciled_clean"
_STATUS_SUPERSEDED_OWNER = "superseded_owner"
_AADS_RUNTIME_STATE_PATHS = {".active_container", ".active_port"}
_AADS_RUNTIME_PREFIXES = ("app/data/",)
_AADS_RUNTIME_SUFFIXES = (".lock", ".jsonl", ".tsbuildinfo", ".bak")


def _is_ignored_change_path(project: str, repo: str, file_path: str) -> bool:
    """Return true for runtime state files that must not enter the change ledger."""
    normalized = _normalize_repo_path(project, repo, file_path)
    if project == "AADS" and repo == "aads-server" and normalized in _AADS_RUNTIME_STATE_PATHS:
        return True
    if project == "AADS" and normalized.endswith(_AADS_RUNTIME_SUFFIXES):
        return True
    if project == "AADS" and repo == "aads-server" and normalized.startswith(_AADS_RUNTIME_PREFIXES):
        return True
    return False


def _derive_change_owner(session_id: str, source_tool: str, owner: Optional[str] = None) -> str:
    """Return a compact owner label for file-level dirty attribution."""
    explicit = (owner or "").strip()
    if explicit:
        return explicit[:120]
    sid = (session_id or "").strip()
    if sid:
        return f"chat:{sid[:12]}"
    tool = (source_tool or "").strip()
    return (f"tool:{tool}" if tool else "unknown")[:120]


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
            ALTER TABLE chat_workspace_change_ledger
            ADD COLUMN IF NOT EXISTS owner TEXT DEFAULT '',
            ADD COLUMN IF NOT EXISTS task_id TEXT DEFAULT '',
            ADD COLUMN IF NOT EXISTS change_origin TEXT DEFAULT 'chat_direct',
            ADD COLUMN IF NOT EXISTS git_status TEXT DEFAULT '',
            ADD COLUMN IF NOT EXISTS git_branch TEXT DEFAULT '',
            ADD COLUMN IF NOT EXISTS git_head_sha TEXT DEFAULT '',
            ADD COLUMN IF NOT EXISTS detected_at TIMESTAMPTZ DEFAULT NOW()
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
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_workspace_change_ledger_file_owner
            ON chat_workspace_change_ledger (project, repo, file_path, status, updated_at DESC)
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
    owner: Optional[str] = None,
    task_id: Optional[str] = None,
    change_origin: str = "chat_direct",
    git_status: str = "",
    git_branch: str = "",
    git_head_sha: str = "",
) -> None:
    """세션별 변경 파일 기록 또는 갱신."""
    sid = (session_id or "").strip()
    if not sid:
        raise ValueError("session_id is required")
    if _is_ignored_change_path(project, repo, file_path):
        logger.info(
            "workspace_change_ignored_runtime_state project=%s repo=%s file=%s",
            project,
            repo,
            file_path,
        )
        return
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO chat_workspace_change_ledger
                (session_id, project, repo, file_path, source_tool, change_summary,
                 status, last_error, commit_sha, commit_message,
                 last_modified_at, updated_at, owner, task_id, change_origin,
                 git_status, git_branch, git_head_sha, detected_at)
            VALUES
                ($1, $2, $3, $4, $5, $6,
                 $7, '', NULL, NULL,
                 NOW(), NOW(), $8, $9, $10,
                 $11, $12, $13, NOW())
            ON CONFLICT (session_id, project, repo, file_path) DO UPDATE SET
                source_tool = EXCLUDED.source_tool,
                change_summary = EXCLUDED.change_summary,
                status = $7,
                last_error = '',
                commit_sha = NULL,
                commit_message = NULL,
                owner = EXCLUDED.owner,
                task_id = EXCLUDED.task_id,
                change_origin = EXCLUDED.change_origin,
                git_status = EXCLUDED.git_status,
                git_branch = EXCLUDED.git_branch,
                git_head_sha = EXCLUDED.git_head_sha,
                detected_at = NOW(),
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
            _derive_change_owner(sid, source_tool, owner),
            (task_id or "").strip()[:80],
            (change_origin or "chat_direct").strip()[:80],
            (git_status or "").strip()[:12],
            (git_branch or "").strip()[:120],
            (git_head_sha or "").strip()[:40],
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
                   owner, task_id, change_origin, git_status, git_branch, git_head_sha, detected_at,
                   created_at, updated_at, last_modified_at, finalized_at, pushed_at, deployed_at
            FROM chat_workspace_change_ledger
            WHERE {where}
            ORDER BY updated_at DESC, project, repo, file_path
            """,
            *args,
        )
    return [
        dict(r)
        for r in rows
        if not _is_ignored_change_path(str(r["project"]), str(r["repo"]), str(r["file_path"]))
    ]


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


def _parse_porcelain_paths(text: str) -> set[str]:
    """`git status --porcelain` 출력에서 실제 변경 경로만 뽑는다.

    원격 실행 래퍼가 붙이는 헤더(`[AADS 명령 실행 …]`, `$ cmd`, `[STDERR] …`)는 버린다.
    """
    paths: set[str] = set()
    for line in (text or "").splitlines():
        raw = line.rstrip()
        if not raw or raw.startswith("[") or raw.startswith("$ "):
            continue
        if len(raw) < 4:
            continue
        path = raw[3:].strip()
        if " -> " in path:  # rename
            path = path.split(" -> ", 1)[1].strip()
        path = path.strip('"')
        if path:
            paths.add(path)
    return paths


def _parse_porcelain_entries(text: str) -> list[dict[str, str]]:
    """Parse `git status --porcelain=v1 -b` into path/status/branch entries."""
    entries: list[dict[str, str]] = []
    branch = ""
    for line in (text or "").splitlines():
        raw = line.rstrip()
        if not raw or raw.startswith("[") or raw.startswith("$ "):
            continue
        if raw.startswith("## "):
            branch = raw[3:].split("...", 1)[0].strip()
            continue
        if len(raw) < 4:
            continue
        xy = raw[:2]
        path = raw[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1].strip()
        path = path.strip('"')
        if path:
            entries.append({"path": path, "git_status": xy, "git_branch": branch})
    return entries


def _repo_workdir(project: str, repo: str) -> str:
    if project == "AADS" and repo == "aads-dashboard":
        return "/root/aads/aads-dashboard"
    if project == "AADS":
        return "/root/aads/aads-server"
    return "."


async def _run_local_git(repo_dir: str, *args: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        repo_dir,
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    text = stdout.decode("utf-8", errors="replace")
    err = stderr.decode("utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError((err or text).strip() or f"git exited {proc.returncode}")
    return text


async def sync_git_dirty_snapshot(
    *,
    session_id: str,
    project: str = "AADS",
    repo: Optional[str] = None,
    owner: Optional[str] = None,
    task_id: Optional[str] = None,
    claim_paths: Optional[Iterable[str]] = None,
    mark_stale_clean: bool = True,
) -> dict[str, Any]:
    """Record current git dirty files and reconcile stale dirty ledger rows.

    This is the bridge between the real worktree and `chat_workspace_change_ledger`.
    It lets reports and future automation answer: file -> owner/session/task_id.
    """
    sid = (session_id or "").strip()
    if not sid:
        raise ValueError("session_id is required")
    await ensure_workspace_change_table()
    repos = [repo] if repo else ["aads-server", "aads-dashboard"]
    summary: dict[str, Any] = {
        "ok": True,
        "session_id": sid,
        "project": project,
        "repos": [],
        "recorded": 0,
        "ignored": 0,
        "stale_reconciled": 0,
        "superseded": 0,
    }
    raw_claim_paths = [str(path).strip() for path in (claim_paths or []) if str(path or "").strip()]
    pool = get_pool()
    for repo_name in repos:
        claim_set = {
            _normalize_repo_path(project, repo_name, path)
            for path in raw_claim_paths
        }
        repo_dir = _repo_workdir(project, repo_name)
        status_text = await _run_local_git(repo_dir, "status", "--porcelain=v1", "-b")
        head_sha = (await _run_local_git(repo_dir, "rev-parse", "HEAD")).strip()
        entries = _parse_porcelain_entries(status_text)
        current_paths: set[str] = set()
        ignored_paths: list[str] = []
        recorded_paths: list[str] = []
        for entry in entries:
            path = _normalize_repo_path(project, repo_name, entry["path"])
            if _is_ignored_change_path(project, repo_name, path):
                ignored_paths.append(path)
                continue
            is_claimed = not claim_set or path in claim_set
            current_paths.add(path)
            recorded_paths.append(path)
            await record_change(
                session_id=sid,
                project=project,
                repo=repo_name,
                file_path=path,
                source_tool="git_status_snapshot",
                change_summary=f"git dirty snapshot: {entry['git_status'].strip() or 'changed'}",
                owner=owner if is_claimed else "UNKNOWN-PREEXISTING-DIRTY",
                task_id=task_id if is_claimed else "",
                change_origin="git_status_snapshot",
                git_status=entry["git_status"],
                git_branch=entry["git_branch"],
                git_head_sha=head_sha,
            )
        stale_ids: list[int] = []
        superseded_ids: list[int] = []
        if mark_stale_clean:
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT id, session_id, file_path
                    FROM chat_workspace_change_ledger
                    WHERE project=$1 AND repo=$2 AND status=$3
                    """,
                    project,
                    repo_name,
                    _STATUS_DIRTY,
                )
                for row in rows:
                    path = _normalize_repo_path(project, repo_name, row["file_path"])
                    if path not in current_paths or _is_ignored_change_path(project, repo_name, path):
                        stale_ids.append(int(row["id"]))
                    elif row["session_id"] != sid:
                        superseded_ids.append(int(row["id"]))
                if stale_ids:
                    await conn.execute(
                        """
                        UPDATE chat_workspace_change_ledger
                        SET status=$1,
                            last_error='',
                            change_summary=LEFT(COALESCE(change_summary, '') || ' | reconciled: clean in git status', 1000),
                            updated_at=NOW()
                        WHERE id = ANY($2::bigint[])
                        """,
                        _STATUS_RECONCILED_CLEAN,
                        stale_ids,
                    )
                if superseded_ids:
                    await conn.execute(
                        """
                        UPDATE chat_workspace_change_ledger
                        SET status=$1,
                            last_error='',
                            change_summary=LEFT(COALESCE(change_summary, '') || ' | reconciled: superseded by latest git dirty owner', 1000),
                            updated_at=NOW()
                        WHERE id = ANY($2::bigint[])
                        """,
                        _STATUS_SUPERSEDED_OWNER,
                        superseded_ids,
                    )
        repo_summary = {
            "repo": repo_name,
            "recorded": len(recorded_paths),
            "ignored": len(ignored_paths),
            "stale_reconciled": len(stale_ids),
            "superseded": len(superseded_ids),
            "dirty_files": recorded_paths[:100],
            "ignored_files": ignored_paths[:100],
        }
        summary["repos"].append(repo_summary)
        summary["recorded"] += len(recorded_paths)
        summary["ignored"] += len(ignored_paths)
        summary["stale_reconciled"] += len(stale_ids)
        summary["superseded"] += len(superseded_ids)
    return summary


def _is_committable_path(path: str) -> bool:
    """레포 밖 경로(/tmp, /root/.ssh, ../..)는 git add에서 제외한다."""
    if not path or path.startswith("/"):
        return False
    if path.startswith("../") or "/../" in path or path == "..":
        return False
    return True


async def _finalize_group(
    *,
    session_id: str,
    project: str,
    repo: str,
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    file_paths = [_normalize_repo_path(project, repo, row["file_path"]) for row in rows]
    file_paths = [path for path in dict.fromkeys(file_paths) if path]

    # AADS-FILES(2026-08-18): 레포 밖 경로/이미 사라진 경로가 섞이면 `git add`가 exit 128로
    # 죽어 배포 preflight 전체가 영구 차단됐다. 실제 커밋 가능한 경로만 남긴다.
    skipped_outside = [path for path in file_paths if not _is_committable_path(path)]
    file_paths = [path for path in file_paths if _is_committable_path(path)]
    skipped_missing: list[str] = []
    if file_paths:
        try:
            status_text = await _run_git_command(
                project, repo, "git -c core.quotepath=false status --porcelain"
            )
            changed = _parse_porcelain_paths(status_text)
            if changed:
                skipped_missing = [path for path in file_paths if path not in changed]
                file_paths = [path for path in file_paths if path in changed]
        except Exception:  # 상태 조회 실패 시에는 기존 동작 유지
            pass
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
    if skipped_outside or skipped_missing:
        result["skipped_outside_repo"] = skipped_outside[:20]
        result["skipped_not_dirty"] = skipped_missing[:20]

    all_ledger_paths = [str(row.get("file_path") or "").strip() for row in rows]
    all_ledger_paths = [path for path in dict.fromkeys(all_ledger_paths) if path]

    if not file_paths:
        # 커밋 대상이 하나도 없으면 ledger를 정리해 다음 배포를 막지 않는다.
        if all_ledger_paths:
            try:
                await _mark_group(
                    session_id=session_id,
                    project=project,
                    repo=repo,
                    file_paths=[],
                    ledger_file_paths=all_ledger_paths,
                    status=_STATUS_PUSHED,
                    last_error="",
                    commit_message="Chat-Finalize: no committable change",
                )
            except Exception:
                pass
        result["ok"] = True
        result["status"] = _STATUS_PUSHED
        result["detail"] = "no committable files"
        return result

    commit_message = _build_commit_message(session_id, repo, file_paths)
    quoted_files = " ".join(shlex.quote(path) for path in file_paths)
    ledger_file_paths = all_ledger_paths
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
