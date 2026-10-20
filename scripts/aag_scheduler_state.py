#!/usr/bin/env python3
"""AAG 스케줄러 실행 상태(owner/heartbeat/skip/실패/catch-up) 기록기.

왜 있는가. 2026-10-20 all-projects 로그에서 GO100 이 "already running — skip" 을
찍고 종료코드 0 으로 끝나자 상위 스크립트가 `result=success` 를 적고 KIS 가 같은
공유 artifact 를 다시 적재했다. skip 이 success 로 읽힌 것이다. 이 모듈은 한 번의
시도를 success / failed / skipped 로 분리해 남기고, **freshness 증거는 success 만
갱신**한다.

종료코드 계약 (셸 스크립트와 공유)
    0  정상 (begin 허가 / finish 기록 / status 조회)
    1  상태 파일을 쓰지 못함 등 실행 오류
    2  사용법 오류, STATE_DIR 이 저장소 안
    3  owner 불일치 (다른 실행이 이어받았다)
    7  SKIP — 장중 회피 구간이거나 다른 실행이 살아 있다. 실패도 성공도 아니다.

상태 파일은 `$AAG_STATE_DIR/scheduler/<PROJECT>.json`. 저장소 밖이어야 하며(워크트리를
더럽히지 않는다) 허용된 필드 외에는 쓰지 않는다 — 시크릿을 받을 필드가 없다.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import socket
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_OWNER_LOST = 3
EXIT_SKIP = 7

SCHEMA = "aag-scheduler-state-v1"
REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATE_DIR = "/var/lib/aads/aag"
KST = timezone(timedelta(hours=9))
MARKET_OPEN = time(8, 0)
MARKET_CLOSE = time(15, 40)
DEFAULT_STALE_MINUTES = 30
REASON_MAX = 200
_PROJECT_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class StateDirError(ValueError):
    """STATE_DIR cannot be used (inside a repository or unusable)."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def in_market_window(now: datetime) -> bool:
    """Weekday 08:00 <= KST < 15:40 — remote scans must stay away from trading hours."""
    local = now.astimezone(KST)
    return local.weekday() < 5 and MARKET_OPEN <= local.time() < MARKET_CLOSE


def clean_reason(value: str | None) -> str:
    return _CONTROL_RE.sub(" ", str(value or "")).strip()[:REASON_MAX]


def resolve_state_dir(raw: str | None = None) -> Path:
    base = Path(raw or os.environ.get("AAG_STATE_DIR") or DEFAULT_STATE_DIR).resolve()
    if base == REPO_ROOT or REPO_ROOT in base.parents:
        raise StateDirError("AAG_STATE_DIR must be outside the repository: %s" % base)
    return base


def _state_paths(state_dir: Path, project: str) -> tuple[Path, Path]:
    if not _PROJECT_RE.match(project):
        raise ValueError("invalid project name")
    directory = state_dir / "scheduler"
    name = project.upper()
    return directory / f"{name}.json", directory / f"{name}.lock"


@contextmanager
def _locked(state_dir: Path, project: str) -> Iterator[Path]:
    path, lock_path = _state_paths(state_dir, project)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield path
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _blank(project: str) -> dict[str, Any]:
    return {
        "schema": SCHEMA, "project": project.upper(), "status": "never_run",
        "owner": None, "pid": None, "host": None,
        "started_at": None, "heartbeat_at": None, "finished_at": None, "exit_code": None,
        "reason": None, "last_success_at": None, "last_failure_at": None,
        "last_failure_reason": None, "last_skip_at": None, "last_skip_reason": None,
        "consecutive_failures": 0, "consecutive_skips": 0, "skips_total": 0,
        "abandoned_runs": 0, "recovered_from": None, "catchup": False,
    }


def load_state(path: Path, project: str) -> dict[str, Any]:
    """A missing or corrupt file is `never_run`/`corrupt`, never a crash or a success."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _blank(project)
    except (OSError, ValueError):
        state = _blank(project)
        state["status"] = "corrupt"
        return state
    if not isinstance(raw, dict) or raw.get("schema") != SCHEMA:
        state = _blank(project)
        state["status"] = "corrupt"
        return state
    return {**_blank(project), **{k: raw.get(k) for k in _blank(project) if k in raw}}


def _write(path: Path, state: dict[str, Any]) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, sort_keys=True, indent=1)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _pid_alive(pid: Any, host: Any) -> bool:
    """Liveness is only knowable for a process on this host; otherwise assume alive."""
    if not isinstance(pid, int) or pid <= 0 or host != socket.gethostname():
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def classify(state: dict[str, Any], now: datetime, stale_minutes: int = DEFAULT_STALE_MINUTES) -> str:
    """running / stale_running / success / partial / failed / skipped / never_run / corrupt."""
    status = state.get("status")
    if status != "running":
        return str(status or "never_run")
    heartbeat = parse_time(state.get("heartbeat_at"))
    if heartbeat is None or now - heartbeat > timedelta(minutes=stale_minutes):
        return "stale_running"
    if not _pid_alive(state.get("pid"), state.get("host")):
        return "stale_running"
    return "running"


def freshness_evidence_at(state: dict[str, Any]) -> str | None:
    """Only a completed successful run is freshness evidence; skips and failures are not."""
    return state.get("last_success_at")


def catchup_due(state: dict[str, Any], now: datetime, after_minutes: int) -> bool:
    if after_minutes <= 0:
        return False
    last = parse_time(freshness_evidence_at(state))
    return last is None or now - last > timedelta(minutes=after_minutes)


def _record_skip(state: dict[str, Any], now: datetime, reason: str) -> None:
    state["last_skip_at"] = iso(now)
    state["last_skip_reason"] = clean_reason(reason)
    state["consecutive_skips"] = int(state.get("consecutive_skips") or 0) + 1
    state["skips_total"] = int(state.get("skips_total") or 0) + 1


def record_skip(state_dir: Path, project: str, reason: str, now: datetime | None = None,
                stale_minutes: int = DEFAULT_STALE_MINUTES) -> dict[str, Any]:
    """Record a skipped attempt without touching a live run's ownership or any success time."""
    now = now or utcnow()
    with _locked(state_dir, project) as path:
        state = load_state(path, project)
        live = classify(state, now, stale_minutes) == "running"
        _record_skip(state, now, reason)
        if not live:
            state["status"] = "skipped"
            state["reason"] = clean_reason(reason)
            state["finished_at"] = iso(now)
        _write(path, state)
    return state


def begin(state_dir: Path, project: str, owner: str, *, now: datetime | None = None,
          market_guard: bool = False, stale_minutes: int = DEFAULT_STALE_MINUTES,
          catchup_after_minutes: int = 0, pid: int | None = None) -> tuple[int, dict[str, Any]]:
    now = now or utcnow()
    with _locked(state_dir, project) as path:
        state = load_state(path, project)
        current = classify(state, now, stale_minutes)
        if market_guard and in_market_window(now):
            _record_skip(state, now, "market_window")
            if current != "running":
                state.update(status="skipped", reason="market_window", finished_at=iso(now))
            _write(path, state)
            return EXIT_SKIP, state
        if current == "running" and state.get("owner") != owner:
            _record_skip(state, now, "running_owner")
            _write(path, state)
            return EXIT_SKIP, state
        if current in ("running", "stale_running") and state.get("owner") != owner:
            state["abandoned_runs"] = int(state.get("abandoned_runs") or 0) + 1
            state["recovered_from"] = {
                "owner": state.get("owner"), "heartbeat_at": state.get("heartbeat_at"),
                "reason": "heartbeat_stale_or_owner_dead",
            }
            state["last_failure_at"] = iso(now)
            state["last_failure_reason"] = "abandoned_run"
            state["consecutive_failures"] = int(state.get("consecutive_failures") or 0) + 1
        state["catchup"] = catchup_due(state, now, catchup_after_minutes)
        state.update(
            status="running", owner=owner, pid=pid if pid is not None else os.getpid(),
            host=socket.gethostname(), started_at=iso(now), heartbeat_at=iso(now),
            finished_at=None, exit_code=None, reason=None, consecutive_skips=0,
        )
        _write(path, state)
    return EXIT_OK, state


def heartbeat(state_dir: Path, project: str, owner: str, *, now: datetime | None = None) -> tuple[int, dict[str, Any]]:
    now = now or utcnow()
    with _locked(state_dir, project) as path:
        state = load_state(path, project)
        if state.get("status") != "running" or state.get("owner") != owner:
            return EXIT_OWNER_LOST, state
        state["heartbeat_at"] = iso(now)
        _write(path, state)
    return EXIT_OK, state


def finish(state_dir: Path, project: str, owner: str, outcome: str, *, reason: str = "",
           exit_code: int | None = None, now: datetime | None = None) -> tuple[int, dict[str, Any]]:
    if outcome not in ("success", "failed", "partial"):
        raise ValueError("outcome must be success, failed or partial")
    now = now or utcnow()
    with _locked(state_dir, project) as path:
        state = load_state(path, project)
        if state.get("status") != "running" or state.get("owner") != owner:
            return EXIT_OWNER_LOST, state
        state.update(status=outcome, finished_at=iso(now), heartbeat_at=iso(now),
                     exit_code=exit_code, reason=clean_reason(reason))
        if outcome == "success":
            state["last_success_at"] = iso(now)
            state["consecutive_failures"] = 0
        elif outcome == "failed":
            state["last_failure_at"] = iso(now)
            state["last_failure_reason"] = clean_reason(reason)
            state["consecutive_failures"] = int(state.get("consecutive_failures") or 0) + 1
        _write(path, state)
    return EXIT_OK, state


def status(state_dir: Path, project: str, *, now: datetime | None = None,
           stale_minutes: int = DEFAULT_STALE_MINUTES, catchup_after_minutes: int = 0) -> dict[str, Any]:
    now = now or utcnow()
    path, _ = _state_paths(state_dir, project)
    state = load_state(path, project)
    state["effective_status"] = classify(state, now, stale_minutes)
    state["catchup_due"] = catchup_due(state, now, catchup_after_minutes)
    state["freshness_evidence_at"] = freshness_evidence_at(state)
    state["market_window"] = in_market_window(now)
    return state


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--state-dir", default=None)
    parser.add_argument("--now", default=None, help="ISO-8601 time (tests only)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def add(name: str, *, owner: bool = False) -> argparse.ArgumentParser:
        item = sub.add_parser(name)
        item.add_argument("project")
        if owner:
            item.add_argument("--owner", required=True)
        return item

    begin_p = add("begin", owner=True)
    begin_p.add_argument("--market-guard", action="store_true")
    begin_p.add_argument("--stale-min", type=int, default=DEFAULT_STALE_MINUTES)
    begin_p.add_argument("--catchup-after-min", type=int, default=0)
    begin_p.add_argument("--pid", type=int, default=None)
    add("heartbeat", owner=True)
    finish_p = add("finish", owner=True)
    finish_p.add_argument("--outcome", choices=("success", "failed", "partial"), required=True)
    finish_p.add_argument("--reason", default="")
    finish_p.add_argument("--exit-code", type=int, default=None)
    skip_p = add("skip")
    skip_p.add_argument("--reason", required=True)
    status_p = add("status")
    status_p.add_argument("--stale-min", type=int, default=DEFAULT_STALE_MINUTES)
    status_p.add_argument("--catchup-after-min", type=int, default=0)
    status_p.add_argument("--field", default=None)
    sub.add_parser("market-window")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    now = parse_time(args.now) if args.now else utcnow()
    if now is None:
        print("invalid --now", file=sys.stderr)
        return EXIT_USAGE
    if args.cmd == "market-window":
        inside = in_market_window(now)
        print(json.dumps({"market_window": inside, "now_kst": now.astimezone(KST).isoformat()}))
        return EXIT_SKIP if inside else EXIT_OK
    try:
        state_dir = resolve_state_dir(args.state_dir)
        if args.cmd == "begin":
            code, state = begin(state_dir, args.project, args.owner, now=now,
                                market_guard=args.market_guard, stale_minutes=args.stale_min,
                                catchup_after_minutes=args.catchup_after_min, pid=args.pid)
        elif args.cmd == "heartbeat":
            code, state = heartbeat(state_dir, args.project, args.owner, now=now)
        elif args.cmd == "finish":
            code, state = finish(state_dir, args.project, args.owner, args.outcome,
                                 reason=args.reason, exit_code=args.exit_code, now=now)
        elif args.cmd == "skip":
            state, code = record_skip(state_dir, args.project, args.reason, now=now), EXIT_OK
        else:
            state = status(state_dir, args.project, now=now, stale_minutes=args.stale_min,
                           catchup_after_minutes=args.catchup_after_min)
            code = EXIT_OK
            if args.field:
                print(state.get(args.field) if state.get(args.field) is not None else "")
                return code
    except StateDirError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_USAGE
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_USAGE
    except OSError as exc:
        print("state I/O failed: %s" % exc, file=sys.stderr)
        return EXIT_ERROR
    print(json.dumps(state, ensure_ascii=False, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
