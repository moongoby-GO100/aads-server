#!/usr/bin/env python3
"""Host-side worker for allowlisted local and SSH deployment targets."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import threading
import time
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services.deploy_adapters.targets import ExecutionTarget, get_execution_target

PSQL = ("docker", "exec", "aads-postgres", "psql", "-U", "aads", "-d", "aads", "-qAt")
TERMINAL = {"success", "failed", "blocked", "cancelled", "superseded"}


def sql_text(value: object, limit: int = 2000) -> str:
    text = str(value or "")[:limit].replace("\x00", "").replace("'", "''")
    return f"'{text}'"


def db(sql: str, *, check: bool = True) -> str:
    proc = subprocess.run((*PSQL, "-c", sql), text=True, capture_output=True, timeout=30)
    if check and proc.returncode:
        raise RuntimeError(f"database command failed: {proc.stderr.strip()[:500]}")
    return proc.stdout.strip()


def row_for_run(run_id: int) -> dict[str, str]:
    raw = db(
        "SELECT project, component, release_sha, status, target_env "
        f"FROM deploy_runs WHERE id={run_id};"
    )
    if not raw:
        raise RuntimeError(f"deploy run {run_id} not found")
    values = raw.split("|")
    if len(values) != 5:
        raise RuntimeError("unexpected deploy_runs row shape")
    return dict(zip(("project", "component", "release_sha", "status", "target_env"), values))


def event(run_id: int, phase: str, status: str, detail: str = "") -> None:
    db(
        "INSERT INTO deploy_phase_events(deploy_run_id, phase, status, phase_started_at, "
        "phase_completed_at, error_summary, metadata) VALUES "
        f"({run_id},{sql_text(phase)},{sql_text(status)},NOW(),NOW(),{sql_text(detail)},"
        "'{\"source\":\"unified_component_worker\"}'::jsonb);",
        check=False,
    )


def update_run(run_id: int, status: str, phase: str, detail: str = "") -> None:
    completed = (
        ", phase_completed_at=NOW(), "
        "duration_ms=(EXTRACT(EPOCH FROM (NOW()-COALESCE(phase_started_at,created_at)))*1000)::bigint"
        if status in TERMINAL else ""
    )
    error = f", error_summary={sql_text(detail)}" if detail else ""
    db(
        "UPDATE deploy_runs SET "
        f"status={sql_text(status)}, phase={sql_text(phase)}, updated_at=NOW(), "
        f"last_heartbeat_at=NOW(){completed}{error} WHERE id={run_id};"
        "UPDATE deploy_components SET "
        f"status={sql_text(status)}, phase={sql_text(phase)}, updated_at=NOW()"
        + (
            ", completed_at=NOW(), duration_ms=(EXTRACT(EPOCH FROM "
            "(NOW()-COALESCE(started_at,created_at)))*1000)::bigint"
            if status in TERMINAL else ", started_at=COALESCE(started_at,NOW())"
        )
        + f", log_path={sql_text(f'/root/aads/aads-server/logs/unified-component-{run_id}.log')}"
        + (f", error_summary={sql_text(detail)}" if detail else "")
        + f" WHERE deploy_run_id={run_id};"
    )
    event(run_id, phase, status, detail)


def acquire_lease(run_id: int, target: ExecutionTarget, owner: str) -> None:
    project, component = target.key
    env = row_for_run(run_id)["target_env"] or "production"
    db(
        "INSERT INTO deploy_locks(project,component,target_env,deploy_run_id,owner_instance,owner_epoch,"
        "lease_expires_at,heartbeat_at,metadata) VALUES "
        f"({sql_text(project)},{sql_text(component)},{sql_text(env)},{run_id},{sql_text(owner)},"
        f"{sql_text(owner)},NOW()+INTERVAL '90 seconds',NOW(),'{{}}'::jsonb) "
        "ON CONFLICT(project,component,target_env) DO UPDATE SET "
        "deploy_run_id=EXCLUDED.deploy_run_id,owner_instance=EXCLUDED.owner_instance,"
        "owner_epoch=EXCLUDED.owner_epoch,lease_expires_at=EXCLUDED.lease_expires_at,"
        "heartbeat_at=NOW() WHERE deploy_locks.lease_expires_at < NOW() "
        f"OR deploy_locks.deploy_run_id={run_id};"
    )
    holder = db(
        "SELECT deploy_run_id FROM deploy_locks WHERE "
        f"project={sql_text(project)} AND component={sql_text(component)} AND target_env={sql_text(env)};"
    )
    if holder != str(run_id):
        raise RuntimeError(f"component lease held by deploy run {holder or 'unknown'}")


def heartbeat(run_id: int, target: ExecutionTarget, owner: str, stop: threading.Event) -> None:
    project, component = target.key
    while not stop.wait(20):
        db(
            f"UPDATE deploy_runs SET last_heartbeat_at=NOW(),updated_at=NOW() WHERE id={run_id};"
            "UPDATE deploy_locks SET heartbeat_at=NOW(),lease_expires_at=NOW()+INTERVAL '90 seconds' "
            f"WHERE project={sql_text(project)} AND component={sql_text(component)} "
            f"AND deploy_run_id={run_id} AND owner_instance={sql_text(owner)};",
            check=False,
        )


def release_lease(run_id: int, target: ExecutionTarget, owner: str) -> None:
    project, component = target.key
    db(
        "DELETE FROM deploy_locks WHERE "
        f"project={sql_text(project)} AND component={sql_text(component)} "
        f"AND deploy_run_id={run_id} AND owner_instance={sql_text(owner)};",
        check=False,
    )


def remote_argv(target: ExecutionTarget, command: Iterable[str]) -> list[str]:
    remote = shlex.join(tuple(command))
    return [
        "ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
        "-o", "ConnectTimeout=10", "-p", str(target.port), f"root@{target.host}", remote,
    ]


def run_command(target: ExecutionTarget, command: tuple[str, ...], timeout: int = 60) -> subprocess.CompletedProcess[str]:
    argv = list(command) if target.executor == "local" else remote_argv(target, command)
    return subprocess.run(argv, text=True, capture_output=True, timeout=timeout)


def preflight(target: ExecutionTarget, release_sha: str) -> None:
    if len(release_sha) < 7 or not all(ch in "0123456789abcdefABCDEF" for ch in release_sha):
        raise RuntimeError("release SHA must be a hexadecimal Git object id")
    if target.repo_path:
        check_ref = ("git", "-C", target.repo_path, "cat-file", "-e", f"{release_sha}^{{commit}}")
        ref_result = run_command(target, check_ref)
        if ref_result.returncode:
            raise RuntimeError(f"release SHA is not present in {target.repo_path}")
        if target.executor == "ssh":
            head_result = run_command(target, ("git", "-C", target.repo_path, "rev-parse", "HEAD"))
            if head_result.returncode or not head_result.stdout.strip().startswith(release_sha):
                raise RuntimeError(
                    f"remote HEAD {head_result.stdout.strip()[:12] or 'unknown'} does not match release {release_sha[:12]}"
                )
        if target.require_clean_repo:
            dirty = run_command(target, ("git", "-C", target.repo_path, "status", "--porcelain"))
            if dirty.returncode or dirty.stdout.strip():
                count = len([line for line in dirty.stdout.splitlines() if line.strip()])
                raise RuntimeError(f"dirty deployment repository ({count} paths)")


def deploy(target: ExecutionTarget, release_sha: str, run_id: int) -> None:
    command = (*target.command, release_sha, str(run_id)) if target.executor == "local" else target.command
    result = run_command(target, command, timeout=target.timeout_seconds)
    detail = (result.stdout + "\n" + result.stderr).strip()[-4000:]
    if result.returncode:
        raise RuntimeError(f"deploy command failed ({result.returncode}): {detail}")


def verify_health(target: ExecutionTarget) -> None:
    if target.health_command:
        health = run_command(target, target.health_command, timeout=60)
        health_detail = (health.stdout + "\n" + health.stderr).strip()[-1000:]
        if health.returncode:
            raise RuntimeError(f"post-deploy health failed ({health.returncode}): {health_detail}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True, type=int)
    parser.add_argument("--trigger", default="manual")
    parser.add_argument("--lock-file", required=True)
    args = parser.parse_args()
    owner = f"unified-component:{os.getpid()}:{int(time.time())}"
    stop = threading.Event()
    target: ExecutionTarget | None = None
    thread: threading.Thread | None = None
    current_phase = "host_preflight"
    try:
        row = row_for_run(args.run_id)
        if row["status"] not in {"queued", "claimed"}:
            print(f"run {args.run_id} is not dispatchable: {row['status']}", flush=True)
            return 0
        target = get_execution_target(row["project"], row["component"])
        if target is None:
            raise RuntimeError(f"no execution target for {row['project']}/{row['component']}")
        acquire_lease(args.run_id, target, owner)
        update_run(args.run_id, "running", "host_preflight")
        thread = threading.Thread(target=heartbeat, args=(args.run_id, target, owner, stop), daemon=True)
        thread.start()
        preflight(target, row["release_sha"])
        current_phase = "deploying"
        update_run(args.run_id, "running", "deploying")
        deploy(target, row["release_sha"], args.run_id)
        current_phase = "post_deploy_health"
        update_run(args.run_id, "verifying", "post_deploy_health")
        verify_health(target)
        update_run(args.run_id, "success", "release_certified")
        print(json.dumps({"ok": True, "run_id": args.run_id, "target": target.key}), flush=True)
        return 0
    except subprocess.TimeoutExpired as exc:
        detail = f"deployment timed out after {exc.timeout}s"
        update_run(args.run_id, "failed", "timeout", detail)
        print(detail, file=sys.stderr, flush=True)
        return 1
    except Exception as exc:
        detail = str(exc)[:1800]
        try:
            if current_phase == "host_preflight":
                update_run(args.run_id, "blocked", "host_preflight_blocked", detail)
            else:
                update_run(args.run_id, "failed", current_phase, detail)
        except Exception:
            pass
        print(detail, file=sys.stderr, flush=True)
        return 1
    finally:
        stop.set()
        if thread is not None:
            thread.join(timeout=2)
        if target is not None:
            release_lease(args.run_id, target, owner)
        try:
            Path(args.lock_file).unlink(missing_ok=True)
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
