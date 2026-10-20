#!/usr/bin/env python3
"""AAG 백업 → 격리 복원 → latest-pointer 재구성 → artifact 재처리 검증기.

이 도구는 DSN 을 받지 않는다. 복원 대상은 **새로 만드는 sqlite 파일**뿐이라 운영 DB 를
건드릴 방법 자체가 없다 (라이브 복구 금지를 코드 구조로 보장한다). 증명하는 것:

* 백업 번들(JSONL + manifest sha256 + audit hash chain)이 실제 파일로 직렬화되고,
* 그 파일에서 빈 DB 로 복원되며(FK·CHECK·불변 트리거 적용),
* snapshot / ref / commit / stable key / fingerprint / audit 가 서로 일치하는지 재계산으로 검증하고,
* 포인터를 observation 만으로 재구성해 원본과 대조하며,
* 원본 artifact 를 다시 적재해도 같은 snapshot 으로 수렴하고,
* 손상된 번들은 각각 다른 코드로 거부된다.

증명하지 못하는 것 (보고서에 그대로 적는다): PostgreSQL 자체(pg_dump/pg_restore, 트랜잭션
격리, advisory lock), 운영 데이터 규모, 운영 복원 절차. sqlite 스키마는 마이그레이션
컬럼의 부분집합이며 ingest 규칙은 app/services/aag_ingest_v2.py 의 결정 로직을 옮긴 거울이다.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.aag.v2_contract import (  # noqa: E402
    CANONICALIZATION_VERSION,
    STABLE_KEY_VERSION,
    canonical_json,
    content_fingerprint,
    finding_key_set_digest,
    graph_content,
    input_fingerprint,
    normalize_findings,
    stable_finding_key,
)

BUNDLE_SCHEMA = "aag-backup-bundle-v1"
GENESIS = "0" * 64
UTC = timezone.utc

TABLES: dict[str, tuple[str, ...]] = {
    "aag_scan_runs": ("id",),
    "aag_graph_snapshots_v2": ("id",),
    "aag_snapshot_observations": ("id",),
    "aag_ref_heads": ("project", "repository_id", "target_ref", "governance_scope"),
    "aag_latest_pointers": ("project", "repository_id", "target_ref", "governance_scope"),
    "aag_audit_events": ("occurred_at", "id"),
}
JSON_COLUMNS = {
    "aag_graph_snapshots_v2": ("stats", "nodes", "edges", "findings", "unresolved"),
    "aag_audit_events": ("before_state", "after_state"),
}

SCHEMA_SQL = """
CREATE TABLE aag_scan_runs (
    id TEXT PRIMARY KEY, project TEXT NOT NULL, repository_id TEXT NOT NULL,
    target_ref TEXT NOT NULL, governance_scope TEXT NOT NULL DEFAULT 'default',
    resolved_commit_sha TEXT NOT NULL, expected_target_ref_head_sha TEXT NOT NULL,
    input_fingerprint TEXT NOT NULL, scanner_version TEXT NOT NULL,
    ruleset_digest TEXT NOT NULL, scan_scope_digest TEXT NOT NULL,
    normalization_version TEXT NOT NULL, host TEXT NOT NULL,
    result TEXT NOT NULL DEFAULT 'running' CHECK (result IN ('running','succeeded',
        'no_change_success','scan_failed','ingest_failed','skipped_locked','cancelled',
        'out_of_order','source_behind')),
    error_code TEXT, started_at TEXT NOT NULL, finished_at TEXT
);
CREATE TABLE aag_graph_snapshots_v2 (
    id TEXT PRIMARY KEY, project TEXT NOT NULL, repository_id TEXT NOT NULL,
    content_fingerprint TEXT NOT NULL, canonicalization_version TEXT NOT NULL,
    stable_key_version TEXT NOT NULL, stats TEXT NOT NULL, nodes TEXT NOT NULL,
    edges TEXT NOT NULL, findings TEXT NOT NULL, unresolved TEXT NOT NULL,
    node_count INTEGER NOT NULL CHECK (node_count >= 0),
    edge_count INTEGER NOT NULL CHECK (edge_count >= 0),
    finding_count INTEGER NOT NULL CHECK (finding_count >= 0),
    generated_at TEXT NOT NULL,
    publish_status TEXT NOT NULL DEFAULT 'candidate'
        CHECK (publish_status IN ('candidate','ready','rejected')),
    first_published_at TEXT,
    UNIQUE (project, repository_id, content_fingerprint),
    CHECK ((publish_status = 'ready') = (first_published_at IS NOT NULL))
);
CREATE TABLE aag_snapshot_observations (
    id TEXT PRIMARY KEY, run_id TEXT NOT NULL UNIQUE REFERENCES aag_scan_runs(id),
    snapshot_id TEXT NOT NULL REFERENCES aag_graph_snapshots_v2(id),
    project TEXT NOT NULL, repository_id TEXT NOT NULL, target_ref TEXT NOT NULL,
    governance_scope TEXT NOT NULL DEFAULT 'default', resolved_commit_sha TEXT NOT NULL,
    expected_target_ref_head_sha TEXT NOT NULL, input_fingerprint TEXT NOT NULL,
    content_fingerprint TEXT NOT NULL, authoritative INTEGER NOT NULL DEFAULT 0,
    result TEXT NOT NULL CHECK (result IN ('succeeded','no_change_success')),
    verification_status TEXT NOT NULL
        CHECK (verification_status IN ('verified','commit_mismatch','unknown')),
    verified_at TEXT NOT NULL, created_at TEXT NOT NULL,
    CHECK (NOT authoritative OR verification_status = 'verified')
);
CREATE TABLE aag_ref_heads (
    project TEXT NOT NULL, repository_id TEXT NOT NULL, target_ref TEXT NOT NULL,
    governance_scope TEXT NOT NULL DEFAULT 'default', head_commit_sha TEXT NOT NULL,
    generated_at TEXT NOT NULL, verified_at TEXT NOT NULL,
    observation_id TEXT NOT NULL REFERENCES aag_snapshot_observations(id),
    PRIMARY KEY (project, repository_id, target_ref, governance_scope)
);
CREATE TABLE aag_latest_pointers (
    project TEXT NOT NULL, repository_id TEXT NOT NULL, target_ref TEXT NOT NULL,
    governance_scope TEXT NOT NULL DEFAULT 'default',
    snapshot_id TEXT NOT NULL REFERENCES aag_graph_snapshots_v2(id),
    observation_id TEXT NOT NULL REFERENCES aag_snapshot_observations(id),
    run_id TEXT NOT NULL REFERENCES aag_scan_runs(id),
    resolved_commit_sha TEXT NOT NULL, expected_target_ref_head_sha TEXT NOT NULL,
    generated_at TEXT NOT NULL, verified_at TEXT NOT NULL,
    PRIMARY KEY (project, repository_id, target_ref, governance_scope)
);
CREATE TABLE aag_audit_events (
    id TEXT PRIMARY KEY, project TEXT NOT NULL, actor_id TEXT NOT NULL,
    action TEXT NOT NULL, target_type TEXT NOT NULL, target_id TEXT NOT NULL,
    before_state TEXT, after_state TEXT, reason TEXT NOT NULL,
    correlation_id TEXT NOT NULL, source TEXT NOT NULL, occurred_at TEXT NOT NULL
);
CREATE TRIGGER trg_aag_audit_no_update BEFORE UPDATE ON aag_audit_events
BEGIN SELECT RAISE(ABORT, 'AAG audit events are append-only'); END;
CREATE TRIGGER trg_aag_audit_no_delete BEFORE DELETE ON aag_audit_events
BEGIN SELECT RAISE(ABORT, 'AAG audit events are append-only'); END;
CREATE TRIGGER trg_aag_snapshot_immutable BEFORE UPDATE ON aag_graph_snapshots_v2
WHEN OLD.publish_status = 'ready'
BEGIN SELECT RAISE(ABORT, 'ready AAG snapshots are immutable'); END;
"""
EXPECTED_TRIGGERS = {"trg_aag_audit_no_update", "trg_aag_audit_no_delete", "trg_aag_snapshot_immutable"}


class BundleError(Exception):
    """The backup bundle is corrupt or unusable. `code` is the stable rejection reason."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


# ── helpers ──────────────────────────────────────────────────────────────────
def ts(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)


def _line(row: dict[str, Any]) -> str:
    return json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def audit_chain(rows: list[dict[str, Any]]) -> str:
    head = GENESIS
    for row in rows:
        head = _sha256((head + canonical_json(row)).encode("utf-8"))
    return head


# ── ingest mirror (app/services/aag_ingest_v2.py 결정 로직) ──────────────────────
def ingest_artifact(
    conn: sqlite3.Connection, *, graph: dict[str, Any], project: str, repository_id: str,
    target_ref: str, resolved_commit_sha: str, expected_head_sha: str, generated_at: datetime,
    now: datetime, run_id: str, governance_scope: str = "default",
    scanner_version: str = "aag-v1.1", ruleset_digest: str = "b" * 64,
    scan_scope_digest: str = "c" * 64, host: str = "fixture",
) -> dict[str, Any]:
    project = project.strip().upper()
    body = graph_content(graph, project=project)
    fingerprint = content_fingerprint(graph, project=project)
    input_hash = input_fingerprint(
        project=project, repository_id=repository_id, target_ref=target_ref,
        governance_scope=governance_scope, resolved_commit_sha=resolved_commit_sha,
        expected_target_ref_head_sha=expected_head_sha, scanner_version=scanner_version,
        ruleset_digest=ruleset_digest, scan_scope_digest=scan_scope_digest, parser_versions={},
    )
    scope = (project, repository_id, target_ref, governance_scope)
    conn.execute("BEGIN")
    try:
        if conn.execute("SELECT 1 FROM aag_scan_runs WHERE id=?", (run_id,)).fetchone():
            existing = conn.execute(
                "SELECT input_fingerprint, result FROM aag_scan_runs WHERE id=?", (run_id,)).fetchone()
            if existing["input_fingerprint"] != input_hash:
                raise ValueError("run_id already exists with different input")
            conn.execute("COMMIT")
            return {"run_id": run_id, "result": existing["result"], "replayed": True}
        conn.execute(
            "INSERT INTO aag_scan_runs (id, project, repository_id, target_ref, governance_scope,"
            " resolved_commit_sha, expected_target_ref_head_sha, input_fingerprint, scanner_version,"
            " ruleset_digest, scan_scope_digest, normalization_version, host, result, started_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'running',?)",
            (run_id, *scope, resolved_commit_sha, expected_head_sha, input_hash, scanner_version,
             ruleset_digest, scan_scope_digest, CANONICALIZATION_VERSION, host, ts(now)))
        previous = conn.execute(
            "SELECT generated_at, resolved_commit_sha FROM aag_latest_pointers"
            " WHERE project=? AND repository_id=? AND target_ref=? AND governance_scope=?", scope).fetchone()
        commit_matches = resolved_commit_sha == expected_head_sha
        ordered = (
            previous is None
            or ts(generated_at) > previous["generated_at"]
            or (ts(generated_at) == previous["generated_at"]
                and resolved_commit_sha == previous["resolved_commit_sha"])
        )
        authoritative = commit_matches and ordered
        if not commit_matches:
            result, verification = "source_behind", "commit_mismatch"
        elif not ordered:
            result, verification = "out_of_order", "unknown"
        else:
            result, verification = "succeeded", "verified"
        snap = conn.execute(
            "SELECT id FROM aag_graph_snapshots_v2 WHERE project=? AND repository_id=?"
            " AND content_fingerprint=?", (project, repository_id, fingerprint)).fetchone()
        created = snap is None
        if created:
            snapshot_id = f"snap-{fingerprint[:12]}"
            conn.execute(
                "INSERT INTO aag_graph_snapshots_v2 (id, project, repository_id, content_fingerprint,"
                " canonicalization_version, stable_key_version, stats, nodes, edges, findings,"
                " unresolved, node_count, edge_count, finding_count, generated_at, publish_status,"
                " first_published_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'ready',?)",
                (snapshot_id, project, repository_id, fingerprint, CANONICALIZATION_VERSION,
                 STABLE_KEY_VERSION, *(json.dumps(body[k], sort_keys=True) for k in
                                       ("stats", "nodes", "edges", "findings", "unresolved")),
                 len(body["nodes"]), len(body["edges"]), len(body["findings"]),
                 ts(generated_at), ts(now)))
        else:
            snapshot_id = snap["id"]
        observation_result = "succeeded" if created else "no_change_success"
        if authoritative:
            result = observation_result
        observation_id = f"obs-{run_id}"
        conn.execute(
            "INSERT INTO aag_snapshot_observations (id, run_id, snapshot_id, project, repository_id,"
            " target_ref, governance_scope, resolved_commit_sha, expected_target_ref_head_sha,"
            " input_fingerprint, content_fingerprint, authoritative, result, verification_status,"
            " verified_at, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (observation_id, run_id, snapshot_id, *scope, resolved_commit_sha, expected_head_sha,
             input_hash, fingerprint, int(authoritative), observation_result, verification,
             ts(now), ts(now)))
        if authoritative:
            conn.execute(
                "INSERT INTO aag_ref_heads VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(project, repository_id,"
                " target_ref, governance_scope) DO UPDATE SET head_commit_sha=excluded.head_commit_sha,"
                " generated_at=excluded.generated_at, verified_at=excluded.verified_at,"
                " observation_id=excluded.observation_id",
                (*scope, resolved_commit_sha, ts(generated_at), ts(now), observation_id))
            conn.execute(
                "INSERT INTO aag_latest_pointers VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(project,"
                " repository_id, target_ref, governance_scope) DO UPDATE SET"
                " snapshot_id=excluded.snapshot_id, observation_id=excluded.observation_id,"
                " run_id=excluded.run_id, resolved_commit_sha=excluded.resolved_commit_sha,"
                " expected_target_ref_head_sha=excluded.expected_target_ref_head_sha,"
                " generated_at=excluded.generated_at, verified_at=excluded.verified_at",
                (*scope, snapshot_id, observation_id, run_id, resolved_commit_sha, expected_head_sha,
                 ts(generated_at), ts(now)))
        conn.execute("UPDATE aag_scan_runs SET result=?, finished_at=? WHERE id=?",
                     (result, ts(now), run_id))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return {"run_id": run_id, "result": result, "snapshot_id": snapshot_id,
            "authoritative": authoritative, "created_snapshot": created, "replayed": False}


def append_audit(conn: sqlite3.Connection, *, project: str, actor: str, action: str, target: str,
                 reason: str, at: datetime, seq: int) -> None:
    conn.execute(
        "INSERT INTO aag_audit_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"audit-{seq:04d}", project, actor, action, "snapshot", target, None,
         json.dumps({"seq": seq}), reason, f"corr-{seq:04d}", "api", ts(at)))


# ── 백업 ─────────────────────────────────────────────────────────────────────────
def _dump_table(conn: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    order = ", ".join(TABLES[table])
    rows = []
    for raw in conn.execute(f"SELECT * FROM {table} ORDER BY {order}"):  # noqa: S608 - fixed table names
        row = dict(raw)
        for column in JSON_COLUMNS.get(table, ()):
            if row.get(column) is not None:
                row[column] = json.loads(row[column])
        rows.append(row)
    return rows


def backup(conn: sqlite3.Connection, bundle_dir: Path, *, source: str = "fixture") -> dict[str, Any]:
    bundle_dir.mkdir(parents=True, exist_ok=False)
    tables: dict[str, Any] = {}
    audit_rows: list[dict[str, Any]] = []
    for table in TABLES:
        rows = _dump_table(conn, table)
        if table == "aag_audit_events":
            audit_rows = rows
        data = "".join(_line(row) + "\n" for row in rows).encode("utf-8")
        (bundle_dir / f"{table}.jsonl").write_bytes(data)
        tables[table] = {"file": f"{table}.jsonl", "rows": len(rows), "sha256": _sha256(data)}
    manifest = {"schema": BUNDLE_SCHEMA, "source": source, "tables": tables,
                "audit_chain_head": audit_chain(audit_rows)}
    manifest["bundle_digest"] = _bundle_digest(manifest)
    (bundle_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=1, sort_keys=True), encoding="utf-8")
    return manifest


def _bundle_digest(manifest: dict[str, Any]) -> str:
    return _sha256(canonical_json(
        {"schema": manifest.get("schema"), "tables": manifest.get("tables"),
         "audit_chain_head": manifest.get("audit_chain_head")}).encode("utf-8"))


# ── 번들 검증 + 복원 ───────────────────────────────────────────────────────────────
def read_bundle(bundle_dir: Path) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    manifest_path = bundle_dir / "manifest.json"
    if not manifest_path.is_file():
        raise BundleError("missing_manifest")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise BundleError("bad_manifest", str(exc)) from exc
    if not isinstance(manifest, dict) or manifest.get("schema") != BUNDLE_SCHEMA:
        raise BundleError("schema_mismatch", str(manifest.get("schema") if isinstance(manifest, dict) else ""))
    listed = manifest.get("tables")
    if not isinstance(listed, dict) or set(listed) != set(TABLES):
        raise BundleError("table_set_mismatch", str(sorted(listed) if isinstance(listed, dict) else listed))
    if manifest.get("bundle_digest") != _bundle_digest(manifest):
        raise BundleError("bundle_digest_mismatch")
    known = {"manifest.json", *(meta["file"] for meta in listed.values())}
    extra = sorted(p.name for p in bundle_dir.iterdir() if p.name not in known)
    if extra:
        raise BundleError("unlisted_file", ", ".join(extra))
    data: dict[str, list[dict[str, Any]]] = {}
    for table, meta in listed.items():
        path = bundle_dir / meta["file"]
        if not path.is_file():
            raise BundleError("missing_table_file", table)
        raw = path.read_bytes()
        if _sha256(raw) != meta["sha256"]:
            raise BundleError("checksum_mismatch", table)
        rows = []
        for number, text in enumerate(raw.decode("utf-8").splitlines(), 1):
            try:
                row = json.loads(text)
            except ValueError as exc:
                raise BundleError("bad_row", f"{table}:{number}") from exc
            if not isinstance(row, dict):
                raise BundleError("bad_row", f"{table}:{number}")
            rows.append(row)
        if len(rows) != meta["rows"]:
            raise BundleError("row_count_mismatch", f"{table}: {len(rows)} != {meta['rows']}")
        data[table] = rows
    if audit_chain(data["aag_audit_events"]) != manifest.get("audit_chain_head"):
        raise BundleError("audit_chain_mismatch")
    return manifest, data


def restore(bundle_dir: Path, dest: Path) -> sqlite3.Connection:
    """Restore into a brand-new file. Refuses existing paths — there is no way to name a live DB."""
    manifest, data = read_bundle(bundle_dir)
    del manifest
    if dest.exists():
        raise BundleError("destination_exists", str(dest))
    conn = connect(dest)
    create_schema(conn)
    conn.execute("BEGIN")
    try:
        for table in TABLES:
            for row in data[table]:
                values = dict(row)
                for column in JSON_COLUMNS.get(table, ()):
                    if values.get(column) is not None:
                        values[column] = json.dumps(values[column], sort_keys=True)
                columns = list(values)
                conn.execute(
                    f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",  # noqa: S608
                    [values[c] for c in columns])
        conn.execute("COMMIT")
    except sqlite3.Error as exc:
        conn.execute("ROLLBACK")
        conn.close()
        dest.unlink(missing_ok=True)
        raise BundleError("constraint_violation", str(exc)) from exc
    return conn


# ── 정합성 검증 ────────────────────────────────────────────────────────────────────
def _json(value: str | None) -> Any:
    return json.loads(value) if value is not None else None


def verify_integrity(conn: sqlite3.Connection) -> list[dict[str, str]]:
    """Recompute everything derivable. An empty list means every cross-check agreed."""
    failures: list[dict[str, str]] = []

    def fail(code: str, detail: str) -> None:
        failures.append({"code": code, "detail": detail})

    triggers = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    for missing in sorted(EXPECTED_TRIGGERS - triggers):
        fail("immutability_trigger_missing", missing)

    snapshots = {r["id"]: r for r in conn.execute("SELECT * FROM aag_graph_snapshots_v2")}
    for row in snapshots.values():
        graph = {k: _json(row[k]) for k in ("stats", "nodes", "edges", "findings", "unresolved")}
        project = row["project"]
        try:
            recomputed = content_fingerprint(graph, project=project)
        except ValueError as exc:
            fail("stable_key_mismatch", f"{row['id']}: {exc}")
            continue
        if recomputed != row["content_fingerprint"]:
            fail("snapshot_content_fingerprint", row["id"])
        for finding in graph["findings"]:
            if finding.get("stable_finding_key") != stable_finding_key(project, finding):
                fail("stable_key_mismatch", f"{row['id']}: {finding.get('rule')}")
                break
        for column, size in (("node_count", len(graph["nodes"])), ("edge_count", len(graph["edges"])),
                             ("finding_count", len(graph["findings"]))):
            if row[column] != size:
                fail("snapshot_count_mismatch", f"{row['id']}.{column}")
        if row["stable_key_version"] != STABLE_KEY_VERSION:
            fail("stable_key_version", row["id"])

    runs = {r["id"]: r for r in conn.execute("SELECT * FROM aag_scan_runs")}
    observations = {r["id"]: r for r in conn.execute("SELECT * FROM aag_snapshot_observations")}
    for obs in observations.values():
        snap, run = snapshots.get(obs["snapshot_id"]), runs.get(obs["run_id"])
        if snap is None or run is None:
            fail("observation_dangling", obs["id"])
            continue
        if obs["content_fingerprint"] != snap["content_fingerprint"]:
            fail("observation_fingerprint_mismatch", obs["id"])
        if obs["input_fingerprint"] != run["input_fingerprint"]:
            fail("observation_input_mismatch", obs["id"])
        if (obs["resolved_commit_sha"], obs["project"], obs["repository_id"]) != (
                run["resolved_commit_sha"], run["project"], run["repository_id"]):
            fail("observation_run_scope_mismatch", obs["id"])
        if (obs["project"], obs["repository_id"]) != (snap["project"], snap["repository_id"]):
            fail("observation_snapshot_scope_mismatch", obs["id"])

    pointers = [dict(r) for r in conn.execute("SELECT * FROM aag_latest_pointers")]
    heads = {tuple(r[k] for k in TABLES["aag_ref_heads"]): r for r in conn.execute("SELECT * FROM aag_ref_heads")}
    for pointer in pointers:
        scope = tuple(pointer[k] for k in TABLES["aag_latest_pointers"])
        obs = observations.get(pointer["observation_id"])
        if obs is None:
            fail("pointer_dangling", str(scope))
            continue
        if not obs["authoritative"] or obs["verification_status"] != "verified":
            fail("pointer_not_authoritative", str(scope))
        if pointer["resolved_commit_sha"] != obs["resolved_commit_sha"]:
            fail("pointer_commit_mismatch", str(scope))
        if pointer["snapshot_id"] != obs["snapshot_id"] or pointer["run_id"] != obs["run_id"]:
            fail("pointer_link_mismatch", str(scope))
        if scope != (obs["project"], obs["repository_id"], obs["target_ref"], obs["governance_scope"]):
            fail("pointer_scope_mismatch", str(scope))
        head = heads.get(scope)
        if head is None or head["observation_id"] != pointer["observation_id"] \
                or head["head_commit_sha"] != pointer["resolved_commit_sha"]:
            fail("ref_head_mismatch", str(scope))

    rebuilt = {tuple(p[k] for k in TABLES["aag_latest_pointers"]): p for p in rebuild_pointers(conn)}
    current = {tuple(p[k] for k in TABLES["aag_latest_pointers"]): p for p in pointers}
    for scope in sorted(set(rebuilt) ^ set(current)):
        fail("pointer_rebuild_scope_mismatch", str(scope))
    for scope in sorted(set(rebuilt) & set(current)):
        for column in ("snapshot_id", "observation_id", "run_id", "resolved_commit_sha", "verified_at"):
            if rebuilt[scope][column] != current[scope][column]:
                fail("pointer_rebuild_mismatch", f"{scope}.{column}")
    return failures


# ── 포인터 재구성 ──────────────────────────────────────────────────────────────────
def rebuild_pointers(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Latest pointer per scope = newest authoritative+verified observation.

    generated_at 은 ref_heads 에 같은 observation 이 남아 있으면 그 값, 없으면 snapshot 최초
    생성 시각(하한)이다 — 내용이 같은 재스캔(no_change_success)의 실제 스캔 시각은 observation 에
    저장되지 않기 때문이다. 이 경우 `generated_at_source` 가 snapshot_first_seen 으로 남는다.
    """
    rows = conn.execute(
        "SELECT o.*, s.generated_at AS snapshot_generated_at, h.generated_at AS head_generated_at,"
        " h.observation_id AS head_observation_id FROM aag_snapshot_observations o"
        " JOIN aag_graph_snapshots_v2 s ON s.id=o.snapshot_id"
        " JOIN aag_scan_runs r ON r.id=o.run_id"
        " LEFT JOIN aag_ref_heads h ON h.project=o.project AND h.repository_id=o.repository_id"
        "  AND h.target_ref=o.target_ref AND h.governance_scope=o.governance_scope"
        " WHERE o.authoritative=1 AND o.verification_status='verified'"
        " ORDER BY o.project, o.repository_id, o.target_ref, o.governance_scope,"
        "  o.verified_at, o.created_at, o.id").fetchall()
    latest: dict[tuple[str, ...], sqlite3.Row] = {}
    for row in rows:
        latest[(row["project"], row["repository_id"], row["target_ref"], row["governance_scope"])] = row
    out = []
    for scope, row in sorted(latest.items()):
        from_head = row["head_observation_id"] == row["id"] and row["head_generated_at"]
        out.append({
            "project": scope[0], "repository_id": scope[1], "target_ref": scope[2],
            "governance_scope": scope[3], "snapshot_id": row["snapshot_id"],
            "observation_id": row["id"], "run_id": row["run_id"],
            "resolved_commit_sha": row["resolved_commit_sha"],
            "expected_target_ref_head_sha": row["expected_target_ref_head_sha"],
            "generated_at": row["head_generated_at"] if from_head else row["snapshot_generated_at"],
            "generated_at_source": "ref_head" if from_head else "snapshot_first_seen",
            "verified_at": row["verified_at"],
        })
    return out


def apply_rebuilt_pointers(conn: sqlite3.Connection, pointers: list[dict[str, Any]]) -> None:
    conn.execute("BEGIN")
    try:
        conn.execute("DELETE FROM aag_latest_pointers")
        conn.execute("DELETE FROM aag_ref_heads")
        for p in pointers:
            conn.execute(
                "INSERT INTO aag_ref_heads VALUES (?,?,?,?,?,?,?,?)",
                (p["project"], p["repository_id"], p["target_ref"], p["governance_scope"],
                 p["resolved_commit_sha"], p["generated_at"], p["verified_at"], p["observation_id"]))
            conn.execute(
                "INSERT INTO aag_latest_pointers VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (p["project"], p["repository_id"], p["target_ref"], p["governance_scope"],
                 p["snapshot_id"], p["observation_id"], p["run_id"], p["resolved_commit_sha"],
                 p["expected_target_ref_head_sha"], p["generated_at"], p["verified_at"]))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


# ── fixture ────────────────────────────────────────────────────────────────────────
BASE = datetime(2026, 10, 20, 3, 0, tzinfo=UTC)
GO100 = {"project": "GO100", "repository_id": "kis-autotrade-v4", "target_ref": "refs/heads/main"}
OTHER = {"project": "AADS", "repository_id": "aads-server", "target_ref": "refs/heads/main"}


def fixture_graph(project: str, variant: int) -> dict[str, Any]:
    findings = [
        {"rule": "ROUTE_DRIFT", "severity": "P1", "file": f"app/{project.lower()}/routes.py",
         "method": "GET", "path": "/api/v1/orders"},
        {"rule": "ORPHAN_TABLE", "severity": "P2", "table": "trade_log"},
    ]
    if variant >= 2:
        findings.append({"rule": "UNUSED_SYMBOL", "severity": "P3", "symbol": f"legacy_{variant}"})
    nodes = [{"id": f"n{i}", "kind": "module"} for i in range(3 + variant)]
    return {
        "stats": {"coverage": {"inventory_scanned": 10 + variant, "inventory_total": 10 + variant,
                               "risk_scanned": 4, "risk_total": 4}},
        "nodes": nodes, "edges": [{"from": "n0", "to": "n1"}], "findings": findings,
        "unresolved": [],
    }


def build_fixture(conn: sqlite3.Connection) -> dict[str, Any]:
    """Two projects, republish, content change, rejected and failed runs, audit trail."""
    create_schema(conn)
    sha1, sha2, sha3 = "1" * 40, "2" * 40, "3" * 40
    steps = []

    def step(spec: dict[str, str], variant: int, sha: str, expected: str, minutes: int,
             gen_minutes: int, run: str) -> None:
        steps.append(ingest_artifact(
            conn, graph=fixture_graph(spec["project"], variant), **spec, resolved_commit_sha=sha,
            expected_head_sha=expected, generated_at=BASE + timedelta(minutes=gen_minutes),
            now=BASE + timedelta(minutes=minutes), run_id=run))

    step(GO100, 1, sha1, sha1, 1, 0, "run-go-1")           # first publish
    step(GO100, 1, sha1, sha1, 61, 60, "run-go-2")         # rescan, same content -> no_change_success
    step(GO100, 2, sha2, sha2, 121, 120, "run-go-3")       # new commit/content
    step(GO100, 3, sha3, "9" * 40, 181, 180, "run-go-4")   # source_behind: not authoritative
    step(OTHER, 1, sha1, sha1, 5, 4, "run-aads-1")
    conn.execute(
        "INSERT INTO aag_scan_runs (id, project, repository_id, target_ref, governance_scope,"
        " resolved_commit_sha, expected_target_ref_head_sha, input_fingerprint, scanner_version,"
        " ruleset_digest, scan_scope_digest, normalization_version, host, result, error_code,"
        " started_at, finished_at) VALUES ('run-go-5','GO100','kis-autotrade-v4','refs/heads/main',"
        "'default',?,?,?,'aag-v1.1',?,?,?,'fixture','ingest_failed','ingest_failed',?,?)",
        (sha3, sha3, "d" * 64, "b" * 64, "c" * 64, CANONICALIZATION_VERSION,
         ts(BASE + timedelta(minutes=200)), ts(BASE + timedelta(minutes=201))))
    for seq in range(1, 4):
        append_audit(conn, project="GO100", actor="admin-1", action=f"fixture.action{seq}",
                     target=f"snap-{seq}", reason="restore drill fixture",
                     at=BASE + timedelta(minutes=10 * seq), seq=seq)
    return {"steps": steps}


# ── 손상 시나리오 ──────────────────────────────────────────────────────────────────
def _rewrite(bundle: Path, table: str, fn: Callable[[list[dict[str, Any]]], list[dict[str, Any]]],
             refresh_manifest: bool) -> None:
    path = bundle / f"{table}.jsonl"
    rows = [json.loads(t) for t in path.read_text(encoding="utf-8").splitlines()]
    data = "".join(_line(r) + "\n" for r in fn(rows)).encode("utf-8")
    path.write_bytes(data)
    if refresh_manifest:
        manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
        manifest["tables"][table].update(sha256=_sha256(data), rows=len(data.splitlines()))
        manifest["bundle_digest"] = _bundle_digest(manifest)
        (bundle / "manifest.json").write_text(json.dumps(manifest, indent=1, sort_keys=True), encoding="utf-8")


def _edit_manifest(bundle: Path, fn: Callable[[dict[str, Any]], None]) -> None:
    path = bundle / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    fn(manifest)
    path.write_text(json.dumps(manifest, indent=1, sort_keys=True), encoding="utf-8")


def _tamper_finding(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows[0]["findings"][0]["severity"] = "P0"
    return rows


def _tamper_stable_key(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows[0]["findings"][0]["stable_finding_key"] = "aag-stable-key-v1:" + "0" * 64
    return rows


def _pointer_to_nonauth(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows[0]["observation_id"] = "obs-run-go-4"
    return rows


def _pointer_commit(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows[0]["resolved_commit_sha"] = "f" * 40
    return rows


def _obs_dangling(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows[0]["snapshot_id"] = "snap-does-not-exist"
    return rows


def _audit_reorder(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows[0], rows[1] = rows[1], rows[0]
    return rows


def _audit_drop(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return rows[:-1]


def _obs_fingerprint(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows[0]["content_fingerprint"] = "e" * 64
    return rows


CORRUPTIONS: list[tuple[str, str, Callable[[Path], None]]] = [
    ("flipped_byte", "checksum_mismatch",
     lambda b: (b / "aag_graph_snapshots_v2.jsonl").write_bytes(
         (b / "aag_graph_snapshots_v2.jsonl").read_bytes().replace(b"P1", b"P0", 1))),
    ("truncated_file", "checksum_mismatch",
     lambda b: (b / "aag_audit_events.jsonl").write_bytes(
         (b / "aag_audit_events.jsonl").read_bytes()[:-40])),
    ("missing_table_file", "missing_table_file",
     lambda b: (b / "aag_latest_pointers.jsonl").unlink()),
    ("unlisted_extra_file", "unlisted_file",
     lambda b: (b / "aag_rogue.jsonl").write_text("{}\n")),
    ("missing_manifest", "missing_manifest", lambda b: (b / "manifest.json").unlink()),
    ("manifest_not_json", "bad_manifest", lambda b: (b / "manifest.json").write_text("{nope")),
    ("manifest_wrong_schema", "schema_mismatch",
     lambda b: _edit_manifest(b, lambda m: m.update(schema="aag-backup-bundle-v0"))),
    ("manifest_digest_forged", "bundle_digest_mismatch",
     lambda b: _edit_manifest(b, lambda m: m.update(bundle_digest="0" * 64))),
    ("row_count_lie", "row_count_mismatch",
     lambda b: _edit_manifest(b, lambda m: (m["tables"]["aag_scan_runs"].update(rows=99),
                                            m.update(bundle_digest=_bundle_digest(m))))),
    ("audit_row_reordered", "audit_chain_mismatch",
     lambda b: _rewrite(b, "aag_audit_events", _audit_reorder, True)),
    ("audit_row_dropped", "audit_chain_mismatch",
     lambda b: _rewrite(b, "aag_audit_events", _audit_drop, True)),
    ("observation_dangling_snapshot", "constraint_violation",
     lambda b: _rewrite(b, "aag_snapshot_observations", _obs_dangling, True)),
    ("snapshot_content_tampered", "snapshot_content_fingerprint",
     lambda b: _rewrite(b, "aag_graph_snapshots_v2", _tamper_finding, True)),
    ("stable_key_tampered", "stable_key_mismatch",
     lambda b: _rewrite(b, "aag_graph_snapshots_v2", _tamper_stable_key, True)),
    ("observation_fingerprint_tampered", "observation_fingerprint_mismatch",
     lambda b: _rewrite(b, "aag_snapshot_observations", _obs_fingerprint, True)),
    ("pointer_to_nonauthoritative_observation", "pointer_not_authoritative",
     lambda b: _rewrite(b, "aag_latest_pointers", _pointer_to_nonauth, True)),
    ("pointer_commit_tampered", "pointer_commit_mismatch",
     lambda b: _rewrite(b, "aag_latest_pointers", _pointer_commit, True)),
]


def restore_and_verify(bundle: Path, dest: Path) -> dict[str, Any]:
    """Outcome: restored+verified (ok) or rejected with a stable code."""
    try:
        conn = restore(bundle, dest)
    except BundleError as exc:
        return {"ok": False, "stage": "restore", "codes": [exc.code], "detail": exc.detail}
    try:
        failures = verify_integrity(conn)
    finally:
        conn.close()
    if failures:
        dest.unlink(missing_ok=True)
        return {"ok": False, "stage": "verify", "codes": sorted({f["code"] for f in failures}),
                "failures": failures}
    return {"ok": True, "stage": "verify", "codes": []}


# ── artifact 재처리 ────────────────────────────────────────────────────────────────
def reprocess_artifact(conn: sqlite3.Connection, graph: dict[str, Any], spec: dict[str, str], *,
                       commit_sha: str, generated_at: datetime, now: datetime, run_id: str) -> dict[str, Any]:
    before = {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in TABLES}  # noqa: S608
    outcome = ingest_artifact(
        conn, graph=graph, **spec, resolved_commit_sha=commit_sha, expected_head_sha=commit_sha,
        generated_at=generated_at, now=now, run_id=run_id)
    after = {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in TABLES}
    return {"outcome": outcome, "row_delta": {t: after[t] - before[t] for t in TABLES}}


# ── drill ──────────────────────────────────────────────────────────────────────────
def run_drill(workdir: Path) -> dict[str, Any]:
    report: dict[str, Any] = {"schema": "aag-restore-drill-v1", "engine": f"sqlite {sqlite3.sqlite_version}",
                              "steps": {}, "corruption": [], "limitations": LIMITATIONS}
    source_path = workdir / "source.sqlite"
    source = connect(source_path)
    build_fixture(source)
    bundle = workdir / "bundle"
    manifest = backup(source, bundle)
    original_pointers = [dict(r) for r in source.execute(
        "SELECT * FROM aag_latest_pointers ORDER BY project, repository_id, target_ref")]
    source.close()
    report["steps"]["backup"] = {
        "tables": {t: m["rows"] for t, m in manifest["tables"].items()},
        "bundle_digest": manifest["bundle_digest"], "audit_chain_head": manifest["audit_chain_head"],
        "bytes": sum(p.stat().st_size for p in bundle.iterdir()),
    }

    restored_path = workdir / "restored.sqlite"
    result = restore_and_verify(bundle, restored_path)
    report["steps"]["restore_verify"] = result
    if not result["ok"]:
        report["ok"] = False
        return report

    conn = connect(restored_path)
    rebuilt = rebuild_pointers(conn)
    wiped = conn.execute("SELECT count(*) FROM aag_latest_pointers").fetchone()[0]
    apply_rebuilt_pointers(conn, rebuilt)
    compare = []
    for original in original_pointers:
        match = next((p for p in rebuilt if (p["project"], p["repository_id"], p["target_ref"],
                                              p["governance_scope"]) == (original["project"],
                                              original["repository_id"], original["target_ref"],
                                              original["governance_scope"])), None)
        identity_ok = bool(match) and all(match[c] == original[c] for c in (
            "snapshot_id", "observation_id", "run_id", "resolved_commit_sha",
            "expected_target_ref_head_sha", "verified_at"))
        compare.append({"project": original["project"], "identity_equal": identity_ok,
                        "generated_at_equal": bool(match) and match["generated_at"] == original["generated_at"],
                        "generated_at_source": match["generated_at_source"] if match else None})
    report["steps"]["pointer_rebuild"] = {
        "pointers_before_wipe": wiped, "rebuilt": len(rebuilt), "compare": compare,
        "post_rebuild_failures": verify_integrity(conn),
    }

    spec = GO100
    sha3_graph = fixture_graph("GO100", 2)
    sha2 = "2" * 40
    same = reprocess_artifact(conn, sha3_graph, spec, commit_sha=sha2,
                              generated_at=BASE + timedelta(minutes=120),
                              now=BASE + timedelta(minutes=300), run_id="run-reprocess-1")
    replay = reprocess_artifact(conn, sha3_graph, spec, commit_sha=sha2,
                                generated_at=BASE + timedelta(minutes=120),
                                now=BASE + timedelta(minutes=300), run_id="run-reprocess-1")
    tampered = fixture_graph("GO100", 2)
    tampered["findings"][0]["rule"] = "SOMETHING_ELSE"
    changed = reprocess_artifact(conn, tampered, spec, commit_sha=sha2,
                                 generated_at=BASE + timedelta(minutes=121),
                                 now=BASE + timedelta(minutes=301), run_id="run-reprocess-2")
    conflict = None
    try:
        ingest_artifact(conn, graph=fixture_graph("GO100", 1), **spec, resolved_commit_sha="1" * 40,
                        expected_head_sha="1" * 40, generated_at=BASE, now=BASE, run_id="run-reprocess-1")
    except ValueError as exc:
        conflict = str(exc)
    key_digest_equal = finding_key_set_digest(
        normalize_findings("GO100", sha3_graph["findings"])) == finding_key_set_digest(
        json.loads(conn.execute("SELECT findings FROM aag_graph_snapshots_v2 WHERE id=?",
                                (same["outcome"]["snapshot_id"],)).fetchone()[0]))
    report["steps"]["artifact_reprocess"] = {
        "same_content": same, "same_run_replay": replay, "changed_content": changed,
        "run_id_reuse_with_different_input_rejected": conflict,
        "stable_key_set_equal": key_digest_equal,
        "post_reprocess_failures": verify_integrity(conn),
    }
    conn.close()

    for name, expected, mutate in CORRUPTIONS:
        copy = workdir / f"corrupt-{name}"
        shutil.copytree(bundle, copy)
        mutate(copy)
        outcome = restore_and_verify(copy, workdir / f"corrupt-{name}.sqlite")
        report["corruption"].append({
            "case": name, "expected": expected, "rejected": not outcome["ok"],
            "stage": outcome["stage"], "codes": outcome["codes"],
            "expected_code_seen": expected in outcome["codes"],
        })
    report["steps"]["destination_guard"] = restore_and_verify(bundle, restored_path)

    rebuild = report["steps"]["pointer_rebuild"]
    reproc = report["steps"]["artifact_reprocess"]
    report["ok"] = bool(
        result["ok"] and rebuild["rebuilt"] == len(original_pointers) and wiped == len(original_pointers)
        and all(c["identity_equal"] for c in rebuild["compare"]) and not rebuild["post_rebuild_failures"]
        and same["outcome"]["result"] == "no_change_success" and not same["row_delta"]["aag_graph_snapshots_v2"]
        and replay["outcome"]["replayed"] and not any(replay["row_delta"].values())
        and changed["outcome"]["created_snapshot"] and conflict is not None and key_digest_equal
        and not reproc["post_reprocess_failures"]
        and all(c["rejected"] and c["expected_code_seen"] for c in report["corruption"])
        and not report["steps"]["destination_guard"]["ok"])
    return report


LIMITATIONS = [
    "sqlite 임시 DB 로 검증했다 — PostgreSQL 15 의 pg_dump/pg_restore, 트랜잭션 격리, advisory lock 은 증명하지 않는다",
    "스키마는 마이그레이션 컬럼의 부분집합이고 ingest 규칙은 aag_ingest_v2.py 결정 로직의 거울이다",
    "입력은 합성 fixture 다 — 운영 데이터 규모·실제 손상 패턴은 검증하지 않았다",
    "audit hash chain 은 백업 시점에 이 도구가 만든다 — 운영 DB 에는 체인이 없으므로 백업 이후의 변조만 잡는다",
    "라이브 DB 복구·운영 복원 절차는 실행하지 않았다 (이 도구는 DSN 을 받지 않는다)",
    "공격자가 파일과 manifest 와 audit_chain_head 를 모두 다시 계산하면 번들 계층은 못 잡는다 — 서명이 필요하다",
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    drill = sub.add_parser("drill", help="fixture 로 백업→복원→재구성→재처리→손상 거부를 실행")
    drill.add_argument("--workdir", default=None)
    drill.add_argument("--report", default=None)
    restore_p = sub.add_parser("restore", help="번들을 새 sqlite 파일로 복원하고 검증")
    restore_p.add_argument("--bundle", required=True)
    restore_p.add_argument("--dest", required=True)
    args = parser.parse_args(argv)
    if args.cmd == "restore":
        result = restore_and_verify(Path(args.bundle), Path(args.dest))
        print(json.dumps(result, ensure_ascii=False, indent=1))
        return 0 if result["ok"] else 1
    if args.workdir:
        workdir = Path(args.workdir)
        workdir.mkdir(parents=True, exist_ok=True)
        report = run_drill(workdir)
    else:
        with tempfile.TemporaryDirectory(prefix="aag-restore-") as tmp:
            report = run_drill(Path(tmp))
    text = json.dumps(report, ensure_ascii=False, indent=1, sort_keys=True, default=str)
    if args.report:
        Path(args.report).write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
