#!/usr/bin/env python3
"""AAG 운영 PostgreSQL 사본 기준 백업 → 일회용 PG15 복원 → 정합성 검증기.

scripts/aag_restore_verify.py(sqlite 거울)가 증명하지 못하는 부분 — pg_dump/pg_restore,
트리거, FK, advisory lock — 을 실제 PostgreSQL 15 로 검증한다.

안전 구조
* 운영 DB 쪽은 `docker exec aads-postgres psql` 의 **READ ONLY REPEATABLE READ 트랜잭션** 하나와
  그 스냅샷에 붙는 `pg_dump` 뿐이다. 서버가 쓰기를 거부하고, 클라이언트도 쓰기 SQL 을 보내기 전에 막는다.
* 복원은 `docker run --network none --rm` 일회용 컨테이너에만 한다. 운영 compose 는 쓰지 않는다.
* aag_scanner_credentials.token_hash 는 덤프에서 제외하고 id 기반 가짜 값으로 대체해 적재한다.
  원본 값은 메모리에만 두고 덤프 파일에 남았는지 바이트 스캔한 뒤 버린다(출력·보고서에 쓰지 않는다).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import select
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

AAG_TABLES: tuple[str, ...] = (
    "aag_api_consumer_events", "aag_audit_events", "aag_baseline_findings", "aag_baselines",
    "aag_central_outage_overrides", "aag_finding_exceptions", "aag_graph_snapshots",
    "aag_graph_snapshots_v2", "aag_latest_pointers", "aag_project_grants", "aag_ref_heads",
    "aag_revalidation_jobs", "aag_rule_lifecycle", "aag_scan_runs", "aag_scanner_credentials",
    "aag_snapshot_observations",
)
SECRET_COLUMNS: dict[str, tuple[str, ...]] = {"aag_scanner_credentials": ("token_hash",)}
SCOPE_KEYS = ("project", "repository_id", "target_ref", "governance_scope")
POINTER_COMPARE_FIELDS = ("snapshot_id", "observation_id", "run_id", "resolved_commit_sha",
                          "expected_target_ref_head_sha", "verified_at")
EXPECTED_TRIGGERS: dict[tuple[str, str], frozenset[str]] = {
    ("aag_audit_events", "trg_aag_audit_append_only"): frozenset({"UPDATE", "DELETE"}),
    ("aag_graph_snapshots_v2", "trg_aag_ready_snapshot_immutable"): frozenset({"UPDATE", "DELETE"}),
    ("aag_baselines", "trg_aag_protect_approved_baseline"): frozenset({"UPDATE", "DELETE"}),
    ("aag_baseline_findings", "trg_aag_protect_approved_baseline_findings"):
        frozenset({"INSERT", "UPDATE", "DELETE"}),
    ("aag_rule_lifecycle", "trg_aag_protect_enforced_rule"): frozenset({"UPDATE", "DELETE"}),
}
SOURCE_CONTAINER = "aads-postgres"
DB_USER, DB_NAME = "aads", "aads"
DEFAULT_TIMEOUT_SECONDS = 1800
MASK_PREFIX = "aag-restore-drill-mask:"
DRILL_NAME_PREFIX = "aag-restore-drill"


class DrillError(Exception):
    """A step could not run (as opposed to a verification finding)."""


class DrillTimeout(DrillError):
    pass


class SessionError(DrillError):
    pass


class Deadline:
    def __init__(self, seconds: float, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._end = clock() + seconds

    def remaining(self) -> float:
        left = self._end - self._clock()
        if left <= 0:
            raise DrillTimeout("전체 시간 상한 초과")
        return left


# ── 순수 로직 (DB 없이 단위테스트) ────────────────────────────────────────────────────
Failure = dict[str, str]


def scope_of(row: dict[str, Any]) -> tuple[str, ...]:
    return tuple(str(row[k]) for k in SCOPE_KEYS)


def runner_job_label(env: dict[str, str], repo_root: str) -> str:
    job = env.get("AADS_HEAVY_JOB_ID", "")
    if not re.fullmatch(r"runner-[0-9A-Za-z_-]+", job):
        match = re.fullmatch(r"/tmp/aads-wt-(runner-[0-9A-Za-z_-]+)", repo_root)
        job = match.group(1) if match else ""
    return job


_WRITE_WORDS = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|CREATE|ALTER|TRUNCATE|GRANT|REVOKE|COPY|VACUUM|REINDEX|CLUSTER|"
    r"REFRESH|COMMENT|LOCK|DO|CALL|SET\s+ROLE)\b", re.IGNORECASE)


def is_read_only_sql(sql: str) -> bool:
    """클라이언트측 방어선. 문자열 리터럴을 지운 뒤 쓰기 키워드가 있으면 거부한다."""
    stripped = re.sub(r"'(?:[^']|'')*'", "''", sql)
    return _WRITE_WORDS.search(stripped) is None


def mask_token_sql() -> str:
    return (f"encode(sha256(convert_to('{MASK_PREFIX}'||c.id::text,'utf8')),'hex')")


def compare_counts(source: dict[str, dict[str, Any]], restored: dict[str, dict[str, Any]],
                   tables: Sequence[str] = AAG_TABLES) -> tuple[list[dict[str, Any]], list[Failure]]:
    rows, failures = [], []
    for table in tables:
        src, dst = source.get(table), restored.get(table)
        if src is None or dst is None:
            failures.append({"code": "table_missing", "detail": table})
            rows.append({"table": table, "source": None if src is None else src["n"],
                         "restored": None if dst is None else dst["n"], "count_ok": False,
                         "digest_ok": False})
            continue
        count_ok = src["n"] == dst["n"]
        digest_ok = src["digest"] == dst["digest"]
        if not count_ok:
            failures.append({"code": "row_count_mismatch", "detail": f"{table}: {src['n']} != {dst['n']}"})
        if not digest_ok:
            failures.append({"code": "content_digest_mismatch", "detail": table})
        rows.append({"table": table, "source": src["n"], "restored": dst["n"],
                     "count_ok": count_ok, "digest_ok": digest_ok})
    return rows, failures


def compare_schema(source: dict[str, dict[str, list[str]]],
                   restored: dict[str, dict[str, list[str]]]) -> list[Failure]:
    failures: list[Failure] = []
    for table in sorted(set(source) | set(restored)):
        if table not in source or table not in restored:
            failures.append({"code": "schema_table_missing", "detail": table})
            continue
        for part in ("columns", "constraints", "indexes"):
            if source[table].get(part) != restored[table].get(part):
                failures.append({"code": f"schema_{part}_mismatch", "detail": table})
    return failures


def trigger_events(row: dict[str, Any]) -> frozenset[str]:
    events = set()
    if row.get("ins"):
        events.add("INSERT")
    if row.get("upd"):
        events.add("UPDATE")
    if row.get("del"):
        events.add("DELETE")
    return frozenset(events)


def check_triggers(source: list[dict[str, Any]], restored: list[dict[str, Any]],
                   expected: dict[tuple[str, str], frozenset[str]] = EXPECTED_TRIGGERS) -> list[Failure]:
    def index(rows: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
        return {(r["table"], r["name"]): r for r in rows}

    src, dst = index(source), index(restored)
    failures: list[Failure] = []
    for key, events in sorted(expected.items()):
        label = f"{key[0]}.{key[1]}"
        row = dst.get(key)
        if row is None:
            failures.append({"code": "trigger_missing", "detail": label})
            continue
        if row.get("enabled") != "O":
            failures.append({"code": "trigger_disabled", "detail": label})
        if not row.get("before"):
            failures.append({"code": "trigger_not_before", "detail": label})
        if not events <= trigger_events(row):
            failures.append({"code": "trigger_events_mismatch", "detail": label})
    for key in sorted(set(src) ^ set(dst)):
        failures.append({"code": "trigger_set_differs_from_source", "detail": f"{key[0]}.{key[1]}"})
    for key in sorted(set(src) & set(dst)):
        if trigger_events(src[key]) != trigger_events(dst[key]) or src[key].get("enabled") != dst[key].get("enabled"):
            failures.append({"code": "trigger_definition_differs_from_source", "detail": f"{key[0]}.{key[1]}"})
    return failures


def check_orphans(orphans: dict[str, int]) -> list[Failure]:
    return [{"code": "orphan_reference", "detail": f"{name}: {count}"}
            for name, count in sorted(orphans.items()) if count]


def check_pointer_consistency(pointers: list[dict[str, Any]], heads: list[dict[str, Any]],
                              observations: list[dict[str, Any]]) -> list[Failure]:
    obs = {o["id"]: o for o in observations}
    heads_by = {scope_of(h): h for h in heads}
    failures: list[Failure] = []
    for pointer in pointers:
        scope = scope_of(pointer)
        label = "/".join(scope)
        observation = obs.get(pointer["observation_id"])
        if observation is None:
            failures.append({"code": "pointer_observation_not_authoritative", "detail": label})
            continue
        if observation["verification_status"] != "verified":
            failures.append({"code": "pointer_observation_not_verified", "detail": label})
        if not observation["run_exists"] or not observation["snapshot_exists"]:
            failures.append({"code": "pointer_observation_dangling", "detail": label})
        if pointer["resolved_commit_sha"] != observation["resolved_commit_sha"]:
            failures.append({"code": "pointer_commit_mismatch", "detail": label})
        if pointer["snapshot_id"] != observation["snapshot_id"] or pointer["run_id"] != observation["run_id"]:
            failures.append({"code": "pointer_link_mismatch", "detail": label})
        if scope != scope_of(observation):
            failures.append({"code": "pointer_scope_mismatch", "detail": label})
        head = heads_by.get(scope)
        if (head is None or head["observation_id"] != pointer["observation_id"]
                or head["head_commit_sha"] != pointer["resolved_commit_sha"]
                or head["generated_at"] != pointer["generated_at"]):
            failures.append({"code": "ref_head_mismatch", "detail": label})
    return failures


def rebuild_pointers(observations: list[dict[str, Any]]) -> dict[tuple[str, ...], dict[str, Any]]:
    """scope 별 최신 pointer = authoritative + verified 이고 run/snapshot 이 실재하는 가장 늦은 observation.

    정렬은 ingest 와 같은 단서(verified_at, created_at, id)다. 시각은 SQL 이 고정폭 UTC 문자열로 준다.
    """
    eligible = [o for o in observations
                if o["authoritative"] and o["verification_status"] == "verified"
                and o["run_exists"] and o["snapshot_exists"]]
    eligible.sort(key=lambda o: (o["verified_at"], o["created_at"], o["id"]))
    latest: dict[tuple[str, ...], dict[str, Any]] = {}
    for o in eligible:
        latest[scope_of(o)] = {
            "project": o["project"], "repository_id": o["repository_id"],
            "target_ref": o["target_ref"], "governance_scope": o["governance_scope"],
            "snapshot_id": o["snapshot_id"], "observation_id": o["id"], "run_id": o["run_id"],
            "resolved_commit_sha": o["resolved_commit_sha"],
            "expected_target_ref_head_sha": o["expected_target_ref_head_sha"],
            "verified_at": o["verified_at"],
        }
    return latest


def compare_pointers(rebuilt: dict[tuple[str, ...], dict[str, Any]],
                     original: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[Failure]]:
    current = {scope_of(p): p for p in original}
    rows, failures = [], []
    for scope in sorted(set(rebuilt) | set(current)):
        label = "/".join(scope)
        if scope not in rebuilt or scope not in current:
            code = "pointer_rebuild_extra_scope" if scope in rebuilt else "pointer_rebuild_missing_scope"
            failures.append({"code": code, "detail": label})
            rows.append({"scope": label, "match": False, "diff": [code]})
            continue
        diff = [f for f in POINTER_COMPARE_FIELDS if rebuilt[scope][f] != current[scope][f]]
        for f in diff:
            failures.append({"code": "pointer_rebuild_mismatch", "detail": f"{label}.{f}"})
        rows.append({"scope": label, "match": not diff, "diff": diff,
                     "snapshot_id": current[scope]["snapshot_id"], "run_id": current[scope]["run_id"]})
    return rows, failures


def classify_lock_probe(a_acquired: bool, b_while_held: bool, b_other_key: bool,
                        b_after_release: bool, advisory_granted: int) -> list[Failure]:
    failures: list[Failure] = []
    if not a_acquired:
        failures.append({"code": "lock_first_session_not_acquired", "detail": ""})
    if b_while_held:
        failures.append({"code": "lock_second_session_also_acquired", "detail": ""})
    if not b_other_key:
        failures.append({"code": "lock_other_scope_blocked", "detail": ""})
    if not b_after_release:
        failures.append({"code": "lock_not_released_on_commit", "detail": ""})
    if advisory_granted != 1:
        failures.append({"code": "lock_granted_count", "detail": str(advisory_granted)})
    return failures


def find_secret_leaks(chunks: Iterable[bytes], secrets: Sequence[str]) -> int:
    """청크 경계에 걸친 비밀값까지 찾아, 발견된 비밀값 '개수'만 돌려준다(값은 돌려주지 않는다)."""
    needles = [s.encode("utf-8") for s in secrets if s]
    if not needles:
        return 0
    keep = max(len(n) for n in needles) - 1
    found: set[bytes] = set()
    tail = b""
    for chunk in chunks:
        window = tail + chunk
        for needle in needles:
            if needle in window:
                found.add(needle)
        tail = window[-keep:] if keep else b""
    return len(found)


def iter_file_chunks(paths: Iterable[Path], size: int = 1 << 20) -> Iterable[bytes]:
    for path in paths:
        with path.open("rb") as handle:
            while True:
                data = handle.read(size)
                if not data:
                    break
                yield data
        yield b"\n"


def ignorable_restore_error(line: str) -> bool:
    return bool(re.search(r"schema \"public\" already exists|extension \"plpgsql\" already exists", line))


# ── 프로세스 I/O ──────────────────────────────────────────────────────────────────────
def run_cmd(argv: list[str], deadline: Deadline, *, stdout_path: Path | None = None,
            input_bytes: bytes | None = None, check: bool = True) -> subprocess.CompletedProcess:
    timeout = deadline.remaining()
    try:
        if stdout_path is not None:
            with stdout_path.open("wb") as handle:
                proc = subprocess.run(argv, stdout=handle, stderr=subprocess.PIPE, input=input_bytes,
                                      timeout=timeout)
        else:
            proc = subprocess.run(argv, capture_output=True, input=input_bytes, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise DrillTimeout(f"명령 시간 초과: {argv[:4]}") from exc
    if check and proc.returncode != 0:
        tail = (proc.stderr or b"").decode("utf-8", "replace").strip().splitlines()[-3:]
        raise DrillError(f"명령 실패 rc={proc.returncode}: {' '.join(argv[:5])} :: {' | '.join(tail)}")
    return proc


class PsqlSession:
    """오래 사는 psql 하나. 문장마다 센티널을 보내 응답 끝을 안다. 출력은 jsonb 한 줄 규약."""

    def __init__(self, argv: list[str], deadline: Deadline, *, read_only: bool = False) -> None:
        self.deadline = deadline
        self.read_only = read_only
        self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, bufsize=0)
        self._buf = b""

    def _send(self, sql: str) -> str:
        if self.read_only and not is_read_only_sql(sql):
            raise SessionError("읽기 전용 세션에 쓰기 SQL 이 들어왔다 — 거부")
        marker = f"__END_{os.urandom(6).hex()}__"
        assert self.proc.stdin is not None
        try:
            self.proc.stdin.write(f"{sql}\n\\echo {marker}\n".encode("utf-8"))
        except BrokenPipeError as exc:
            raise SessionError("psql 이 이미 종료됨") from exc
        return marker

    def _read_until(self, marker: str) -> str:
        needle = (marker + "\n").encode()
        assert self.proc.stdout is not None
        fd = self.proc.stdout.fileno()
        while needle not in self._buf:
            wait = min(self.deadline.remaining(), 5.0)
            ready, _, _ = select.select([fd], [], [], wait)
            if not ready:
                continue
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                text = self._buf.decode("utf-8", "replace").strip().splitlines()[-3:]
                raise SessionError("psql 이 응답 도중 종료됨: " + " | ".join(text))
            self._buf += chunk
        head, _, self._buf = self._buf.partition(needle)
        return head.decode("utf-8", "replace")

    def lines(self, sql: str) -> list[str]:
        out = self._read_until(self._send(sql))
        return [line for line in out.splitlines() if line.strip()]

    def text(self, sql: str) -> str:
        found = self.lines(sql)
        if not found:
            raise SessionError("빈 응답")
        return found[0].strip()

    def json(self, sql: str) -> Any:
        return json.loads(self.text(sql))

    def close(self) -> None:
        try:
            if self.proc.stdin and self.proc.poll() is None:
                self.proc.stdin.write(b"\\q\n")
                self.proc.stdin.close()
            self.proc.wait(timeout=10)
        except Exception:  # noqa: BLE001 - best-effort teardown
            self.proc.kill()
            self.proc.wait(timeout=10)


def psql_argv(container: str) -> list[str]:
    return ["docker", "exec", "-i", container, "psql", "-U", DB_USER, "-d", DB_NAME,
            "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1"]


# ── 상태 수집 (원본 스냅샷·복원본 공통) ─────────────────────────────────────────────────
def _ts(col: str) -> str:
    return f"to_char({col} AT TIME ZONE 'UTC','YYYY-MM-DD\"T\"HH24:MI:SS.US')"


def _scope_cols(alias: str) -> str:
    return ", ".join(f"'{k}', {alias}.{k}" for k in SCOPE_KEYS)


def _digest_expr(table: str) -> str:
    drop = "".join(f" - '{c}'" for c in SECRET_COLUMNS.get(table, ()))
    return f"(to_jsonb(t){drop})::text" if drop else "t::text"


SQL_ORPHANS = {
    "pointers_missing_snapshot": "SELECT count(*) FROM aag_latest_pointers p LEFT JOIN aag_graph_snapshots_v2 s ON s.id=p.snapshot_id WHERE s.id IS NULL",
    "pointers_missing_observation": "SELECT count(*) FROM aag_latest_pointers p LEFT JOIN aag_snapshot_observations o ON o.id=p.observation_id WHERE o.id IS NULL",
    "pointers_missing_run": "SELECT count(*) FROM aag_latest_pointers p LEFT JOIN aag_scan_runs r ON r.id=p.run_id WHERE r.id IS NULL",
    "pointers_snapshot_not_ready": "SELECT count(*) FROM aag_latest_pointers p JOIN aag_graph_snapshots_v2 s ON s.id=p.snapshot_id WHERE s.publish_status<>'ready'",
    "ref_heads_missing_observation": "SELECT count(*) FROM aag_ref_heads h LEFT JOIN aag_snapshot_observations o ON o.id=h.observation_id WHERE o.id IS NULL",
    "observations_missing_run": "SELECT count(*) FROM aag_snapshot_observations o LEFT JOIN aag_scan_runs r ON r.id=o.run_id WHERE r.id IS NULL",
    "observations_missing_snapshot": "SELECT count(*) FROM aag_snapshot_observations o LEFT JOIN aag_graph_snapshots_v2 s ON s.id=o.snapshot_id WHERE s.id IS NULL",
    "consumer_events_missing_snapshot": "SELECT count(*) FROM aag_api_consumer_events e LEFT JOIN aag_graph_snapshots_v2 s ON s.id=e.snapshot_id WHERE e.snapshot_id IS NOT NULL AND s.id IS NULL",
}


def collect_state(session: PsqlSession) -> dict[str, Any]:
    state: dict[str, Any] = {"tables": {}, "schema": {}, "orphans": {}}
    state["server_version"] = session.text("SHOW server_version;")
    for table in AAG_TABLES:
        state["tables"][table] = session.json(
            "SELECT jsonb_build_object('n', count(*), 'digest', "
            "md5(coalesce(string_agg(md5(x.r), '' ORDER BY md5(x.r)), ''))) "
            f"FROM (SELECT {_digest_expr(table)} AS r FROM public.{table} t) x;")
        state["schema"][table] = session.json(
            "SELECT jsonb_build_object("
            "'columns', (SELECT coalesce(jsonb_agg(c.column_name||':'||c.data_type||':'||c.is_nullable||':'||"
            "coalesce(c.column_default,'') ORDER BY c.ordinal_position),'[]') FROM information_schema.columns c "
            f"WHERE c.table_schema='public' AND c.table_name='{table}'), "
            "'constraints', (SELECT coalesce(jsonb_agg(conname||' '||pg_get_constraintdef(oid) ORDER BY conname),'[]') "
            f"FROM pg_constraint WHERE conrelid='public.{table}'::regclass), "
            "'indexes', (SELECT coalesce(jsonb_agg(indexdef ORDER BY indexname),'[]') FROM pg_indexes "
            f"WHERE schemaname='public' AND tablename='{table}'));")
    state["triggers"] = session.json(
        "SELECT coalesce(jsonb_agg(jsonb_build_object('table', c.relname, 'name', t.tgname, "
        "'enabled', t.tgenabled::text, 'before', (t.tgtype & 2)>0, 'ins', (t.tgtype & 4)>0, "
        "'del', (t.tgtype & 8)>0, 'upd', (t.tgtype & 16)>0) ORDER BY c.relname, t.tgname),'[]') "
        "FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid "
        "WHERE NOT t.tgisinternal AND c.relname LIKE 'aag\\_%';")
    state["pointers"] = session.json(
        "SELECT coalesce(jsonb_agg(jsonb_build_object(" + _scope_cols("p") + ", "
        "'snapshot_id', p.snapshot_id::text, 'observation_id', p.observation_id::text, "
        "'run_id', p.run_id::text, 'resolved_commit_sha', p.resolved_commit_sha, "
        "'expected_target_ref_head_sha', p.expected_target_ref_head_sha, "
        f"'generated_at', {_ts('p.generated_at')}, 'verified_at', {_ts('p.verified_at')}) "
        "ORDER BY p.project, p.repository_id, p.target_ref, p.governance_scope),'[]') FROM aag_latest_pointers p;")
    state["ref_heads"] = session.json(
        "SELECT coalesce(jsonb_agg(jsonb_build_object(" + _scope_cols("h") + ", "
        "'head_commit_sha', h.head_commit_sha, 'observation_id', h.observation_id::text, "
        f"'generated_at', {_ts('h.generated_at')}) "
        "ORDER BY h.project, h.repository_id, h.target_ref, h.governance_scope),'[]') FROM aag_ref_heads h;")
    state["observations"] = session.json(
        "SELECT coalesce(jsonb_agg(jsonb_build_object('id', o.id::text, 'run_id', o.run_id::text, "
        "'snapshot_id', o.snapshot_id::text, " + _scope_cols("o") + ", "
        "'resolved_commit_sha', o.resolved_commit_sha, "
        "'expected_target_ref_head_sha', o.expected_target_ref_head_sha, "
        "'authoritative', o.authoritative, 'verification_status', o.verification_status, "
        f"'verified_at', {_ts('o.verified_at')}, 'created_at', {_ts('o.created_at')}, "
        "'run_exists', r.id IS NOT NULL, 'snapshot_exists', s.id IS NOT NULL)),'[]') "
        "FROM aag_snapshot_observations o LEFT JOIN aag_scan_runs r ON r.id=o.run_id "
        "LEFT JOIN aag_graph_snapshots_v2 s ON s.id=o.snapshot_id WHERE o.authoritative;")
    for name, sql in SQL_ORPHANS.items():
        state["orphans"][name] = int(session.text(sql + ";"))
    return state


def masked_credentials_json(session: PsqlSession) -> str:
    return session.text(
        "SELECT coalesce(jsonb_agg((to_jsonb(c) || jsonb_build_object('token_hash', "
        f"{mask_token_sql()})) ORDER BY c.id),'[]') FROM aag_scanner_credentials c;")


# ── 복원 환경 ────────────────────────────────────────────────────────────────────────
def start_scratch(name: str, image: str, deadline: Deadline, job_label: str) -> None:
    seconds = max(60, int(deadline.remaining()))
    argv = ["docker", "run", "-d", "--rm", "--init", "--name", name, "--network", "none",
            "--label", "aag.restore_drill=1", "--memory", "2g", "--cpus", "2", "--pids-limit", "512",
            "--tmpfs", "/var/lib/postgresql/data:rw,size=3g,mode=0700",
            "-e", f"POSTGRES_USER={DB_USER}", "-e", f"POSTGRES_DB={DB_NAME}",
            "-e", "POSTGRES_HOST_AUTH_METHOD=trust"]
    if job_label:
        argv += ["--label", f"aads.runner_job={job_label}"]
    argv += ["--entrypoint", "sh", image, "-c", f"exec timeout {seconds} docker-entrypoint.sh postgres"]
    run_cmd(argv, deadline)
    for _ in range(120):
        probe = run_cmd(["docker", "exec", name, "pg_isready", "-h", "127.0.0.1", "-U", DB_USER],
                        deadline, check=False)
        if probe.returncode == 0:
            return
        time.sleep(1)
    raise DrillError("일회용 PostgreSQL 이 120초 안에 준비되지 않음")


def restore_into(name: str, schema_dump: Path, data_dump: Path, masked_json: str,
                 deadline: Deadline) -> dict[str, Any]:
    info: dict[str, Any] = {}
    for dump in (schema_dump, data_dump):
        run_cmd(["docker", "cp", str(dump), f"{name}:/tmp/{dump.name}"], deadline)
    schema = run_cmd(["docker", "exec", name, "pg_restore", "-U", DB_USER, "-d", DB_NAME,
                      "--no-owner", "--no-privileges", f"/tmp/{schema_dump.name}"], deadline, check=False)
    errors = [ln for ln in schema.stderr.decode("utf-8", "replace").splitlines()
              if ln.startswith("pg_restore: error")]
    info["schema_restore_rc"] = schema.returncode
    info["schema_restore_errors"] = [ln for ln in errors if not ignorable_restore_error(ln)]
    info["schema_restore_ignored"] = len(errors) - len(info["schema_restore_errors"])
    data = run_cmd(["docker", "exec", name, "pg_restore", "-U", DB_USER, "-d", DB_NAME, "--data-only",
                    "--single-transaction", "--exit-on-error", f"/tmp/{data_dump.name}"],
                   deadline, check=False)
    info["data_restore_rc"] = data.returncode
    info["data_restore_stderr_tail"] = data.stderr.decode("utf-8", "replace").strip().splitlines()[-3:]
    if data.returncode == 0:
        tag = "m" + hashlib.sha256(masked_json.encode()).hexdigest()[:12]
        load = ("INSERT INTO aag_scanner_credentials SELECT * FROM jsonb_populate_recordset("
                f"NULL::aag_scanner_credentials, ${tag}${masked_json}${tag}$::jsonb);\n")
        loaded = run_cmd(["docker", "exec", "-i", name, "psql", "-U", DB_USER, "-d", DB_NAME, "-X", "-q",
                          "-v", "ON_ERROR_STOP=1", "-f", "-"], deadline, input_bytes=load.encode(),
                         check=False)
        info["credentials_load_rc"] = loaded.returncode
    return info


def probe_blocked(name: str, sql: str, deadline: Deadline) -> dict[str, Any]:
    proc = run_cmd(["docker", "exec", name, "psql", "-U", DB_USER, "-d", DB_NAME, "-X", "-q",
                    "-v", "ON_ERROR_STOP=1", "-c", f"BEGIN; {sql}; ROLLBACK;"], deadline, check=False)
    message = [ln for ln in proc.stderr.decode("utf-8", "replace").splitlines() if "ERROR" in ln]
    return {"blocked": proc.returncode != 0 and bool(message), "message": message[0] if message else ""}


def run_lock_probe(name: str, deadline: Deadline) -> tuple[dict[str, Any], list[Failure]]:
    a = PsqlSession(psql_argv(name), deadline)
    b = PsqlSession(psql_argv(name), deadline)
    try:
        key = int(a.text(
            "SELECT hashtextextended(project||chr(31)||repository_id||chr(31)||target_ref||chr(31)||"
            "governance_scope, 0) FROM aag_latest_pointers ORDER BY project, repository_id LIMIT 1;"))
        a.lines("BEGIN;")
        a.lines(f"SELECT pg_advisory_xact_lock({key});")
        b.lines("BEGIN;")
        held = b.text(f"SELECT pg_try_advisory_xact_lock({key});") == "t"
        other = b.text(f"SELECT pg_try_advisory_xact_lock({key + 1});") == "t"
        granted = int(b.text(
            "SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND granted "
            f"AND classid::bigint = (({key}::bigint >> 32) & 4294967295) "
            f"AND objid::bigint = ({key}::bigint & 4294967295);"))
        a.lines("COMMIT;")
        after = b.text(f"SELECT pg_try_advisory_xact_lock({key});") == "t"
        b.lines("COMMIT;")
    finally:
        a.close()
        b.close()
    info = {"key_source": "aag_latest_pointers scope_key (ingest 와 같은 hashtextextended)",
            "second_session_while_held": held, "other_scope_while_held": other,
            "second_session_after_commit": after, "advisory_granted_on_key": granted}
    return info, classify_lock_probe(True, held, other, after, granted)


# ── 드릴 본체 ────────────────────────────────────────────────────────────────────────
def run_drill(*, timeout: int, workdir_parent: str | None = None) -> dict[str, Any]:
    deadline = Deadline(timeout)
    started = time.monotonic()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    name = f"{DRILL_NAME_PREFIX}-{stamp}"
    workdir = Path(tempfile.mkdtemp(prefix=f"{DRILL_NAME_PREFIX}-", dir=workdir_parent))
    workdir.chmod(0o700)
    repo_root = str(Path(__file__).resolve().parents[1])
    result: dict[str, Any] = {"container": name, "started_at": stamp, "phases": {}, "failures": []}
    failures: list[Failure] = result["failures"]
    schema_dump, data_dump = workdir / "schema.dump", workdir / "data.dump"

    def phase(label: str, begin: float) -> None:
        result["phases"][label] = round(time.monotonic() - begin, 1)

    try:
        image = run_cmd(["docker", "inspect", SOURCE_CONTAINER, "--format", "{{.Config.Image}}"],
                        deadline).stdout.decode().strip()
        result["image"] = image

        t = time.monotonic()
        src = PsqlSession(psql_argv(SOURCE_CONTAINER), deadline, read_only=True)
        try:
            src.lines("SET TIME ZONE 'UTC';")
            src.lines("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;")
            snapshot_id = src.text("SELECT pg_export_snapshot();")
            result["snapshot"] = snapshot_id
            source_state = collect_state(src)
            masked = masked_credentials_json(src)
            secrets_in_memory = src.json("SELECT coalesce(jsonb_agg(btrim(token_hash)),'[]') "
                                         "FROM aag_scanner_credentials;")
            phase("원본 스냅샷 수집", t)

            t = time.monotonic()
            common = ["docker", "exec", SOURCE_CONTAINER, "pg_dump", "-U", DB_USER, "-d", DB_NAME,
                      "-Fc", "-Z", "0", "--no-owner", "--no-privileges", f"--snapshot={snapshot_id}"]
            run_cmd(common + ["--schema-only"], deadline, stdout_path=schema_dump)
            run_cmd(common + ["--data-only", "-t", "public.aag_*",
                              "--exclude-table-data=aag_scanner_credentials"], deadline,
                    stdout_path=data_dump)
            result["dump_bytes"] = {"schema": schema_dump.stat().st_size, "data": data_dump.stat().st_size}
            phase("pg_dump", t)
        finally:
            if src.proc.poll() is None:
                src.lines("ROLLBACK;")
            src.close()

        t = time.monotonic()
        start_scratch(name, image, deadline, runner_job_label(dict(os.environ), repo_root))
        phase("일회용 컨테이너 기동", t)

        t = time.monotonic()
        restore_info = restore_into(name, schema_dump, data_dump, masked, deadline)
        result["restore"] = restore_info
        for line in restore_info["schema_restore_errors"]:
            failures.append({"code": "schema_restore_error", "detail": line})
        if restore_info["data_restore_rc"] != 0:
            failures.append({"code": "data_restore_failed", "detail": " | ".join(restore_info["data_restore_stderr_tail"])})
        if restore_info.get("credentials_load_rc", 1) != 0:
            failures.append({"code": "credentials_load_failed", "detail": ""})
        phase("pg_restore", t)

        if not any(f["code"] in ("data_restore_failed", "credentials_load_failed") for f in failures):
            t = time.monotonic()
            dst = PsqlSession(psql_argv(name), deadline)
            try:
                dst.lines("SET TIME ZONE 'UTC';")
                restored_state = collect_state(dst)
            finally:
                dst.close()
            result["server_versions"] = {"source": source_state["server_version"],
                                         "restored": restored_state["server_version"]}
            rows, count_failures = compare_counts(source_state["tables"], restored_state["tables"])
            result["table_rows"] = rows
            failures += count_failures
            failures += compare_schema(source_state["schema"], restored_state["schema"])
            failures += check_triggers(source_state["triggers"], restored_state["triggers"])
            result["triggers"] = restored_state["triggers"]
            result["orphans_source"], result["orphans_restored"] = source_state["orphans"], restored_state["orphans"]
            failures += check_orphans(restored_state["orphans"])
            result["pointers_source"] = source_state["pointers"]
            failures += check_pointer_consistency(restored_state["pointers"], restored_state["ref_heads"],
                                                  restored_state["observations"])
            rebuilt = rebuild_pointers(restored_state["observations"])
            result["authoritative_observations"] = len(restored_state["observations"])
            pointer_rows, pointer_failures = compare_pointers(rebuilt, source_state["pointers"])
            result["pointer_rows"] = pointer_rows
            failures += pointer_failures
            phase("복원본 검증", t)

            t = time.monotonic()
            probes = {
                "audit_update": "UPDATE aag_audit_events SET reason=reason WHERE id=(SELECT id FROM aag_audit_events LIMIT 1)",
                "audit_delete": "DELETE FROM aag_audit_events WHERE id=(SELECT id FROM aag_audit_events LIMIT 1)",
                "ready_snapshot_update": "UPDATE aag_graph_snapshots_v2 SET stats=stats WHERE id=(SELECT id FROM aag_graph_snapshots_v2 WHERE publish_status='ready' LIMIT 1)",
                "ready_snapshot_delete": "DELETE FROM aag_graph_snapshots_v2 WHERE id=(SELECT id FROM aag_graph_snapshots_v2 WHERE publish_status='ready' LIMIT 1)",
            }
            result["probes"] = {k: probe_blocked(name, sql, deadline) for k, sql in probes.items()}
            for key, outcome in result["probes"].items():
                if not outcome["blocked"]:
                    failures.append({"code": "mutation_not_blocked", "detail": key})
            result["lock"], lock_failures = run_lock_probe(name, deadline)
            failures += lock_failures
            phase("불변성·advisory lock", t)

            restored_tokens = restored_state_tokens(name, deadline)
            leaked_in_db = len(set(restored_tokens) & set(secrets_in_memory))
        else:
            leaked_in_db = 0
        leaks = find_secret_leaks(iter_file_chunks([schema_dump, data_dump]), secrets_in_memory)
        control = [p["snapshot_id"] for p in source_state["pointers"][:1]]
        control_found = find_secret_leaks(iter_file_chunks([schema_dump, data_dump]), control)
        result["secrets"] = {"count": len(secrets_in_memory), "leaked_in_dump_files": leaks,
                             "leaked_in_restored_db": leaked_in_db,
                             "scan_control_expected": len(control), "scan_control_found": control_found}
        if leaks or leaked_in_db:
            failures.append({"code": "secret_exposed", "detail": f"dump={leaks} db={leaked_in_db}"})
        if control_found != len(control):
            failures.append({"code": "secret_scan_blind", "detail": "양성 대조값을 덤프에서 찾지 못함 — 스캔이 덤프를 읽지 못한다"})
        del secrets_in_memory
    except Exception as exc:  # noqa: BLE001 - 어떤 실패든 보고서와 정리까지는 도달해야 한다
        failures.append({"code": "drill_aborted", "detail": f"{type(exc).__name__}: {exc}"})
    finally:
        cleanup = cleanup_resources(name, workdir)
        result["cleanup"] = cleanup
        if cleanup["remaining_containers"] or cleanup["workdir_exists"]:
            failures.append({"code": "cleanup_incomplete", "detail": json.dumps(cleanup)})
        result["elapsed_seconds"] = round(time.monotonic() - started, 1)
    result["ok"] = not failures
    return result


def restored_state_tokens(name: str, deadline: Deadline) -> list[str]:
    session = PsqlSession(psql_argv(name), deadline)
    try:
        return session.json("SELECT coalesce(jsonb_agg(btrim(token_hash)),'[]') FROM aag_scanner_credentials;")
    finally:
        session.close()


def cleanup_resources(name: str, workdir: Path) -> dict[str, Any]:
    forever = Deadline(120)
    run_cmd(["docker", "rm", "-f", "-v", name], forever, check=False)
    shutil.rmtree(workdir, ignore_errors=True)
    listing = run_cmd(["docker", "ps", "-a", "--filter", f"name={DRILL_NAME_PREFIX}", "--format", "{{.Names}}"],
                      forever, check=False).stdout.decode().split()
    return {"remaining_containers": len(listing), "workdir_exists": workdir.exists()}


# ── 보고서 ───────────────────────────────────────────────────────────────────────────
def _mark(ok: bool) -> str:
    return "일치" if ok else "**불일치**"


def render_report(result: dict[str, Any], *, document_key: str = "go100-aag-prod-db-restore-drill") -> str:
    lines = [
        "# GO100 AAG 운영 DB 복구 검증", "",
        f"document_key: `{document_key}`", "",
        f"- 실행 시각(UTC): {result.get('started_at')}",
        f"- 결과: **{'통과' if result.get('ok') else '실패'}**",
        f"- 총 소요: {result.get('elapsed_seconds')}초 (상한 {DEFAULT_TIMEOUT_SECONDS}초)",
        f"- PostgreSQL 버전: 원본 {result.get('server_versions', {}).get('source')} / "
        f"복원 {result.get('server_versions', {}).get('restored')}",
        f"- 원본 일관성: REPEATABLE READ READ ONLY 스냅샷 `{result.get('snapshot')}` 에서 카운트·다이제스트·덤프를 모두 추출",
        f"- 일회용 컨테이너: `{result.get('container')}` (이미지 `{result.get('image')}`, `--network none`, tmpfs)", "",
        "## 단계별 소요", "", "| 단계 | 초 |", "|---|---|",
    ]
    lines += [f"| {k} | {v} |" for k, v in result.get("phases", {}).items()]
    lines += ["", "## 테이블별 row count / 내용 다이제스트", "",
              "| 테이블 | 원본 | 복원 | row count | 내용 md5 |", "|---|---:|---:|---|---|"]
    lines += [f"| {r['table']} | {r['source']} | {r['restored']} | {_mark(r['count_ok'])} | {_mark(r['digest_ok'])} |"
              for r in result.get("table_rows", [])]
    lines += ["", "내용 md5 는 행 전체 텍스트의 정렬 해시다. aag_scanner_credentials 는 token_hash 를 뺀 나머지 컬럼만 비교한다.", "",
              "## latest pointer 재구성", "",
              f"authoritative 이고 verified 인 observation {result.get('authoritative_observations')}건에서 scope 별 "
              "최신(verified_at, created_at, id)을 다시 뽑아 원본 pointer 와 대조했다.", "",
              "| scope | 결과 | 차이 |", "|---|---|---|"]
    lines += [f"| {r['scope']} | {_mark(r['match'])} | {', '.join(r['diff']) or '-'} |"
              for r in result.get("pointer_rows", [])]
    lines += ["", "## 참조 무결성 (복원본 고아 행 수)", "", "| 검사 | 원본 | 복원 |", "|---|---:|---:|"]
    lines += [f"| {k} | {result.get('orphans_source', {}).get(k)} | {v} |"
              for k, v in result.get("orphans_restored", {}).items()]
    lines += ["", "## 불변 트리거", "", "| 테이블 | 트리거 | 활성 | 이벤트 |", "|---|---|---|---|"]
    lines += [f"| {t['table']} | {t['name']} | {t['enabled']} | {', '.join(sorted(trigger_events(t)))} |"
              for t in result.get("triggers", [])]
    lines += ["", "복원본에서 실제로 변경을 시도하고 롤백한 결과:", "", "| 시도 | 차단 | 메시지 |", "|---|---|---|"]
    lines += [f"| {k} | {'차단됨' if v['blocked'] else '**차단 안 됨**'} | {v['message']} |"
              for k, v in result.get("probes", {}).items()]
    lock = result.get("lock", {})
    lines += ["", "## advisory lock 경합", "",
              f"- 키 출처: {lock.get('key_source')}",
              f"- 세션 A 획득 후 세션 B 같은 키 시도: {'획득(문제)' if lock.get('second_session_while_held') else '실패(정상, 1개만 획득)'}",
              f"- 세션 B 다른 키 시도: {'획득(정상)' if lock.get('other_scope_while_held') else '차단(문제)'}",
              f"- 세션 A COMMIT 후 세션 B 같은 키: {'획득(정상)' if lock.get('second_session_after_commit') else '실패(문제)'}",
              f"- 경합 중 granted advisory lock 수: {lock.get('advisory_granted_on_key')}"]
    secrets = result.get("secrets", {})
    lines += ["", "## 비밀값 비노출", "",
              f"- 마스킹 대상 token_hash 수: {secrets.get('count')} (원본 값은 이 문서·로그·덤프에 쓰지 않음)",
              "- 덤프는 `--exclude-table-data=aag_scanner_credentials` 로 받고, 복원본에는 id 기반 sha256 가짜 값을 적재",
              f"- 비압축 덤프 파일에서 원본 값 발견: {secrets.get('leaked_in_dump_files')}건",
              f"- 복원 DB 에서 원본 값 발견: {secrets.get('leaked_in_restored_db')}건",
              f"- 스캔 양성 대조(덤프에 반드시 있는 pointer snapshot id): {secrets.get('scan_control_found')}/"
              f"{secrets.get('scan_control_expected')} 발견 — 스캔이 실제로 덤프 내용을 읽는다는 증거"]
    restore = result.get("restore", {})
    lines += ["", "## pg_restore", "",
              f"- schema 복원 rc={restore.get('schema_restore_rc')}, 무시한 기본 객체 오류 {restore.get('schema_restore_ignored')}건, "
              f"실제 오류 {len(restore.get('schema_restore_errors', []))}건",
              f"- data 복원 rc={restore.get('data_restore_rc')} (`--single-transaction --exit-on-error`)",
              f"- 덤프 크기(byte): {result.get('dump_bytes')}"]
    lines += ["", "## 실패 항목", ""]
    lines += [f"- `{f['code']}` {f['detail']}" for f in result.get("failures", [])] or ["- 없음"]
    cleanup = result.get("cleanup", {})
    lines += ["", "## 일회용 자원 정리", "",
              f"- `docker ps -a | grep {DRILL_NAME_PREFIX}` 결과: {cleanup.get('remaining_containers')}건",
              f"- 덤프 작업 디렉터리 잔존: {'있음' if cleanup.get('workdir_exists') else '없음'}", ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--report", default=None, help="마크다운 보고서 경로")
    parser.add_argument("--json", default=None, help="원시 결과 JSON 경로")
    args = parser.parse_args(argv)
    result = run_drill(timeout=args.timeout)
    text = render_report(result)
    if args.report:
        Path(args.report).write_text(text, encoding="utf-8")
    if args.json:
        Path(args.json).write_text(json.dumps(result, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(f"ok={result['ok']} failures={len(result['failures'])} elapsed={result['elapsed_seconds']}s")
    for failure in result["failures"]:
        print(f"  {failure['code']}: {failure['detail']}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
