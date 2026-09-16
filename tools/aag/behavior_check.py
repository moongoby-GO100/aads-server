#!/usr/bin/env python3
"""AAG L3 — 런타임 행위 규칙 점검기 (AADS Architecture Governance).

L1(구조)·L2(계약)은 코드를 읽어 잡는다. 그런데 2026-09-16 하루에 난 사고 셋은
코드가 문법·구조상 멀쩡한데도 났다.

  1. 10:28:51 러너 스크립트 동기화가 GO100 runner-1791da41(P0)을 재시작으로
     죽였다 → runner_shutdown_requeued, 6분 16초치 LLM 작업 폐기.
  2. runner-3c82de0b 이 승인 SHA 의 base 가 낡아 push 거부됐는데 원인이
     push_fail 하나로 뭉뚱그려져 사람이 매번 수동 대조했다.
  3. 11:06 승인된 GO100 P0 청산 게이트가 배포 후 health check 실패로
     자동 revert 됐다 → 안전장치가 장중에 사라졌다.

셋 다 "설계가 어떤 경우를 안 다뤘다" 이고, 흔적은 코드가 아니라 **실행 기록**에
남는다. 이 스크립트는 그 기록(pipeline_jobs / pipeline_runner_events /
pipeline_runner_hosts)을 규칙으로 읽어 사람이 로그를 뒤지기 전에 먼저 찾는다.

읽기 전용(SELECT)만 수행한다. 종료코드 0=지적 없음 / 1=지적 있음 / 2=실행 불가.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

KST = timezone(timedelta(hours=9))
FIELD_SEP = "\x1e"

PG_CONTAINER = os.environ.get("PG_CONTAINER", "aads-postgres")
PGUSER = os.environ.get("PGUSER", "aads")
PGDATABASE = os.environ.get("PGDATABASE", "aads")

# 좀비 판정: 러너 호스트 하트비트가 이만큼 끊기면 그 호스트의 in-flight 는 유령이다.
# 실측(2026-09-16 11:07)으로 하트비트 최대 지연은 457초였다. 20분은 그 2.6배다.
HOST_STALE_MINUTES = int(os.environ.get("AAG_HOST_STALE_MINUTES", "20"))

# 종료 phase 중 "사람이 봐야 하는 것"만 고른다. 정상 종료(done)는 지적하지 않는다.
TERMINAL_FAILURE_PHASES = {
    "push_fail": "승인된 커밋을 원격에 반영하지 못함",
    "push_stale_base": "승인 SHA 의 base 가 낡아 push 불가 — 재작업·재승인 필요",
    "health_check_fail_rollback": "배포 후 헬스체크 실패로 자동 revert — 변경이 되돌아감",
    "deploy_lock_fail": "배포 락 획득 실패 — 다른 배포가 장시간 점유",
    "deploy_worktree_not_isolated": "격리 worktree 검증 실패로 배포 차단",
    "deploy_commit_sha_mismatch": "승인 SHA 와 worktree HEAD 불일치",
}


def _psql(sql: str) -> list[list[str]]:
    """읽기 전용 질의. 실패하면 RuntimeError."""
    if not sql.lstrip().upper().startswith("SELECT"):
        raise RuntimeError("read-only: SELECT only")
    proc = subprocess.run(
        [
            "docker", "exec", "-i", PG_CONTAINER,
            "psql", "-U", PGUSER, "-d", PGDATABASE,
            "-q", "-t", "-A", "-P", "footer=off", "-F", FIELD_SEP,
            "-c", sql,
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"psql failed: {proc.stderr.strip()[:300]}")
    return parse_psql_output(proc.stdout)


def parse_psql_output(text: str) -> list[list[str]]:
    """psql -A -F '\\x1e' 출력을 행·열로 나눈다.

    str.splitlines() 를 쓰면 안 된다 — 파이썬은 \\x1e(RECORD SEPARATOR)도
    줄바꿈으로 취급해서 필드 구분자가 행 구분자로 둔갑한다(실측: 4행 2열이
    8행 1열로 쪼개졌다). 그래서 행은 \\n 으로만 나눈다.
    """
    rows = []
    for line in text.split("\n"):
        if not line.strip():
            continue
        rows.append(line.split(FIELD_SEP))
    return rows


# ── 규칙 (순수 함수 — DB 없이 단위테스트한다) ──────────────────────────


def find_requeue_waste(events: Iterable[dict]) -> list[dict]:
    """재시작으로 버려진 작업과 폐기된 시간.

    job_started 뒤에 job_requeued 가 오면 그 사이 시간이 통째로 버려진 것이다.
    러너는 requeue 로 작업을 잃지는 않지만 LLM 시간과 비용은 잃는다.
    """
    by_job: dict[str, list[dict]] = {}
    for ev in events:
        by_job.setdefault(ev["job_id"], []).append(ev)

    findings = []
    for job_id, evs in by_job.items():
        evs = sorted(evs, key=lambda e: e["observed_at"])
        started_at = None
        for ev in evs:
            if ev["event_type"] == "job_started":
                started_at = ev["observed_at"]
            elif ev["event_type"] == "job_requeued" and started_at is not None:
                wasted = (ev["observed_at"] - started_at).total_seconds()
                findings.append({
                    "rule": "JOB_REQUEUED_BY_RESTART",
                    "severity": "P1",
                    "job_id": job_id,
                    "project": ev.get("project", ""),
                    "at": ev["observed_at"],
                    "detail": (
                        f"러너 재시작으로 작업이 되돌려짐 — {int(wasted // 60)}분 "
                        f"{int(wasted % 60)}초치 작업 폐기"
                    ),
                    "wasted_seconds": int(wasted),
                })
                started_at = None
    return findings


def find_terminal_failures(jobs: Iterable[dict]) -> list[dict]:
    """사람이 봐야 하는 종료 상태."""
    findings = []
    for job in jobs:
        phase = (job.get("phase") or "").strip()
        meaning = TERMINAL_FAILURE_PHASES.get(phase)
        if not meaning:
            continue
        findings.append({
            "rule": phase.upper(),
            "severity": "P0" if phase == "health_check_fail_rollback" else "P1",
            "job_id": job.get("job_id", ""),
            "project": job.get("project", ""),
            "at": job.get("updated_at"),
            "detail": meaning,
        })
    return findings


def find_zombie_jobs(jobs: Iterable[dict], hosts: Iterable[dict], now: datetime) -> list[dict]:
    """러너가 죽었는데 DB 에만 살아 있는 작업.

    이 상태는 두 가지를 동시에 망친다 — 작업은 영원히 끝나지 않고,
    busy 게이트(runner_busy_lib.sh)가 그 호스트의 동기화를 영원히 미룬다.
    """
    last_seen = {h["host"]: h["last_seen_at"] for h in hosts}
    limit = timedelta(minutes=HOST_STALE_MINUTES)
    findings = []
    for job in jobs:
        host = (job.get("runner_host") or "").strip()
        if not host:
            continue
        seen = last_seen.get(host)
        if seen is None:
            age_text = "하트비트 기록 없음"
        elif now - seen <= limit:
            continue
        else:
            age_text = f"하트비트 {int((now - seen).total_seconds() // 60)}분 끊김"
        findings.append({
            "rule": "ZOMBIE_INFLIGHT_JOB",
            "severity": "P1",
            "job_id": job.get("job_id", ""),
            "project": job.get("project", ""),
            "at": job.get("updated_at"),
            "detail": f"host={host} 이 {age_text} 인데 작업이 {job.get('status')} 로 남아 있음",
        })
    return findings


# ── 수집 ────────────────────────────────────────────────────────────


def _ts(raw: str) -> datetime:
    return datetime.fromisoformat(raw.replace(" ", "T"))


def collect(hours: int) -> dict[str, list[dict]]:
    events = [
        {
            "job_id": r[0],
            "event_type": r[1],
            "observed_at": _ts(r[2]),
            "project": r[3],
        }
        for r in _psql(
            "SELECT e.job_id, e.event_type, e.created_at, COALESCE(j.project,'') "
            "FROM pipeline_runner_events e "
            "LEFT JOIN pipeline_jobs j ON j.job_id = e.job_id "
            f"WHERE e.created_at > NOW() - INTERVAL '{int(hours)} hours' "
            "AND e.event_type IN ('job_started','job_requeued') "
            "ORDER BY e.created_at"
        )
        if len(r) >= 4
    ]
    jobs = [
        {
            "job_id": r[0],
            "project": r[1],
            "status": r[2],
            "phase": r[3],
            "runner_host": r[4],
            "updated_at": _ts(r[5]),
        }
        for r in _psql(
            "SELECT job_id, project, status, COALESCE(phase,''), COALESCE(runner_host,''), updated_at "
            "FROM pipeline_jobs "
            f"WHERE updated_at > NOW() - INTERVAL '{int(hours)} hours' "
            "ORDER BY updated_at"
        )
        if len(r) >= 6
    ]
    hosts = [
        {"host": r[0], "last_seen_at": _ts(r[1])}
        for r in _psql("SELECT host, last_seen_at FROM pipeline_runner_hosts")
        if len(r) >= 2
    ]
    return {"events": events, "jobs": jobs, "hosts": hosts}


def render_markdown(findings: list[dict], hours: int) -> str:
    now = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")
    lines = [
        f"# AAG L3 행위 점검 — 최근 {hours}시간",
        "",
        f"생성 {now} · 지적 {len(findings)}건",
        "",
    ]
    if not findings:
        lines.append("지적 없음.")
        return "\n".join(lines)
    lines += [
        "| 심각도 | 규칙 | 작업 | 프로젝트 | 시각(KST) | 내용 |",
        "|---|---|---|---|---|---|",
    ]
    for f in sorted(findings, key=lambda x: (x["severity"], x["at"] or datetime.min)):
        at = f["at"].astimezone(KST).strftime("%m-%d %H:%M") if f.get("at") else "-"
        lines.append(
            f"| {f['severity']} | `{f['rule']}` | `{f['job_id']}` | {f['project']} | {at} | {f['detail']} |"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="AAG L3 런타임 행위 점검")
    ap.add_argument("--hours", type=int, default=24, help="조회 구간(기본 24시간)")
    ap.add_argument("--json", action="store_true", help="JSON 출력")
    ap.add_argument("--out", help="결과를 파일로도 저장")
    args = ap.parse_args(argv)

    try:
        data = collect(args.hours)
    except Exception as exc:  # noqa: BLE001 — 실행 불가와 지적 없음을 반드시 구분한다
        print(f"ERROR: 수집 실패 — {exc}", file=sys.stderr)
        return 2

    now = datetime.now(timezone.utc)
    findings: list[dict] = []
    findings += find_requeue_waste(data["events"])
    findings += find_terminal_failures(data["jobs"])
    findings += find_zombie_jobs(
        [j for j in data["jobs"] if j["status"] in ("claimed", "running", "deploying")],
        data["hosts"],
        now,
    )

    if args.json:
        payload = [
            {**f, "at": f["at"].isoformat() if f.get("at") else None}
            for f in findings
        ]
        out = json.dumps({"hours": args.hours, "count": len(findings), "findings": payload},
                         ensure_ascii=False, indent=2)
    else:
        out = render_markdown(findings, args.hours)

    print(out)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(out + "\n")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
