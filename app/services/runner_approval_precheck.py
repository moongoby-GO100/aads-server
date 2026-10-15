"""러너 승인 전 사전검증 (FR-13a) — 승인 버튼을 누르기 전에 실패할 승인을 미리 보인다.

승인 게이트 판정(`evaluate_approval_gates`)은 `approve_or_reject` 와 이 모듈이 **같은 함수**를
쓴다. 판정 로직을 두 벌 두면 사전검증은 통과인데 승인은 409 인 어긋남이 생긴다.
`fail` 인 게이트 항목의 detail 은 승인 API 가 돌려주는 409 메시지와 글자까지 같다.

이 모듈은 읽기 전용이다. 상태 전이·승인·git 변경(fetch 포함)을 하지 않는다.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Any, Mapping, Optional, Sequence

AWAITING_APPROVAL = "awaiting_approval"
GIT_TIMEOUT_SEC = 10
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_DEAD_PARENT_STATUSES = frozenset({"error", "cancelled", "canceled"})
_PARENT_OK_STATUSES = frozenset({"done", "completed", "approved", "deploying"})

PASS, WARN, FAIL = "pass", "warn", "fail"


def is_deploy_only_instruction(instruction: str) -> bool:
    lines = (instruction or "").splitlines()[:20]
    return "DEPLOY_ONLY: true" in "\n".join(lines)


def _check(key: str, status: str, detail: str) -> dict[str, str]:
    return {"key": key, "status": status, "detail": detail}


def _get(row: Any, key: str, default: Any = None) -> Any:
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


def evaluate_approval_gates(
    *,
    deploy_only: bool,
    git_diff: str,
    commit_hash: str,
    changed_files: Any,
    review: Optional[Mapping[str, Any]],
) -> list[dict[str, str]]:
    """승인 시점 게이트를 순서대로 판정한다. fail 의 detail 이 곧 409 메시지다."""
    skipped = "DEPLOY_ONLY 작업 — 검사 생략"
    checks: list[dict[str, str]] = []

    if deploy_only:
        checks.append(_check("git_diff", PASS, skipped))
    elif "diff --git " not in (git_diff or ""):
        checks.append(_check("git_diff", FAIL, "승인 차단: 유효한 git diff가 없습니다"))
    else:
        checks.append(_check("git_diff", PASS, "유효한 git diff 있음"))

    commit_hash = (commit_hash or "").strip()
    if deploy_only:
        checks.append(_check("commit_hash", PASS, skipped))
    elif not _SHA_RE.match(commit_hash):
        checks.append(_check("commit_hash", FAIL, "승인 차단: 승인용 commit_hash가 없습니다"))
    else:
        checks.append(_check("commit_hash", PASS, f"commit_hash {commit_hash[:12]}"))

    if deploy_only:
        checks.append(_check("changed_files", PASS, skipped))
    elif not changed_files:
        checks.append(_check("changed_files", FAIL, "승인 차단: 실제 변경 파일 목록이 없습니다"))
    else:
        checks.append(_check("changed_files", PASS, "변경 파일 목록 있음"))

    if deploy_only:
        checks.append(_check("ai_review", PASS, skipped))
    elif not review:
        checks.append(_check("ai_review", FAIL, "승인 차단: AI 리뷰 결과가 없습니다"))
    elif review["verdict"] != "APPROVE":
        detail = f"승인 차단: AI 리뷰 미통과 ({review['verdict']}, score={review['score']})"
        if review["flag_category"]:
            detail += f", category={review['flag_category']}"
        checks.append(_check("ai_review", FAIL, detail))
    else:
        checks.append(_check("ai_review", PASS, f"APPROVE (score={review['score']})"))

    return checks


def first_failure(checks: Sequence[Mapping[str, str]]) -> Optional[Mapping[str, str]]:
    for item in checks:
        if item["status"] == FAIL:
            return item
    return None


async def _run_git(repo: str, *args: str) -> tuple[int, str]:
    """(returncode, stdout). 124=시간 초과, 127=git 실행 불가."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", repo, *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        return 127, ""
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=GIT_TIMEOUT_SEC)
    except asyncio.TimeoutError:
        return 124, ""
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
    return proc.returncode if proc.returncode is not None else 1, out.decode("utf-8", "replace").strip()


class _Undecided(Exception):
    """git 으로 신선도를 판별할 수 없는 경우 (저장소 접근 불가·커밋 없음 등)."""


async def _base_freshness(
    repo: str, sha: str, changed_files: Sequence[str], remote_ref: str = "origin/main",
) -> dict[str, str]:
    """stale_base 예측 — 러너 push 사전판별(classify_push_state)과 같은 조상 관계 기준.

    네트워크를 쓰지 않는다: 이 저장소가 마지막으로 본 origin/main 기준이다.
    """
    key = "base_freshness"
    if not repo or not os.path.isdir(repo):
        raise _Undecided("프로젝트 저장소에 접근할 수 없어 base 신선도를 판별하지 못했습니다")
    rc, _ = await _run_git(repo, "rev-parse", "--git-dir")
    if rc != 0:
        raise _Undecided("프로젝트 디렉터리가 git 저장소가 아니어서 base 신선도를 판별하지 못했습니다")
    if not _SHA_RE.match(sha or ""):
        raise _Undecided("commit_hash 가 없어 base 신선도를 판별하지 못했습니다")
    rc, _ = await _run_git(repo, "cat-file", "-e", f"{sha}^{{commit}}")
    if rc != 0:
        raise _Undecided(f"커밋 {sha[:12]} 이 이 저장소에 없어 판별하지 못했습니다")
    rc, remote_sha = await _run_git(repo, "rev-parse", "--verify", f"{remote_ref}^{{commit}}")
    if rc != 0 or not _SHA_RE.match(remote_sha):
        raise _Undecided(f"{remote_ref} 를 읽지 못해 판별하지 못했습니다 (git rc={rc})")

    rc, _ = await _run_git(repo, "merge-base", "--is-ancestor", sha, remote_sha)
    if rc == 0:
        return _check(key, PASS, f"{sha[:12]} 는 이미 {remote_ref} 에 포함됨")
    if rc != 1:
        raise _Undecided(f"조상 판별 실패 (git rc={rc})")
    rc, _ = await _run_git(repo, "merge-base", "--is-ancestor", remote_sha, sha)
    if rc == 0:
        return _check(key, PASS, f"{remote_ref}({remote_sha[:12]}) 위에서 fast-forward 가능")
    if rc != 1:
        raise _Undecided(f"조상 판별 실패 (git rc={rc})")

    rc, base = await _run_git(repo, "merge-base", remote_sha, sha)
    if rc != 0 or not _SHA_RE.match(base):
        return _check(key, FAIL, f"stale_base 예상: {remote_ref} 와 공통 조상이 없습니다")
    _, ahead = await _run_git(repo, "rev-list", "--count", f"{base}..{remote_sha}")
    note = f"{remote_ref} 가 base({base[:12]}) 보다 {ahead or '?'} 커밋 전진"

    files = list(changed_files)
    if not files:
        _, listed = await _run_git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", sha)
        files = [ln for ln in listed.splitlines() if ln]
    rc, touched_raw = await _run_git(repo, "diff", "--name-only", base, remote_sha)
    if rc != 0:
        return _check(key, WARN, f"stale_base 예상: {note}. 겹치는 파일은 판별하지 못했습니다")
    overlap = sorted(set(files) & {ln for ln in touched_raw.splitlines() if ln})
    if overlap:
        shown = ", ".join(overlap[:5]) + (f" 외 {len(overlap) - 5}개" if len(overlap) > 5 else "")
        return _check(key, FAIL, f"stale_base 예상: {note}. 같은 파일을 건드림: {shown}")
    return _check(key, WARN, f"stale_base 예상: {note}. 겹치는 파일은 없어 자동 rebase 가 가능할 수 있음")


async def check_base_freshness(
    repo: str, sha: str, changed_files: Sequence[str], remote_ref: str = "origin/main",
) -> dict[str, str]:
    """git 전체 작업에 GIT_TIMEOUT_SEC 상한 — 명령별 상한이 쌓여 응답이 늘어지지 않게 한다.

    판별 불가면 warn 을 돌려준다.
    """
    key = "base_freshness"
    try:
        return await asyncio.wait_for(
            _base_freshness(repo, sha, changed_files, remote_ref), timeout=GIT_TIMEOUT_SEC,
        )
    except _Undecided as exc:
        return _check(key, WARN, str(exc))
    except asyncio.TimeoutError:
        return _check(key, WARN, f"git 판별이 {GIT_TIMEOUT_SEC}초를 넘어 중단했습니다")


async def _db_base_freshness_hint(
    conn: Any, *, tenant_id: str, project: str, job_id: str, started_at: Any,
    changed_files: Sequence[str], git_reason: str,
) -> dict[str, str]:
    """git 저장소를 볼 수 없을 때의 근사 — 이 작업이 시작된 뒤 같은 파일을 바꿔 끝난 다른 작업.

    증명이 아니므로 fail 로 올리지 않는다.
    """
    key = "base_freshness"
    base_msg = git_reason
    if not changed_files or started_at is None:
        return _check(key, WARN, base_msg)
    rows = await conn.fetch(
        """
        SELECT job_id, actual_changed_files
        FROM pipeline_jobs
        WHERE tenant_id = $1::uuid AND project = $2 AND job_id <> $3
          AND status IN ('done', 'completed', 'deploying')
          AND completed_at > $4
          AND actual_changed_files ?| $5::text[]
        ORDER BY completed_at DESC
        LIMIT 5
        """,
        tenant_id,
        project,
        job_id,
        started_at,
        list(changed_files),
    )
    if not rows:
        return _check(key, WARN, f"{base_msg} (이 작업 시작 이후 같은 파일을 바꾼 완료 작업은 없음)")
    ids = ", ".join(r["job_id"] for r in rows)
    return _check(key, WARN, f"{base_msg}. 근사: 이 작업 시작 이후 같은 파일을 바꿔 끝난 작업이 있어 stale_base 가능성 — {ids}")


def check_depends_on(parent_id: Optional[str], parent_status: Optional[str]) -> dict[str, str]:
    key = "depends_on"
    if not parent_id:
        return _check(key, PASS, "의존 작업 없음")
    if parent_status is None:
        return _check(key, WARN, f"부모 {parent_id} 를 조회하지 못했습니다")
    if parent_status in _DEAD_PARENT_STATUSES:
        return _check(key, FAIL, f"부모 {parent_id} 가 {parent_status} 상태입니다")
    if parent_status in _PARENT_OK_STATUSES:
        return _check(key, PASS, f"부모 {parent_id} {parent_status}")
    return _check(key, WARN, f"부모 {parent_id} 가 아직 {parent_status} 상태입니다")


def _as_file_list(value: Any) -> list[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    return [str(f) for f in value] if isinstance(value, (list, tuple)) else []


async def precheck(
    job_id: str, *, conn: Any, tenant_id: str, repo_dir: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """승인 전 사전검증. 작업이 없으면 None. 읽기 전용이다."""
    row = await conn.fetchrow(
        "SELECT * FROM pipeline_jobs WHERE job_id = $1 AND tenant_id = $2::uuid",
        job_id,
        tenant_id,
    )
    if not row:
        return None

    status = _get(row, "status")
    if status != AWAITING_APPROVAL:
        return {
            "ok": False,
            "job_id": job_id,
            "checks": [_check("status", FAIL, "승인 대기 상태가 아닙니다")],
        }
    checks = [_check("status", PASS, AWAITING_APPROVAL)]

    changed_files = _get(row, "actual_changed_files") or []
    review = await conn.fetchrow(
        """
        SELECT verdict, score, flag_category, needs_retry
        FROM code_reviews
        WHERE job_id = $1
        ORDER BY created_at DESC
        LIMIT 1
        """,
        job_id,
    )
    deploy_only = is_deploy_only_instruction(_get(row, "instruction") or "")
    commit_hash = (_get(row, "commit_hash") or "").strip()
    checks += evaluate_approval_gates(
        deploy_only=deploy_only,
        git_diff=_get(row, "git_diff") or "",
        commit_hash=commit_hash,
        changed_files=changed_files,
        review=review,
    )

    project = _get(row, "project") or ""
    if deploy_only:
        checks.append(_check("base_freshness", PASS, "DEPLOY_ONLY 작업 — 검사 생략"))
    else:
        if repo_dir is None:
            from app.core.project_config import get_workdir
            repo_dir = get_workdir(project)
        files = _as_file_list(changed_files)
        try:
            fresh = await asyncio.wait_for(
                _base_freshness(repo_dir, commit_hash, files), timeout=GIT_TIMEOUT_SEC,
            )
        except _Undecided as exc:
            fresh = await _db_base_freshness_hint(
                conn, tenant_id=tenant_id, project=project, job_id=job_id,
                started_at=_get(row, "started_at"), changed_files=files, git_reason=str(exc),
            )
        except asyncio.TimeoutError:
            fresh = _check("base_freshness", WARN, f"git 판별이 {GIT_TIMEOUT_SEC}초를 넘어 중단했습니다")
        checks.append(fresh)

    parent_id = _get(row, "depends_on")
    parent_status = None
    if parent_id:
        parent_status = await conn.fetchval(
            "SELECT status FROM pipeline_jobs WHERE job_id = $1 AND tenant_id = $2::uuid",
            parent_id,
            tenant_id,
        )
    checks.append(check_depends_on(parent_id, parent_status))

    busy = await conn.fetchval(
        """
        SELECT job_id FROM pipeline_jobs
        WHERE tenant_id = $1::uuid AND project = $2 AND status = 'deploying' AND job_id <> $3
        LIMIT 1
        """,
        tenant_id,
        project,
        job_id,
    )
    if busy:
        checks.append(_check("deploy_in_progress", WARN, f"같은 프로젝트의 {busy} 가 배포 중 — 승인 후 대기할 수 있습니다"))
    else:
        checks.append(_check("deploy_in_progress", PASS, "진행 중인 같은 프로젝트 배포 없음"))

    return {"ok": first_failure(checks) is None, "job_id": job_id, "checks": checks}
