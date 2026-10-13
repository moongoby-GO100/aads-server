#!/usr/bin/env python3
"""러너 작업 결과를 핸드오버 DB(project_handover_entries)에 upsert 하고 다시 읽어 확인한다 (R-001, 2026-10-08).

새 쓰기 경로를 만들지 않는다 — app.services.handover_store.upsert_handover_entry 를 그대로 쓴다.
app 패키지를 import 할 수 없는 호스트(contabo14 등 DB_MODE=psql 원격 러너)에서는 같은 엔드포인트인
POST {AADS_API_URL}/api/v1/handovers 로 폴백한다. 인증은 서버가 이미 쓰는 AADS_MONITOR_KEY(x-monitor-key)이며
러너 EnvironmentFile(/root/.config/aads-runner.env)에만 둔다 — 코드에 하드코딩하지 않는다(R-KEY).
미설정이면 error=http_fallback_unconfigured, HTTP 실패도 예외 없이 사유만 돌려준다(러너 작업은 계속).
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
import urllib.error
import urllib.request

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


def build_entry(payload: dict) -> dict:
    """저장소 직접 쓰기와 HTTP 폴백이 공유하는 필드. 한쪽만 고치면 두 경로가 갈라진다."""
    job = payload.get("job") or {}
    job_id = str(payload["job_id"])
    return {
        "entry_key": entry_key_for(job_id),
        "entry_type": "verification",
        "title": build_title(str(job.get("instruction") or ""), job_id),
        "body": build_body(payload),
        "status": "active",
        "priority": "P2",
        "source_task_id": job_id,
        "metadata": {
            "stage": payload.get("stage"),
            "commit_hash": job.get("commit_hash"),
            "changed_files": len(job.get("changed_files") or []),
        },
        "change_summary": f"runner {payload.get('stage')}",
    }


def http_fallback_write(payload: dict, opener=urllib.request.urlopen, timeout: float = 20.0) -> dict:
    """handover_store 를 import 할 수 없을 때 POST /api/v1/handovers 로 같은 entry 를 upsert 한다. 예외를 던지지 않는다."""
    key = entry_key_for(str(payload.get("job_id") or ""))
    base = (os.getenv("AADS_API_URL") or "").strip().rstrip("/")
    monitor_key = (os.getenv("AADS_MONITOR_KEY") or "").strip()
    if not base or not monitor_key:
        missing = [n for n, v in (("AADS_API_URL", base), ("AADS_MONITOR_KEY", monitor_key)) if not v]
        return {"ok": False, "entry_key": key, "error": f"http_fallback_unconfigured: {','.join(missing)} not set"}
    try:
        entry = build_entry(payload)
        entry["metadata"] = {**entry["metadata"], "source_kind": "runner", "write_path": "http_fallback"}
        body = json.dumps({"project_key": str(payload["project"]), **entry}, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            f"{base}/api/v1/handovers", data=body, method="POST",
            headers={"Content-Type": "application/json", "x-monitor-key": monitor_key},
        )
        with opener(req, timeout=timeout) as resp:
            status = int(getattr(resp, "status", 0) or resp.getcode())
            data = json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        return {"ok": False, "entry_key": key, "error": f"http_fallback_failed: HTTP {exc.code}"}
    except Exception as exc:
        return {"ok": False, "entry_key": key, "error": f"http_fallback_failed: {type(exc).__name__}: {exc}"[:300]}
    saved = data.get("entry") if isinstance(data, dict) else None
    if status not in (200, 201) or not isinstance(saved, dict) or not saved.get("revision"):
        return {"ok": False, "entry_key": key, "error": f"http_fallback_failed: unexpected response status={status}"}
    return {"ok": True, "entry_key": key, "revision": int(saved["revision"]),
            "changed": bool(data.get("changed")), "id": saved.get("id"), "via": "http_fallback"}


async def write_and_verify(payload: dict, store=None) -> dict:
    if store is None:
        try:
            from app.services import handover_store as store
        except ImportError:  # ModuleNotFoundError 포함 — app/ 사본을 원격에 두지 않는다(R-RELEASE).
            return http_fallback_write(payload)

    job_id = str(payload["job_id"])
    project = store.normalize_project_key(str(payload["project"]))
    tenant_id = str(payload["tenant_id"])
    entry, changed = await store.upsert_handover_entry(
        tenant_id=tenant_id,
        project_key=project,
        source_kind="runner",
        changed_by="pipeline-runner",
        **build_entry(payload),
    )
    key = entry_key_for(job_id)
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
