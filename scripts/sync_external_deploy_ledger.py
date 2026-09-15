#!/usr/bin/env python3
"""외부 서버 프로젝트의 배포 사실을 AADS 중앙 배포 원장(deploy_runs)에 동기화한다.

왜 필요한가
-----------
GO100(contabo14) 은 자체 blue/green 컨트롤러(`deploy-go100.sh`)로 릴리스를 올린다.
이 경로는 AADS API 를 한 번도 호출하지 않으므로 `deploy_runs` 에 아무 것도 남지 않고,
채팅 아티팩트 '배포' 탭에는 GO100 배포가 영원히 보이지 않는다(2026-09-15 CEO 지적).
비-AADS 프로젝트가 원장에 들어오는 유일한 경로는 Pipeline Runner 가 `done` 으로 끝났을
때의 `register_external_deploy()` 인데, GO100 러너 369건 중 `done` 은 0건이었다.

무엇을 사실로 취급하는가 (추측 금지, R-CRITICAL)
------------------------------------------------
세 파일이 말하는 것만 기록한다.
  1. `/var/lib/aads-release-control/GO100/release-queue.tsv`
     릴리스 큐. 한 줄 = id, sha, status, owner, session, task_id, updated_at, note.
  2. `/opt/go100/backend-releases/<sha>`
     릴리스 디렉터리. 존재 + mtime = 그 SHA 가 그 시각에 슬롯에 설치됐다는 뜻.
  3. `/etc/go100/backend-release-state`
     지금 활성 릴리스와 phase(deployed/rollback 등).

상태 매핑:
  queue=deployed                      -> status=success,  phase=completed
  queue=approved + 릴리스 디렉터리 有 -> status=success,  phase=released      (배포 실행됨)
  queue=approved + 디렉터리 無        -> 건너뜀 (승인만 된 것은 배포가 아니다)
  queue=blocked                       -> status=blocked,  phase=blocked
  queue=pending/rejected              -> 건너뜀
활성 릴리스는 release-state 의 phase 를 그대로 덮어쓴다(예: rollback).
"실패"는 어느 파일도 말하지 않으므로 만들어내지 않는다.

사용
----
  python3 scripts/sync_external_deploy_ledger.py --dry-run          # 확인만
  python3 scripts/sync_external_deploy_ledger.py --days 14          # 기본 적용
  python3 scripts/sync_external_deploy_ledger.py --days 400         # 전체 백필
멱등하다. runner_job_id = 큐 id 로 UNIQUE(project, runner_job_id) 에 UPSERT 한다.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

ENV_FILE = "/root/aads/aads-server/.env"
SSH_TIMEOUT = 30  # R-BG: 백그라운드/원격 호출에는 반드시 시간 상한을 건다.

# 프로젝트별 외부 릴리스 상태 위치. 새 프로젝트는 여기만 추가하면 된다.
SOURCES: dict[str, dict[str, str]] = {
    "GO100": {
        "host": "5.104.86.14",
        "component": "backend",
        "deploy_type": "bluegreen",
        "queue_file": "/var/lib/aads-release-control/GO100/release-queue.tsv",
        "releases_root": "/opt/go100/backend-releases",
        "state_file": "/etc/go100/backend-release-state",
    },
}

SKIP_QUEUE_STATUSES = {"pending", "awaiting_review", "rejected"}


def ssh_read(host: str, command: str) -> str:
    """원격 셸 명령의 stdout 을 돌려준다. 실패하면 빈 문자열."""
    try:
        proc = subprocess.run(
            [
                "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
                "-o", "StrictHostKeyChecking=no", f"root@{host}", command,
            ],
            capture_output=True, text=True, timeout=SSH_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        print(f"[warn] ssh timeout host={host}", file=sys.stderr)
        return ""
    if proc.returncode != 0:
        print(f"[warn] ssh rc={proc.returncode} host={host}: {proc.stderr.strip()[:200]}", file=sys.stderr)
    return proc.stdout


def load_db_env() -> dict[str, str]:
    env: dict[str, str] = {}
    try:
        with open(ENV_FILE, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                if key.startswith("AADS_DB_"):
                    env[key] = value.strip().strip('"').strip("'")
    except OSError as exc:
        print(f"[fatal] .env 읽기 실패: {exc}", file=sys.stderr)
        raise
    return env


def parse_queue(raw: str) -> dict[str, dict[str, str]]:
    """release-queue.tsv -> {sha: row}. 같은 SHA 는 마지막 줄이 최신이다."""
    rows: dict[str, dict[str, str]] = {}
    for line in raw.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        rows[parts[1].strip()] = {
            "queue_id": parts[0].strip(),
            "sha": parts[1].strip(),
            "status": parts[2].strip().lower(),
            "owner": parts[3].strip(),
            "session": parts[4].strip(),
            "task_id": parts[5].strip(),
            "updated_at": parts[6].strip(),
            "note": parts[7].strip() if len(parts) > 7 else "",
        }
    return rows


def parse_release_dirs(raw: str) -> dict[str, datetime]:
    """`find -printf '%f\\t%T@'` 출력 -> {sha: mtime(UTC)}"""
    out: dict[str, datetime] = {}
    for line in raw.splitlines():
        if "\t" not in line:
            continue
        name, epoch = line.split("\t", 1)
        try:
            out[name.strip()] = datetime.fromtimestamp(float(epoch), tz=timezone.utc)
        except ValueError:
            continue
    return out


def parse_state(raw: str) -> dict[str, str]:
    state: dict[str, str] = {}
    for line in raw.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            state[key.strip()] = value.strip()
    return state


def to_dt(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def collect(project: str, cfg: dict[str, str], cutoff: datetime) -> list[dict[str, Any]]:
    host = cfg["host"]
    queue = parse_queue(ssh_read(host, f"cat {cfg['queue_file']} 2>/dev/null || true"))
    dirs = parse_release_dirs(ssh_read(
        host,
        f"find {cfg['releases_root']} -mindepth 1 -maxdepth 1 -printf '%f\\t%T@\\n' 2>/dev/null || true",
    ))
    state = parse_state(ssh_read(host, f"cat {cfg['state_file']} 2>/dev/null || true"))
    active_sha = state.get("release_sha", "")
    active_phase = state.get("phase", "")

    rows: list[dict[str, Any]] = []
    for sha, item in queue.items():
        status = item["status"]
        if status in SKIP_QUEUE_STATUSES:
            continue
        queued_at = to_dt(item["updated_at"])
        deployed_at = dirs.get(sha)
        event_at = deployed_at or queued_at
        if event_at is None or event_at < cutoff:
            continue

        if status == "deployed":
            run_status, phase = "success", "completed"
        elif status == "approved":
            if deployed_at is None:
                continue  # 승인만 되고 릴리스가 설치되지 않았다 = 배포가 아니다
            run_status, phase = "success", "released"
        elif status == "blocked":
            run_status, phase = "blocked", "blocked"
        else:
            continue

        if sha == active_sha and active_phase:
            phase = active_phase  # 예: rollback

        evidence = [f"release_queue={status}"]
        if deployed_at:
            evidence.append(f"release_dir_mtime={deployed_at.astimezone().isoformat(timespec='seconds')}")
        if sha == active_sha:
            evidence.append(f"active_slot={state.get('active_slot', '?')}:{state.get('active_port', '?')}")

        rows.append({
            "project": project,
            "component": cfg["component"],
            "deploy_type": cfg["deploy_type"],
            "release_sha": sha,
            "runner_job_id": item["queue_id"],
            "status": run_status,
            "phase": phase,
            "started_at": queued_at or event_at,
            "completed_at": event_at,
            "requested_by": (item["owner"] or "unknown")[:120],
            "request_source": "external_release_sync",
            "release_title": (item["task_id"] or f"{project} release {sha}")[:180],
            "release_summary": (item["note"] or "; ".join(evidence))[:240],
            "payload": {
                "source": "sync_external_deploy_ledger",
                "host": host,
                "queue_file": cfg["queue_file"],
                "queue_status": status,
                "session": item["session"],
                "evidence": evidence,
                "is_active_release": sha == active_sha,
            },
        })
    rows.sort(key=lambda r: r["completed_at"])
    return rows


UPSERT_SQL = """
INSERT INTO deploy_runs(
    project, component, deploy_type, target_env, release_sha,
    runner_job_id, status, phase,
    phase_started_at, phase_completed_at, queue_position,
    requested_by, request_source, commit_status, push_status,
    auto_start, request_payload, requested_at, last_heartbeat_at,
    release_title, release_summary, approval_policy,
    created_at, updated_at
) VALUES (
    %(project)s, %(component)s, %(deploy_type)s, 'production', %(release_sha)s,
    %(runner_job_id)s, %(status)s, %(phase)s,
    %(started_at)s, %(completed_at)s, 0,
    %(requested_by)s, %(request_source)s, 'committed', 'pushed',
    true, %(payload)s::jsonb, %(started_at)s, %(completed_at)s,
    %(release_title)s, %(release_summary)s, 'auto_if_green',
    %(completed_at)s, NOW()
)
ON CONFLICT (project, runner_job_id) WHERE runner_job_id IS NOT NULL
DO UPDATE SET
    status = EXCLUDED.status,
    phase = EXCLUDED.phase,
    release_sha = EXCLUDED.release_sha,
    phase_completed_at = EXCLUDED.phase_completed_at,
    release_title = EXCLUDED.release_title,
    release_summary = EXCLUDED.release_summary,
    request_payload = EXCLUDED.request_payload,
    updated_at = NOW()
RETURNING id, (xmax = 0) AS inserted
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="외부 프로젝트 배포를 중앙 원장에 동기화")
    parser.add_argument("--days", type=int, default=14, help="며칠치를 동기화할지 (기본 14)")
    parser.add_argument("--project", default="", help="특정 프로젝트만 (기본: 전체)")
    parser.add_argument("--dry-run", action="store_true", help="DB 를 건드리지 않고 출력만")
    args = parser.parse_args()

    cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)
    targets = {k: v for k, v in SOURCES.items() if not args.project or k == args.project.upper()}
    if not targets:
        print(f"[fatal] 알 수 없는 프로젝트: {args.project}", file=sys.stderr)
        return 2

    all_rows: list[dict[str, Any]] = []
    for project, cfg in targets.items():
        rows = collect(project, cfg, cutoff)
        print(f"[collect] {project}: {len(rows)}건 (cutoff={cutoff.date()}, host={cfg['host']})")
        all_rows.extend(rows)

    if args.dry_run:
        for row in all_rows:
            print(f"  {row['completed_at'].astimezone().isoformat(timespec='minutes')} "
                  f"{row['project']}/{row['component']} {row['release_sha']} "
                  f"{row['status']}/{row['phase']} {row['release_title']}")
        print(f"[dry-run] {len(all_rows)}건 — DB 변경 없음")
        return 0

    if not all_rows:
        print("[apply] 대상 없음")
        return 0

    import psycopg2  # 호스트 실행 전제. 컨테이너 내부에서는 asyncpg 경로를 쓴다.

    env = load_db_env()
    conn = psycopg2.connect(
        host=os.getenv("SYNC_DB_HOST", "127.0.0.1"),
        port=int(os.getenv("SYNC_DB_PORT", "5433")),
        dbname=env.get("AADS_DB_NAME", "aads"),
        user=env.get("AADS_DB_USER", "aads"),
        password=env.get("AADS_DB_PASSWORD", ""),
        connect_timeout=10,
    )
    inserted = updated = 0
    try:
        with conn, conn.cursor() as cur:
            for row in all_rows:
                params = dict(row)
                params["payload"] = json.dumps(row["payload"], ensure_ascii=False)
                cur.execute(UPSERT_SQL, params)
                _, is_new = cur.fetchone()
                if is_new:
                    inserted += 1
                else:
                    updated += 1
    finally:
        conn.close()
    print(f"[apply] 신규 {inserted}건 / 갱신 {updated}건 / 합계 {len(all_rows)}건")
    return 0


if __name__ == "__main__":
    sys.exit(main())
