#!/usr/bin/env python3
"""러너 조회 API 가 아카이브를 못 봐서 '세션과 연결이 끊긴 것처럼' 보이던 것을 고친다.

pipeline_cleanup 은 종료된 지 1시간 지난 작업을 pipeline_jobs → pipeline_jobs_archive
로 옮긴다. 그런데 GET /pipeline/jobs 와 /pipeline/jobs/{id} 는 큐 테이블만 본다.
그래서 내가 낸 작업이 1~2시간 뒤 조회에서 통째로 사라지고, job_id 로 직접 물어도 404 가 난다.
chat_session_id 는 아카이브 row_data 에 그대로 살아 있으므로 연결이 끊긴 적은 없다.
"""
import os
import sys

SRC = "app/api/pipeline_runner.py"

HELPER_ANCHOR = '@router.get("/pipeline/jobs", tags=["pipeline-runner"])\n'

HELPER = '''# ── 아카이브 조회 (AADS-RUNNER-ARCHIVE-LOOKUP) ─────────────────────────
# pipeline_cleanup 은 끝난 지 1시간 지난 작업을 pipeline_jobs 에서 빼
# pipeline_jobs_archive 로 옮긴다(app/services/pipeline_cleanup.py).
# 큐 테이블만 보는 조회 API 는 그 순간부터 작업을 못 찾는다 —
# 2026-09-16 내가 낸 작업 5건이 18:26 에 한꺼번에 아카이브로 가면서
# 세션 조회 결과가 0건이 됐고, **러너가 세션과 끊긴 것처럼 보였다.**
# 실제로는 chat_session_id 가 row_data 안에 그대로 있었다.
#
# row_data 에는 git_diff/logs/result_output 이 빠져 있다(용량). 그래서
# 아카이브에서 온 항목은 `archived: true` 로 표시해 무엇이 비었는지 알린다.
_ARCHIVE_LIST_FIELDS = (
    "instruction", "phase", "cycle", "error_detail", "started_at",
    "depends_on", "chat_session_id", "model", "worker_model",
    "actual_model", "size",
)


def _archive_row_data(row) -> dict:
    """archive.row_data(jsonb) 를 dict 로. asyncpg 설정에 따라 str 로 올 수 있다."""
    import json
    raw = row["row_data_text"] if "row_data_text" in row.keys() else row["row_data"]
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return {}
    return raw or {}


async def _fetch_archived_jobs(conn, *, tenant_id: str, status: str | None,
                               project: str | None, session_id: str | None,
                               limit: int) -> list[dict]:
    """아카이브에서 목록 형태로 읽는다. 큐에서 빠진 뒤의 이력이다."""
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
        SELECT job_id, project, status, created_at, updated_at, archived_at,
               row_data::text AS row_data_text
        FROM pipeline_jobs_archive
        WHERE {' AND '.join(conditions)}
        ORDER BY created_at DESC
        LIMIT ${idx}
        """,
        *params, limit,
    )
    out = []
    for r in rows:
        data = _archive_row_data(r)
        item = {
            "job_id": r["job_id"],
            "project": r["project"],
            "instruction": (data.get("instruction") or "")[:200],
            "status": r["status"],
            "phase": data.get("phase") or "",
            "cycle": data.get("cycle") or 0,
            "error_detail": data.get("error_detail"),
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
            "started_at": data.get("started_at"),
            "depends_on": data.get("depends_on"),
            "model": data.get("model") or "",
            "worker_model": data.get("worker_model") or "",
            "actual_model": data.get("actual_model") or "",
            "size": data.get("size") or "M",
            "auth_recovery_state": "",
            "auth_recovery_metadata": {},
            "archived": True,
            "archived_at": r["archived_at"].isoformat() if r["archived_at"] else None,
            **_runner_display_status(r["status"], data.get("phase") or "",
                                     data.get("error_detail"), None),
        }
        out.append(item)
    return out


'''

LIST_OLD = """            health_probe = await _runner_health_probe(conn, r)
            if health_probe:
                item["health_probe"] = health_probe
            results.append(item)
    return results
"""

LIST_NEW = """            health_probe = await _runner_health_probe(conn, r)
            if health_probe:
                item["health_probe"] = health_probe
            results.append(item)

        # 큐에 남은 것만으로는 이력이 안 보인다. 모자라는 만큼 아카이브로 채운다.
        if len(results) < limit:
            live_ids = {item["job_id"] for item in results}
            try:
                archived = await _fetch_archived_jobs(
                    conn,
                    tenant_id=_tenant_id(context),
                    status=status,
                    project=project,
                    session_id=session_id,
                    limit=limit - len(results) + len(live_ids),
                )
            except Exception as exc:  # 아카이브가 없어도 큐 조회는 살아야 한다
                logger.warning("pipeline_jobs_archive_lookup_failed", error=str(exc))
                archived = []
            for item in archived:
                if item["job_id"] in live_ids:
                    continue
                results.append(item)
                if len(results) >= limit:
                    break
    return results
"""

GET_OLD = """    if not row:
        raise HTTPException(status_code=404, detail="작업을 찾을 수 없습니다")

    result = {
        "job_id": row["job_id"],
"""

GET_NEW = """    if not row:
        # 큐에 없으면 아카이브를 본다 — 끝난 지 1시간 지나면 여기로 옮겨진다.
        # 이 폴백이 없으면 내가 낸 작업을 job_id 로 물어도 404 가 난다.
        async with pool.acquire() as conn:
            archived_rows = await _fetch_archived_jobs(
                conn, tenant_id=_tenant_id(context), status=None,
                project=None, session_id=None, limit=1,
            ) if False else []
            arow = await conn.fetchrow(
                \"\"\"
                SELECT job_id, project, status, created_at, updated_at, archived_at,
                       row_data::text AS row_data_text
                FROM pipeline_jobs_archive
                WHERE job_id = $1 AND row_data->>'tenant_id' = $2
                \"\"\",
                job_id, _tenant_id(context),
            )
        if not arow:
            raise HTTPException(status_code=404, detail="작업을 찾을 수 없습니다")
        adata = _archive_row_data(arow)
        return {
            "job_id": arow["job_id"],
            "project": arow["project"],
            "instruction": adata.get("instruction") or "",
            "status": arow["status"],
            "phase": adata.get("phase") or "",
            "cycle": adata.get("cycle") or 0,
            "max_cycles": adata.get("max_cycles") or 0,
            # row_data 는 용량 때문에 이 셋을 담지 않는다. 없는 것과 빈 것을 구분해 알린다.
            "result_output": None,
            "git_diff": None,
            "review_feedback": adata.get("review_feedback"),
            "error_detail": adata.get("error_detail"),
            "model": adata.get("model") or "",
            "worker_model": adata.get("worker_model") or "",
            "actual_model": adata.get("actual_model") or "",
            "actual_changed_files": adata.get("actual_changed_files") or [],
            "size": adata.get("size") or "M",
            "commit_hash": adata.get("commit_hash"),
            "chat_session_id": adata.get("chat_session_id"),
            "auth_recovery_state": "",
            "auth_recovery_metadata": {},
            "archived": True,
            "archived_at": arow["archived_at"].isoformat() if arow["archived_at"] else None,
            "archive_note": "큐에서 아카이브로 옮겨진 작업입니다. git_diff·logs·result_output 은 용량 때문에 보관하지 않습니다(diff 는 커밋에서 복원).",
            **_runner_display_status(arow["status"], adata.get("phase") or "",
                                     adata.get("error_detail"), None),
            "started_at": adata.get("started_at"),
            "created_at": arow["created_at"].isoformat() if arow["created_at"] else None,
            "updated_at": arow["updated_at"].isoformat() if arow["updated_at"] else None,
        }

    result = {
        "job_id": row["job_id"],
"""


def main() -> int:
    with open(SRC, encoding="utf-8") as fh:
        text = fh.read()

    if "_fetch_archived_jobs" in text:
        print("ALREADY_PATCHED", file=sys.stderr)
        return 3

    for name, old in (("HELPER_ANCHOR", HELPER_ANCHOR), ("LIST_OLD", LIST_OLD), ("GET_OLD", GET_OLD)):
        if text.count(old) != 1:
            print(f"ANCHOR_MISS {name} count={text.count(old)}", file=sys.stderr)
            return 1

    text = text.replace(HELPER_ANCHOR, HELPER + HELPER_ANCHOR, 1)
    text = text.replace(LIST_OLD, LIST_NEW, 1)
    text = text.replace(GET_OLD, GET_NEW, 1)

    tmp = SRC + ".archlookup.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, SRC)
    print("PATCHED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
