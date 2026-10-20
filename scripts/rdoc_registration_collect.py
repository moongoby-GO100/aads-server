#!/usr/bin/env python3
"""R-DOC M5 일일 정본 등록률 수집기 (수동 실행 / 승인 후 타이머 실행).

기본은 읽기 전용 dry-run 이다 — DB 에는 SELECT 만 하고 계산 결과를 JSON 으로 출력한다.
--apply 를 줄 때만 rdoc_registration_daily 에 멱등 upsert 한다. 그 테이블이 아직 없으면
(스키마 설치는 별도 승인) 아무것도 쓰지 않고 종료코드 2 로 끝난다.

  collect  [--start D --end D] [--fetch] [--apply]   신규 문서 등록률(A) + 게이트 ok 비율(B) 계산
  report   [--start D --end D]                       저장된 행으로 날짜별 수집 상태와 7일 연속 여부

날짜는 KST(YYYY-MM-DD). 기본은 어제 하루. 종료코드: 0 정상 / 1 오류 / 2 스키마 미설치로 쓰기 불가.
수집하지 못한 날짜를 0% 나 성공으로 채우지 않는다. 7일 연속 on_time 이 쌓이기 전에는 M5 를 완료로 보지 않는다.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services import rdoc_registration_metrics as m  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "project_document_m3_inventory", ROOT / "scripts" / "project_document_m3_inventory.py")
_inv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_inv)  # connection_params / classified_error 재사용(자격증명 마스킹 포함)

DEFAULT_PROJECT = "AADS"


def _parse_date(s: str) -> dt.date:
    return dt.date.fromisoformat(s)


def _jsonable(o):
    if isinstance(o, (dt.date, dt.datetime)):
        return o.isoformat()
    raise TypeError(type(o).__name__)


def _fetch_origin(repo: str, ref: str) -> bool:
    """ref 가 origin/<branch> 일 때 fetch 가 성공해야 소스 최신성을 확인한 것으로 본다."""
    if not ref.startswith("origin/"):
        return False
    try:
        subprocess.run(["git", "fetch", "origin", ref.split("/", 1)[1], "--quiet"], cwd=repo,
                       check=True, capture_output=True, timeout=60)
        return True
    except Exception:
        return False


async def _connect(read_only: bool):
    import asyncpg

    settings = {"statement_timeout": "30000"}
    if read_only:
        settings["default_transaction_read_only"] = "on"
    return await asyncio.wait_for(
        asyncpg.connect(**_inv.connection_params(), timeout=5, server_settings=settings), timeout=10)


async def collect(args) -> dict:
    m.parse_tenant_uuid(args.tenant_id, "--tenant-id")  # 형식 오류는 DB 연결 전에 거부
    now = dt.datetime.now(m.KST)
    end = args.end or (now.date() - dt.timedelta(days=1))
    start = args.start or end
    repo = str(ROOT)
    verified = _fetch_origin(repo, args.ref) if args.fetch else False
    tip = subprocess.run(["git", "rev-parse", args.ref], cwd=repo, capture_output=True, text=True,
                         check=True, timeout=10).stdout.strip()
    source = {"git_ref": args.ref, "git_ref_sha": tip, "fetched": verified}

    conn = await _connect(read_only=True)
    try:
        async with conn.transaction(readonly=True, isolation="repeatable_read"):
            trusted = await conn.fetchval("SELECT public.aads_internal_tenant_id()::text")
            tenant = m.resolve_trusted_tenant(trusted, args.tenant_id)
            known = [r["project_key"] for r in await conn.fetch(m.KNOWN_PROJECTS_SQL, tenant)]
            files = m.collect_report_history(repo, args.ref, start)
            docs, excluded = m.classify_new_files(files, args.project, known, since=start)
            revisions = [dict(r) for r in await conn.fetch(
                m.REVISIONS_BY_PATH_SQL, tenant, sorted({d["path"] for d in docs}))]
            lo = dt.datetime.combine(start, dt.time(0, 0), tzinfo=m.KST)
            hi = dt.datetime.combine(end + dt.timedelta(days=1), dt.time(0, 0), tzinfo=m.KST)
            try:
                events = [dict(r) for r in await conn.fetch(m.GATE_EVENTS_SQL, lo, hi, args.project)]
                events_error = None
            except Exception as exc:  # canonical_gate_events 미설치 등
                events, events_error = [], _inv.classified_error(exc)
    finally:
        await conn.close()

    rows = m.build_daily_a(
        docs=docs, excluded=excluded, revisions=revisions, tenant_id=tenant,
        projects=sorted({args.project, *[d["project"] for d in docs]}), start=start, end=end,
        collected_at=now, source_verified=verified, source_info=source)
    if events_error is None:
        rows += m.build_daily_b(
            events=events, tenant_id=tenant, project=args.project, start=start, end=end,
            collected_at=now, source_verified=True)

    result = {"mode": "apply" if args.apply else "dry-run", "tenant_id": tenant, "start": start,
              "end": end, "source": source, "rows": rows, "gate_events_error": events_error,
              "written": 0}
    if args.apply:
        wconn = await _connect(read_only=False)
        try:
            async with wconn.transaction():
                result["written"] = await m.upsert_rows(wconn, rows)
        except Exception as exc:
            result["write_error"] = _inv.classified_error(exc)
            result["exit"] = 2 if "schema" in result["write_error"] else 1
        finally:
            await wconn.close()
    return result


async def report(args) -> dict:
    m.parse_tenant_uuid(args.tenant_id, "--tenant-id")  # 형식 오류는 DB 연결 전에 거부
    end = args.end or (dt.datetime.now(m.KST).date() - dt.timedelta(days=1))
    start = args.start or (end - dt.timedelta(days=m.REQUIRED_CONSECUTIVE_DAYS - 1))
    conn = await _connect(read_only=True)
    try:
        trusted = await conn.fetchval("SELECT public.aads_internal_tenant_id()::text")
        tenant = m.resolve_trusted_tenant(trusted, args.tenant_id)
        out = {"tenant_id": tenant, "start": start, "end": end, "reports": []}
        try:
            for metric in m.METRICS:
                rows = [dict(r) for r in await conn.fetch(
                    m.DAILY_ROWS_SQL, tenant, args.project, metric, start, end)]
                out["reports"].append(m.coverage_report(
                    rows, tenant_id=tenant, project=args.project, metric=metric, start=start, end=end))
        except Exception as exc:
            out["error"] = _inv.classified_error(exc)
            out["exit"] = 2 if "schema" in out["error"] else 1
        return out
    finally:
        await conn.close()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("collect", "report"):
        s = sub.add_parser(name)
        s.add_argument("--start", type=_parse_date)
        s.add_argument("--end", type=_parse_date)
        s.add_argument("--project", default=DEFAULT_PROJECT)
        s.add_argument("--tenant-id")
        if name == "collect":
            s.add_argument("--ref", default="origin/main")
            s.add_argument("--fetch", action="store_true",
                           help="origin 을 fetch 해 소스 최신성을 확인한다. 없으면 quality=unverified_source")
            s.add_argument("--apply", action="store_true", help="rdoc_registration_daily 에 upsert (기본: 쓰지 않음)")
    args = p.parse_args()
    try:
        result = asyncio.run(collect(args) if args.cmd == "collect" else report(args))
    except Exception as exc:
        print(json.dumps({"error": _inv.classified_error(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, default=_jsonable))
    return int(result.get("exit", 0))


if __name__ == "__main__":
    sys.exit(main())
