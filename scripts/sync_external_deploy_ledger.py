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
        # 정본은 nginx 실측이다(2026-09-15 CEO 결정). state_file 의 active_slot 은
        # 배포 스크립트가 적어 둔 "의도"이고, 실제로 트래픽을 받는 슬롯은 여기다.
        "upstream_file": "/etc/nginx/conf.d/go100-backend-upstream.conf",
    },
    # NTV2(cafe24_114) 는 블루/그린이 아니라 컨테이너 교체 방식이라 릴리스
    # 디렉터리가 없다. 배포 스크립트가 `aads-record-release` 로 직접 사실을
    # 남기고, 여기서는 그 큐만 읽는다(releases_root 없음).
    "NTV2": {
        "host": "114.207.244.86",
        "component": "frontend",
        "deploy_type": "rolling",
        "queue_file": "/var/lib/aads-release-control/NTV2/release-queue.tsv",
        "state_file": "/etc/aads-release/ntv2-release-state",
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


def parse_nginx_active_port(raw: str) -> str:
    """upstream 설정에서 실제로 트래픽을 받는 포트를 돌려준다.

    `backup` 이 붙은 줄은 장애 시에만 쓰이므로 활성이 아니다. 활성 서버 줄이
    여러 개면(가중치 분산) 판정하지 않고 빈 문자열을 돌려준다 — 추측 금지.
    """
    active: list[str] = []
    for line in raw.splitlines():
        stripped = line.split("#", 1)[0].strip()
        if not stripped.startswith("server "):
            continue
        if "backup" in stripped or "down" in stripped:
            continue
        target = stripped.split()[1].rstrip(";")
        if ":" in target:
            active.append(target.rsplit(":", 1)[1])
    return active[0] if len(active) == 1 else ""


def resolve_slots(state: dict[str, str], nginx_port: str) -> tuple[str, str, str]:
    """(current_slot, candidate_slot, mismatch_note) 를 돌려준다.

    current_slot 은 nginx 실측 포트를 state 의 포트-슬롯 대응표로 되돌린 값이다.
    실측이 없거나 대응되는 슬롯이 없으면 state 의 active_slot 을 쓰되, 그때는
    mismatch_note 에 근거를 남긴다.
    """
    active_slot = state.get("active_slot", "")
    standby_slot = state.get("standby_slot", "")
    port_to_slot = {
        state.get("active_port", ""): active_slot,
        state.get("standby_port", ""): standby_slot,
    }
    port_to_slot.pop("", None)

    if not nginx_port:
        return active_slot, standby_slot, "nginx_upstream_unreadable"
    measured = port_to_slot.get(nginx_port, "")
    if not measured:
        return active_slot, standby_slot, f"nginx_port_unmapped={nginx_port}"
    if measured != active_slot:
        # 실측이 정본이다. state 는 뒤집힌 것으로 본다.
        other = active_slot if measured == standby_slot else standby_slot
        return measured, other, f"slot_mismatch state={active_slot} nginx={measured}:{nginx_port}"
    return measured, standby_slot, ""


def to_dt(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def collect(project: str, cfg: dict[str, str], cutoff: datetime) -> list[dict[str, Any]]:
    host = cfg["host"]
    queue = parse_queue(ssh_read(host, f"cat {cfg['queue_file']} 2>/dev/null || true"))
    releases_root = cfg.get("releases_root", "")
    dirs: dict[str, datetime] = {}
    if releases_root:
        dirs = parse_release_dirs(ssh_read(
            host,
            f"find {releases_root} -mindepth 1 -maxdepth 1 -printf '%f\\t%T@\\n' 2>/dev/null || true",
        ))
    state = parse_state(ssh_read(host, f"cat {cfg['state_file']} 2>/dev/null || true"))
    active_sha = state.get("release_sha", "")
    active_phase = state.get("phase", "")

    upstream_file = cfg.get("upstream_file", "")
    nginx_port = ""
    if upstream_file:
        nginx_port = parse_nginx_active_port(
            ssh_read(host, f"cat {upstream_file} 2>/dev/null || true")
        )
    current_slot, candidate_slot, slot_note = resolve_slots(state, nginx_port)
    if slot_note:
        print(f"[warn] {project} 슬롯 실측 주의: {slot_note}", file=sys.stderr)

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
        elif status == "failed":
            # 배포 스크립트가 직접 실패를 남긴 경우에만 실패로 적는다.
            # 파일이 말하지 않는 실패를 만들어내지는 않는다(R-CRITICAL).
            run_status, phase = "failed", "failed"
        else:
            continue

        if sha == active_sha and active_phase:
            phase = active_phase  # 예: rollback

        evidence = [f"release_queue={status}"]
        if deployed_at:
            evidence.append(f"release_dir_mtime={deployed_at.astimezone().isoformat(timespec='seconds')}")
        is_active_release = sha == active_sha
        if is_active_release:
            evidence.append(f"state_active_slot={state.get('active_slot', '?')}:{state.get('active_port', '?')}")
            evidence.append(f"nginx_active_port={nginx_port or 'unreadable'}")
            if slot_note:
                evidence.append(slot_note)

        rows.append({
            "project": project,
            "component": cfg["component"],
            "deploy_type": cfg["deploy_type"],
            "release_sha": sha,
            "runner_job_id": item["queue_id"],
            "status": run_status,
            "phase": phase,
            # 슬롯은 지금 트래픽을 받는 릴리스에만 기록한다. 과거 행에 현재
            # 슬롯을 적으면 이력 전체가 지금 상태로 오염된다.
            "current_slot": current_slot if is_active_release else None,
            "candidate_slot": candidate_slot if is_active_release else None,
            "started_at": queued_at or event_at,
            "completed_at": event_at,
            "requested_by": (item["owner"] or "unknown")[:120],
            "request_source": "external_release_sync",
            "release_title": (item["task_id"] or f"{project} release {sha}")[:180],
            "release_summary": (item["note"] or "; ".join(evidence))[:240],
            # 실패 사유는 화면(배포 탭)이 읽는 컬럼에 넣어야 보인다.
            "error_summary": (item["note"] or f"release_queue={status}")[:240] if run_status in ("failed", "blocked") else None,
            "payload": {
                "source": "sync_external_deploy_ledger",
                "host": host,
                "queue_file": cfg["queue_file"],
                "queue_status": status,
                "session": item["session"],
                "evidence": evidence,
                "is_active_release": is_active_release,
                "slot_truth_source": "nginx_upstream",
                "slot_mismatch": slot_note or None,
            },
        })
    rows.sort(key=lambda r: r["completed_at"])
    return rows


UPSERT_SQL = """
INSERT INTO deploy_runs(
    project, component, deploy_type, target_env, release_sha,
    runner_job_id, status, phase,
    phase_started_at, phase_completed_at, queue_position,
    current_slot, candidate_slot, error_summary,
    requested_by, request_source, commit_status, push_status,
    auto_start, request_payload, requested_at, last_heartbeat_at,
    release_title, release_summary, approval_policy,
    created_at, updated_at
) VALUES (
    %(project)s, %(component)s, %(deploy_type)s, 'production', %(release_sha)s,
    %(runner_job_id)s, %(status)s, %(phase)s,
    %(started_at)s, %(completed_at)s, 0,
    %(current_slot)s, %(candidate_slot)s, %(error_summary)s,
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
    current_slot = EXCLUDED.current_slot,
    candidate_slot = EXCLUDED.candidate_slot,
    error_summary = EXCLUDED.error_summary,
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
