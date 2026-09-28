#!/usr/bin/env python3
"""Read-only, repeatable M3 comparison. Output contains counts and opaque IDs only."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import unquote, urlsplit
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
MAX_BYTES = 262144
PROJECT = re.compile(r"^[A-Z0-9][A-Z0-9_-]{0,63}$")

# Every row query is scoped before any data leaves PostgreSQL. No legacy write is used.
GOALS_SQL = """SELECT d.id, d.goal_id, d.kind, d.doc_path, d.document_key, d.version
FROM goal_documents d JOIN goals g ON g.id=d.goal_id
WHERE g.tenant_id=$1::uuid AND g.project=$2 ORDER BY d.id"""
ARTIFACTS_SQL = """SELECT id, artifact_type, artifact_name, version,
encode(digest(content::text, 'sha256'), 'hex') AS json_hash
FROM project_artifacts WHERE tenant_id=$1::uuid AND upper(project_id)=$2 ORDER BY id"""
CHAT_SQL = """SELECT a.type, count(*) AS rows,
count(DISTINCT encode(digest(convert_to(a.content, 'UTF8'), 'sha256'), 'hex')) AS distinct_hashes
FROM chat_artifacts a JOIN chat_sessions s ON s.id=a.session_id
JOIN chat_workspaces w ON w.id=s.workspace_id
WHERE a.tenant_id=$1::uuid AND s.tenant_id=$1::uuid
AND w.tenant_id=$1::uuid AND upper(w.project_key)=$2 GROUP BY a.type ORDER BY a.type"""
CANONICAL_SQL = """SELECT h.document_key, h.kind, r.version, r.source_path, r.content_hash
FROM project_document_heads h JOIN project_document_revisions r ON r.head_id=h.id
WHERE h.tenant_id=$1::uuid AND r.tenant_id=$1::uuid
AND h.project_key=$2 AND r.project_key=$2 ORDER BY h.document_key, r.revision"""
COUNT_SQL = {
    "heads": "SELECT count(*) FROM project_document_heads WHERE tenant_id=$1::uuid AND project_key=$2",
    "revisions": "SELECT count(*) FROM project_document_revisions WHERE tenant_id=$1::uuid AND project_key=$2",
    "grants": "SELECT count(*) FROM project_document_grants WHERE tenant_id=$1::uuid AND project_key=$2",
    "legacy_links": "SELECT count(*) FROM project_document_legacy_links WHERE tenant_id=$1::uuid AND project_key=$2",
}
def source_hash(path: str, root: Path = ROOT) -> tuple[str, str | None]:
    """Return status and digest without exposing a path or document body."""
    if not isinstance(path, str) or len(path) > 512 or "\\" in path or "://" in path or "\x00" in path:
        return "invalid_path", None
    relative = Path(path)
    if (relative.is_absolute() or not relative.parts or ".." in relative.parts
            or relative.parts[0] not in {"docs", "reports"}
            or any(part.startswith(".") for part in relative.parts)):
        return "invalid_path", None
    try:
        resolved = (root / relative).resolve(strict=True)
        root_resolved = root.resolve()
        if not resolved.is_relative_to(root_resolved) or not resolved.is_file():
            return "invalid_path", None
        # M1 accepts only public repository paths after symlink resolution too.
        resolved_parts = resolved.relative_to(root_resolved).parts
        if (not resolved_parts or resolved_parts[0] not in {"docs", "reports"}
                or any(part.startswith(".") for part in resolved_parts)):
            return "invalid_path", None
        if resolved.stat().st_size > MAX_BYTES:
            return "too_large", None
        with resolved.open("rb") as stream:
            body = stream.read(MAX_BYTES + 1)
        if len(body) > MAX_BYTES:
            return "too_large", None
        body.decode("utf-8")
    except UnicodeDecodeError:
        return "non_utf8", None
    except RuntimeError:
        # Path.resolve raises RuntimeError for a symlink loop on some Python versions.
        return "invalid_path", None
    except (OSError, ValueError):
        return "missing_or_unreadable", None
    if not body:
        return "empty", None
    return "ok", hashlib.sha256(body).hexdigest()


def compare(goal_rows, artifact_rows, chat_rows, revisions, canonical_counts, root: Path = ROOT):
    """Keep identity and hash disagreements separate; never select a canonical winner."""
    canonical = defaultdict(list)
    canonical_identity = Counter()
    canonical_hashes = Counter()
    for row in revisions:
        identity = (row["kind"], row["version"])
        canonical[identity].append((row["source_path"], row["content_hash"], row["document_key"]))
        canonical_identity[(row["document_key"], *identity)] += 1
        canonical_hashes[row["content_hash"]] += 1
    statuses = Counter()
    identity = Counter()
    hashes = Counter()
    paths = Counter()
    matches = Counter()
    key_observations = Counter()
    goal_versions = Counter()
    review_rows = []
    for row in goal_rows:
        path = row["doc_path"]
        status, digest = source_hash(path, root)
        statuses[status] += 1
        key = (row["document_key"], row["kind"], row["version"])
        contract_key = (row["kind"], row["version"])
        goal_versions[(row["kind"], str(row["version"]))] += 1
        identity[key] += 1
        paths[path] += 1
        if digest:
            hashes[digest] += 1
        targets = canonical.get(contract_key, [])
        exact_targets = [target_key for target_path, target_hash, target_key in targets
                         if target_path == path and target_hash == digest]
        if not targets:
            outcome = "unmatched"
        elif status != "ok":
            outcome = "file_unverifiable"
        elif len(exact_targets) > 1:
            outcome = "ambiguous_exact"
            key_observations["ambiguous"] += 1
        elif exact_targets:
            outcome = "exact"
            key_observations["same" if row["document_key"] in exact_targets else "different"] += 1
        elif any(target_path == path for target_path, _, _ in targets):
            outcome = "hash_mismatch"
        else:
            outcome = "path_mismatch"
        matches[outcome] += 1
        review_rows.append({
            "id": row.get("id"), "kind": row["kind"], "version": row["version"],
            "document_key_sha256": hashlib.sha256(str(row["document_key"]).encode()).hexdigest(),
            "path_sha256": hashlib.sha256(str(path).encode()).hexdigest(),
            "file_sha256": digest, "file_status": status, "comparison": outcome,
            "exact_candidate_count": len(exact_targets),
            "document_key_observation": (
                "ambiguous" if len(exact_targets) > 1 else
                "same" if row["document_key"] in exact_targets else "different"
            ) if exact_targets else "not_comparable",
        })
    artifact_hashes = Counter(row["json_hash"] for row in artifact_rows)
    artifact_identity = Counter((row["artifact_type"], row["artifact_name"], row["version"])
                                for row in artifact_rows)
    artifact_versions = Counter((row["artifact_type"], str(row["version"])) for row in artifact_rows)
    chat_count = sum(row["rows"] for row in chat_rows)
    return {
        "canonical": {**dict(canonical_counts),
                      "duplicate_identity_groups": sum(n > 1 for n in canonical_identity.values()),
                      "duplicate_content_hash_groups": sum(n > 1 for n in canonical_hashes.values())},
        "goal_documents": {
            "rows": len(goal_rows), "file_status": dict(sorted(statuses.items())),
            "kind_versions": {f"{kind}|{version}": count for (kind, version), count in sorted(goal_versions.items())},
            "comparison": dict(sorted(matches.items())),
            "document_key_observation": dict(sorted(key_observations.items())),
            "duplicate_identity_groups": sum(n > 1 for n in identity.values()),
            "duplicate_path_groups": sum(n > 1 for n in paths.values()),
            "duplicate_content_hash_groups": sum(n > 1 for n in hashes.values()),
            "unmatched_rows": len(goal_rows) - matches["exact"],
            "review_rows": review_rows,
        },
        "project_artifacts": {
            "rows": len(artifact_rows),
            "kinds": dict(sorted(Counter(row["artifact_type"] for row in artifact_rows).items())),
            "kind_versions": {f"{kind}|{version}": count for (kind, version), count in sorted(artifact_versions.items())},
            "duplicate_json_hash_groups": sum(n > 1 for n in artifact_hashes.values()),
            "duplicate_identity_groups": sum(n > 1 for n in artifact_identity.values()),
            "unmatched_rows": len(artifact_rows),
            "note": "JSON hashes are evidence only; no document body equivalence is inferred",
        },
        "chat_artifacts": {
            "rows": chat_count,
            "kinds": {row["type"]: row["rows"] for row in chat_rows},
            "duplicate_content_hash_rows": sum(row["rows"] - row["distinct_hashes"] for row in chat_rows),
            "unmatched_rows": chat_count,
            "note": "Scoped aggregate only; no chat artifact is promoted automatically",
        },
    }


def connection_params() -> dict:
    """Use DATABASE_URL when present; otherwise use libpq-style PG variables."""
    url = os.environ.get("DATABASE_URL")
    if url:
        parsed = urlsplit(url)
        if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname:
            raise ValueError("invalid DATABASE_URL")
        try:
            port = parsed.port or 5432
        except ValueError as exc:
            raise ValueError("invalid DATABASE_URL port") from exc
        return {"host": parsed.hostname, "port": port,
                "database": unquote(parsed.path.lstrip("/")),
                "user": unquote(parsed.username or ""),
                "password": unquote(parsed.password or "")}
    return {"host": os.environ.get("PGHOST"), "port": int(os.environ.get("PGPORT", "5432")),
            "database": os.environ.get("PGDATABASE"), "user": os.environ.get("PGUSER"),
            "password": os.environ.get("PGPASSWORD")}


def classified_error(exc: Exception) -> str:
    """Keep actionable error detail without exposing connection credentials."""
    name = type(exc).__name__
    code = getattr(exc, "sqlstate", None)
    if code and (code.startswith("08") or code.startswith("28")):
        category = "connection" if code.startswith("08") else "permission"
    elif code == "42501" or name in {"InsufficientPrivilegeError", "InvalidPasswordError"}:
        category = "permission"
    elif code in {"42P01", "42883", "3F000"} or name in {"UndefinedTableError", "UndefinedFunctionError"}:
        category = "schema"
    elif isinstance(exc, (OSError, ConnectionError, TimeoutError)) or "Connection" in name:
        category = "connection"
    else:
        category = "inventory"
    message = str(exc)
    message = re.sub(r"postgres(?:ql)?://[^\s'\"]+", "[DATABASE_URL redacted]", message, flags=re.I)
    message = re.sub(
        r"(?i)(password\s*[=:]\s*)(?:'[^']*'|\"[^\"]*\"|[^\s,;]+)",
        r"\1[redacted]", message,
    )
    return f"{category}: {name}: {message}"


async def inventory(tenant: str | None, project: str, root: Path = ROOT):
    import asyncpg

    # Fail closed on an incomplete scope and on missing canonical schema.
    if tenant is not None:
        UUID(tenant)
    if not PROJECT.fullmatch(project):
        raise ValueError("invalid project key")
    conn = await asyncio.wait_for(asyncpg.connect(
        **connection_params(), timeout=5,
        server_settings={"default_transaction_read_only": "on", "statement_timeout": "30000"},
    ), timeout=10)
    try:
        async with conn.transaction(readonly=True, isolation="repeatable_read"):
            # The internal tenant is defined by the existing auth/migration contract.
            trusted_tenant = await conn.fetchval("SELECT public.aads_internal_tenant_id()::text")
            if not trusted_tenant:
                raise RuntimeError("internal tenant unavailable")
            if tenant is not None and UUID(tenant) != UUID(trusted_tenant):
                raise ValueError("tenant does not match internal tenant")
            tenant = trusted_tenant
            args = (tenant, project)
            goals = await conn.fetch(GOALS_SQL, *args)
            artifacts = await conn.fetch(ARTIFACTS_SQL, *args)
            chat = await conn.fetch(CHAT_SQL, *args)
            revisions = await conn.fetch(CANONICAL_SQL, *args)
            counts = {key: await conn.fetchval(sql, *args) for key, sql in COUNT_SQL.items()}
        result = compare(goals, artifacts, chat, revisions, counts, root)
        result["scope"] = {"tenant_id": tenant, "project_key": project}
        return result
    finally:
        await conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-id", help="Optional UUID, checked against the DB internal tenant")
    parser.add_argument("--project-key", required=True)
    args = parser.parse_args()
    try:
        result = asyncio.run(inventory(args.tenant_id, args.project_key))
    except Exception as exc:
        print(f"inventory unavailable: {classified_error(exc)}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
