#!/usr/bin/env python3
"""러너 작업 결과를 핸드오버 DB(project_handover_entries)에 upsert 하고 다시 읽어 확인한다 (R-001, 2026-10-08).

새 쓰기 경로를 만들지 않는다 — app.services.handover_store.upsert_handover_entry 를 그대로 쓴다.
stdin: JSON {"tenant_id","project","job_id","stage","job":{instruction,commit_hash,changed_files,
result_output,review_verdict,review_score,status}}
stdout: 한 줄 JSON {"ok":bool,"revision":int,"changed":bool,"entry_key":str,"error":str}
종료코드: 0=기록·확인 성공, 1=실패
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys

sys.path.insert(0, os.getcwd())
sys.path.insert(0, "/app")

_TEST_LINE = re.compile(r"(\d+\s+(passed|failed|error)|\bPASS(ED)?\b|\bFAIL(ED)?\b|ruff|compileall|dup_guard)", re.I)
_TITLE_LINE = re.compile(r"^\s*TITLE:\s*(.+?)\s*$", re.M)


def ensure_dsn() -> None:
    """호스트(psql 모드) 실행용 — DATABASE_URL 이 없으면 PG* 환경변수로 만든다."""
    if os.getenv("DATABASE_URL") or not os.getenv("PGHOST"):
        return
    from urllib.parse import quote

    os.environ["DATABASE_URL"] = "postgresql://{u}:{p}@{h}:{port}/{d}".format(
        u=quote(os.getenv("PGUSER", "")), p=quote(os.getenv("PGPASSWORD", "")),
        h=os.environ["PGHOST"], port=os.getenv("PGPORT", "5432"), d=quote(os.getenv("PGDATABASE", "")),
    )


def entry_key_for(job_id: str) -> str:
    return f"runner:{job_id}"


def build_title(instruction: str, job_id: str) -> str:
    match = _TITLE_LINE.search(instruction or "")
    if match:
        title = match.group(1)
    else:
        title = next((ln.strip() for ln in (instruction or "").splitlines() if ln.strip()), job_id)
    return title[:200] or job_id


def build_body(payload: dict) -> str:
    job = payload.get("job") or {}
    result = str(job.get("result_output") or "")
    files = [str(f) for f in (job.get("changed_files") or []) if str(f).strip()]
    test_lines = [ln.strip()[:200] for ln in result.splitlines() if _TEST_LINE.search(ln)][:8]
    parts = [
        f"## 러너 작업 결과 ({payload.get('stage') or 'unknown'})",
        f"- job_id: {payload.get('job_id')}",
        f"- 상태: {job.get('status') or '-'}",
        f"- 커밋: {job.get('commit_hash') or '(없음)'}",
        f"- 리뷰: {job.get('review_verdict') or '-'} ({job.get('review_score') or '-'})",
        "",
        f"## 변경 파일 ({len(files)})",
        *(f"- {f}" for f in files[:60]),
        *([f"- … 외 {len(files) - 60}개"] if len(files) > 60 else []),
        "",
        "## 테스트 결과",
        *(f"- {ln}" for ln in test_lines),
        *([] if test_lines else ["- (결과 출력에서 테스트 줄을 찾지 못함)"]),
        "",
        "## 결과 요약",
        result.strip()[:3000] or "(결과 출력 없음)",
    ]
    return "\n".join(parts)


async def write_and_verify(payload: dict, store=None) -> dict:
    if store is None:
        from app.services import handover_store as store

    job_id = str(payload["job_id"])
    key = entry_key_for(job_id)
    job = payload.get("job") or {}
    project = store.normalize_project_key(str(payload["project"]))
    tenant_id = str(payload["tenant_id"])
    entry, changed = await store.upsert_handover_entry(
        tenant_id=tenant_id,
        project_key=project,
        entry_key=key,
        entry_type="verification",
        title=build_title(str(job.get("instruction") or ""), job_id),
        body=build_body(payload),
        status="active",
        priority="P2",
        source_kind="runner",
        source_task_id=job_id,
        metadata={
            "stage": payload.get("stage"),
            "commit_hash": job.get("commit_hash"),
            "changed_files": len(job.get("changed_files") or []),
        },
        changed_by="pipeline-runner",
        change_summary=f"runner {payload.get('stage')}",
    )
    async with store._connection() as conn:
        row = await conn.fetchrow(
            "SELECT revision FROM project_handover_entries "
            "WHERE tenant_id=$1::uuid AND project_key=$2 AND entry_key=$3",
            tenant_id, project, key,
        )
    if row is None:
        return {"ok": False, "entry_key": key, "error": "readback_missing"}
    return {"ok": True, "entry_key": key, "revision": int(row["revision"]), "changed": bool(changed), "id": entry.get("id")}


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read())
        ensure_dsn()
        result = asyncio.run(write_and_verify(payload))
    except Exception as exc:  # 러너는 이 실패로 작업을 실패시키지 않는다 — 사유만 남긴다.
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
