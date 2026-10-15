"""클로브 수집 1시간 자동 실행 — 연결된 회사마다 obys_collection_service.run_collection 을 부른다.

원장 확정은 하지 않는다(수집은 검토함까지). 앱 lifespan 에는 붙지 않고 CLI(--once)로만 돈다:
    python -m app.services.obys_collection_scheduler --once
호출은 카페24 systemd timer → 래퍼 → docker exec 이다(deploy/acct/).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import socket
import sys
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any

from app.services import clobe_mcp_client as clobe
from app.services import obys_collection_core as core
from app.services import obys_collection_service as svc

logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))
BACKFILL_DAYS = 3
BUDGET_SECONDS = 840
INSTANCE_PREFIX = "obys-scheduler"

#: 한 번이라도 있으면 exit 1. partial(일시 오류)은 next_retry_at 백오프가 이어받으므로 실패로 세지 않는다.
FAILURE_STATUSES = frozenset({"failed", "error", "lease_lost", "needs_reauth"})
_SEVERITY = ("needs_reauth", "lease_lost", "error", "failed", "partial", "succeeded")
_SCOPE_CODES = frozenset({"company_blocked", "tenant_not_in_collection_scope", "company_not_linked"})


def instance_name() -> str:
    """수동 실행(기본 aads-api)과 달라야 한다 — 같은 이름이면 acquire_lease 가 재진입으로 보고 임대를 가로챈다."""
    return f"{INSTANCE_PREFIX}:{socket.gethostname()}:{os.getpid()}"


def compute_period(last_covered_to: date | None, today: date) -> tuple[date, date, bool]:
    """(시작, 끝, 잘림). 이력이 있으면 마지막 성공 covered_to 3일 전부터, 없으면 기본 창(90일)."""
    if last_covered_to is None:
        start, end = core.resolve_period(None, today, today=today)
        return start, end, False
    start = min(last_covered_to - timedelta(days=BACKFILL_DAYS), today)
    floor = today - timedelta(days=core.MAX_WINDOW_DAYS)
    if start < floor:
        return floor, today, True
    return start, today, False


async def load_state() -> tuple[list[dict[str, Any]], dict[str, dict[str, dict[str, Any]]]]:
    conn = await svc._connect()
    try:
        links = await conn.fetch(
            "SELECT clobe_company_id FROM obys_clobe_company_link "
            "WHERE link_status='linked' AND permission_ok IS NOT FALSE ORDER BY clobe_company_id")
        states = await conn.fetch(
            "SELECT clobe_company_id, data_kind, status, last_success_at, covered_to, next_retry_at "
            "FROM obys_clobe_collection_state")
    finally:
        await conn.close()
    by_company: dict[str, dict[str, dict[str, Any]]] = {}
    for row in states:
        by_company.setdefault(row["clobe_company_id"], {})[row["data_kind"]] = dict(row)
    return [dict(r) for r in links], by_company


async def _reauth_confirmed() -> bool:
    """클로브 연결 상태가 실제로 재인증 필요일 때만 True. 확인에 실패하면 False — 건너뛰지 않고 실행해 본다."""
    try:
        return bool((await clobe.get_status()).get("reauth_required"))
    except Exception:  # noqa: BLE001 - 확인 불가는 '필요 없음'으로 취급한다
        logger.warning("obys_scheduler_reauth_status_unavailable")
        return False


def _plan_groups(kind_states: dict[str, dict[str, Any]], now: datetime, today: date) -> tuple[list[dict[str, Any]], list[str]]:
    """실행할 (기간, 종류들) 묶음과 백오프로 빠진 종류. 같은 기간인 종류는 한 번의 run_collection 으로 묶는다."""
    groups: dict[tuple[date, date], dict[str, Any]] = {}
    deferred: list[str] = []
    for kind in core.COLLECTION_KINDS:
        st = kind_states.get(kind) or {}
        retry = st.get("next_retry_at")
        if retry is not None and retry > now:
            deferred.append(kind)
            continue
        covered = st.get("covered_to") if st.get("last_success_at") else None
        start, end, clamped = compute_period(covered, today)
        g = groups.setdefault((start, end), {"start": start, "end": end, "kinds": [], "clamped": False})
        g["kinds"].append(kind)
        g["clamped"] = g["clamped"] or clamped
    return list(groups.values()), deferred


def _worst(statuses: list[str]) -> str:
    for s in _SEVERITY:
        if s in statuses:
            return s
    return "succeeded"


async def _run_company(
    company_id: str, kind_states: dict[str, dict[str, Any]], now: datetime, today: date, instance: str,
    deadline: float,
) -> dict[str, Any]:
    groups, deferred = _plan_groups(kind_states, now, today)
    out: dict[str, Any] = {"company_id": company_id, "deferred_kinds": deferred, "runs": []}
    if not groups:
        out["status"] = "skipped_backoff"
        return out
    statuses: list[str] = []
    for g in groups:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            statuses.append("error")
            out["detail"] = "budget_exhausted"
            break
        try:
            res = await asyncio.wait_for(
                svc.run_collection(company_id, kinds=g["kinds"], period_start=g["start"], period_end=g["end"],
                                   mode="live", instance=instance),
                timeout=remaining)
        except svc.CollectionError as exc:
            if exc.code == "collection_in_progress":
                out["status"] = "skipped_busy" if not statuses else _worst(statuses)
                out["detail"] = exc.code
                return out
            if exc.code in _SCOPE_CODES:
                out["status"] = "skipped_scope"
                out["detail"] = exc.code
                return out
            statuses.append("error")
            out["detail"] = exc.code
            break
        except asyncio.TimeoutError:
            statuses.append("error")
            out["detail"] = "timeout"
            break
        except Exception as exc:  # noqa: BLE001 - 한 회사의 예외가 다른 회사를 막지 않는다
            logger.warning("obys_scheduler_company_failed type=%s", type(exc).__name__)
            statuses.append("error")
            out["detail"] = type(exc).__name__
            break
        status = str(res.get("status") or "error")
        statuses.append(status)
        out["runs"].extend(
            {"kind": r.get("data_kind"), "status": r.get("status"), "error_code": r.get("error_code")}
            for r in res.get("runs") or [])
        if res.get("error_code"):
            out["detail"] = res["error_code"]
        out.setdefault("period", [g["start"].isoformat(), g["end"].isoformat()])
        if g["clamped"]:
            out["period_clamped"] = True
        if status in ("needs_reauth", "lease_lost"):
            break
    out["status"] = _worst(statuses)
    return out


async def run_due(now: datetime | None = None, *, budget_seconds: float = BUDGET_SECONDS) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    today = now.astimezone(KST).date()
    instance = instance_name()
    started = time.monotonic()
    deadline = started + budget_seconds
    links, states = await load_state()
    companies: list[dict[str, Any]] = []
    reauth_confirmed = False
    for link in links:
        cid = link["clobe_company_id"]
        kind_states = states.get(cid, {})
        if time.monotonic() >= deadline:
            companies.append({"company_id": cid, "status": "skipped_budget"})
            continue
        flagged = any((s.get("status") == "needs_reauth") for s in kind_states.values())
        if reauth_confirmed or (flagged and await _reauth_confirmed()):
            reauth_confirmed = True
            companies.append({"company_id": cid, "status": "skipped_needs_reauth"})
            continue
        result = await _run_company(cid, kind_states, now, today, instance, deadline)
        companies.append(result)
        if result["status"] == "needs_reauth" and await _reauth_confirmed():
            reauth_confirmed = True
    tally: dict[str, int] = {}
    for c in companies:
        tally[c["status"]] = tally.get(c["status"], 0) + 1
    budget_exhausted = tally.get("skipped_budget", 0) > 0
    ok = not budget_exhausted and not any(s in FAILURE_STATUSES for s in tally)
    return {
        "ok": ok, "instance": instance, "today_kst": today.isoformat(), "total": len(companies),
        "tally": tally, "budget_exhausted": budget_exhausted,
        "elapsed_seconds": round(time.monotonic() - started, 1), "companies": companies,
    }


async def run_backfill(
    kinds: list[str], start: date, end: date, *, company_ids: list[str] | None = None, dry_run: bool = False,
    pause_seconds: float = 2.0, budget_seconds: float = 3000,
) -> dict[str, Any]:
    """지정 종류·기간을 연결된 회사마다 순서대로 다시 받는다(클로브 호출은 회사 하나씩, 회사 사이 pause).

    run_collection 의 임대(lease)·기간 상한(MAX_WINDOW_DAYS)을 그대로 따른다. 적재는 source_key 멱등이라 다시 돌려도 안전하다.
    dry_run 은 대상 회사와 기간만 돌려주고 클로브·DB 쓰기를 하지 않는다.
    """
    if any(k not in core.COLLECTION_KINDS for k in kinds):
        raise ValueError("invalid_kind")
    start, end = core.resolve_period(start, end)
    links, _ = await load_state()
    targets = [str(link["clobe_company_id"]) for link in links]
    if company_ids:
        targets = [c for c in targets if c in set(company_ids)]
    plan = {"kinds": kinds, "period": [start.isoformat(), end.isoformat()], "companies": len(targets), "dry_run": dry_run}
    if dry_run:
        return {"ok": True, **plan, "results": []}
    instance = instance_name()
    deadline = time.monotonic() + budget_seconds
    results: list[dict[str, Any]] = []
    for i, cid in enumerate(targets):
        if time.monotonic() >= deadline:
            results.extend({"company_id": c, "status": "skipped_budget"} for c in targets[i:])
            break
        if i:
            await asyncio.sleep(pause_seconds)
        try:
            res = await svc.run_collection(cid, kinds=kinds, period_start=start, period_end=end, mode="live", instance=instance)
        except svc.CollectionError as exc:
            results.append({"company_id": cid, "status": "skipped_busy" if exc.code == "collection_in_progress" else "error",
                            "detail": exc.code})
            continue
        except Exception as exc:  # noqa: BLE001 - 한 회사의 예외가 다른 회사를 막지 않는다
            results.append({"company_id": cid, "status": "error", "detail": type(exc).__name__})
            continue
        results.append({"company_id": cid, "status": str(res.get("status") or "error"), "detail": res.get("error_code"),
                        "runs": [{"kind": r.get("data_kind"), "status": r.get("status"), "error_code": r.get("error_code"),
                                  "counts": r.get("counts")} for r in res.get("runs") or []]})
        if res.get("status") == "needs_reauth":
            results.extend({"company_id": c, "status": "skipped_needs_reauth"} for c in targets[i + 1:])
            break
    bad = [r for r in results if r["status"] in FAILURE_STATUSES or r["status"] == "skipped_budget"]
    return {"ok": not bad, **plan, "results": results}


def _apply_runtime() -> dict[str, Any] | None:
    """오비서 독립 실행 설정을 적용한다. 앱의 create_app 이 하는 단계이며, 빠지면 clobe store_mode()=aads 로 AADS 풀을 찾는다.

    성공이면 None, 실패하면 출력할 오류 JSON. 예외 메시지는 담지 않는다(경로·DSN 노출 방지).
    """
    try:
        from app.core.obys_runtime import RuntimeSettings

        RuntimeSettings.from_env().apply()
    except Exception as exc:  # noqa: BLE001 - 설정이 없으면 수집을 시작하지 않는다
        return {"ok": False, "error": "runtime_config", "detail": type(exc).__name__, "companies": []}
    return None


async def _main_async(args: argparse.Namespace | None = None) -> int:
    failure = _apply_runtime()
    if failure is not None:
        print(json.dumps(failure, ensure_ascii=False, separators=(",", ":")))
        return 1
    try:
        if args is not None and args.backfill_from:
            result = await run_backfill(
                args.kinds or [core.KIND_CHANNEL], date.fromisoformat(args.backfill_from),
                date.fromisoformat(args.backfill_to) if args.backfill_to else datetime.now(KST).date(),
                company_ids=args.company or None, dry_run=args.dry_run, pause_seconds=args.pause_seconds)
        else:
            result = await run_due()
    except Exception as exc:  # noqa: BLE001 - 대상 조회 자체가 실패한 경우도 JSON 한 줄로 남긴다
        result = {"ok": False, "error": type(exc).__name__, "companies": []}
    finally:
        try:
            await clobe.close_store_pool()
        except Exception:  # noqa: BLE001
            pass
    print(json.dumps(result, ensure_ascii=False, default=str, separators=(",", ":")))
    return 0 if result.get("ok") else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="클로브 수집 1시간 자동 실행")
    parser.add_argument("--once", action="store_true", required=True, help="한 번 돌고 종료")
    parser.add_argument("--backfill-from", metavar="YYYY-MM-DD", help="지정하면 정기 실행 대신 이 날짜부터 다시 받는다(수동)")
    parser.add_argument("--backfill-to", metavar="YYYY-MM-DD", help="재수집 끝 날짜(기본 오늘 KST)")
    parser.add_argument("--kinds", nargs="+", choices=list(core.COLLECTION_KINDS), help="재수집 종류(기본 channel_revenue)")
    parser.add_argument("--company", action="append", help="재수집할 clobe_company_id(반복 가능, 기본 연결된 전체)")
    parser.add_argument("--dry-run", action="store_true", help="재수집 대상만 출력하고 호출하지 않는다")
    parser.add_argument("--pause-seconds", type=float, default=2.0, help="회사 사이 대기(기본 2초)")
    args = parser.parse_args(argv)
    if args.backfill_to and not args.backfill_from:
        parser.error("--backfill-to 는 --backfill-from 과 함께 쓴다")
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    sys.exit(main())
