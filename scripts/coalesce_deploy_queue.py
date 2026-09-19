#!/usr/bin/env python3
"""Fail-closed host-side coalescing for AADS deploy requests.

The API image intentionally has no ``.git`` directory, so commit ancestry can
only be decided on the host.  This script keeps one ready request per release
lane, batches only proven ancestors with complete low-risk manifests, and
persists every inclusion before marking the older request superseded.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

RISKY_PATH_PATTERNS = (
    re.compile(r"(^|/)migrations/", re.IGNORECASE),
    re.compile(r"(^|/)(requirements(?:\.[^/]+)?|poetry\.lock|package-lock\.json|pnpm-lock\.yaml|yarn\.lock)$", re.IGNORECASE),
    re.compile(r"(^|/)(deploy\.sh|docker-compose(?:\.prod)?\.yml)$", re.IGNORECASE),
    re.compile(r"^scripts/deploy", re.IGNORECASE),
)


@dataclass(frozen=True)
class QueueItem:
    run_id: int
    release_sha: str
    phase: str
    deploy_type: str
    approval_policy: str
    changed_files: tuple[str, ...]
    risk_flags: tuple[str, ...]


def manifest_is_low_risk(item: QueueItem) -> bool:
    if item.risk_flags or not item.changed_files:
        return False
    return not any(pattern.search(path) for path in item.changed_files for pattern in RISKY_PATH_PATTERNS)


def classify_batch(items: Iterable[QueueItem], repo: Path) -> tuple[QueueItem, list[QueueItem], str]:
    ordered = sorted(items, key=lambda item: item.run_id)
    if not ordered:
        raise ValueError("at least one queue item is required")
    if len(ordered) == 1:
        return ordered[0], [], "single_request"

    representative = ordered[-1]
    if not manifest_is_low_risk(representative):
        return ordered[0], [], "representative_manifest_unknown_or_risky"

    included: list[QueueItem] = []
    for candidate in ordered[:-1]:
        if candidate.deploy_type != representative.deploy_type:
            return ordered[0], [], "deploy_type_mismatch"
        if candidate.approval_policy != representative.approval_policy:
            return ordered[0], [], "approval_policy_mismatch"
        if not manifest_is_low_risk(candidate):
            return ordered[0], [], "candidate_manifest_unknown_or_risky"
        relation = subprocess.run(
            ["git", "-C", str(repo), "merge-base", "--is-ancestor", candidate.release_sha, representative.release_sha],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if relation.returncode != 0:
            return ordered[0], [], "git_ancestry_not_proven"
        included.append(candidate)
    return representative, included, "all_older_requests_are_low_risk_ancestors"


def _psql(sql: str, *, capture: bool = True) -> str:
    result = subprocess.run(
        ["docker", "exec", "aads-postgres", "psql", "-v", "ON_ERROR_STOP=1", "-U", "aads", "-d", "aads", "-qAt", "-F", "\t", "-c", sql],
        check=True,
        capture_output=capture,
        text=True,
    )
    return result.stdout if capture else ""


def _decode_json(value: str) -> tuple[str, ...]:
    if not value:
        return ()
    decoded = base64.b64decode(value).decode("utf-8")
    payload = json.loads(decoded)
    return tuple(str(item) for item in payload) if isinstance(payload, list) else ()


def _load_items(project: str, component: str, target_env: str) -> list[QueueItem]:
    for value in (project, component, target_env):
        if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
            raise ValueError(f"unsafe lane value: {value!r}")
    rows = _psql(f"""
        SELECT dr.id, dr.release_sha, dr.phase, dr.deploy_type, dr.approval_policy,
               encode(convert_to(COALESCE(drm.changed_files, '[]'::jsonb)::text, 'UTF8'), 'base64'),
               encode(convert_to(COALESCE(drm.risk_flags, '[]'::jsonb)::text, 'UTF8'), 'base64')
          FROM deploy_runs dr
          LEFT JOIN LATERAL (
              SELECT changed_files, risk_flags
                FROM deploy_release_manifests
               WHERE deploy_run_id = dr.id
               ORDER BY id DESC LIMIT 1
          ) drm ON TRUE
         WHERE dr.project = '{project}'
           AND dr.component = '{component}'
           AND dr.target_env = '{target_env}'
           AND dr.status = 'queued'
           AND dr.phase IN ('queued_for_deploy', 'waiting_batch_predecessor')
           AND COALESCE(dr.auto_start, FALSE) = TRUE
         ORDER BY dr.created_at, dr.id;
    """)
    items: list[QueueItem] = []
    for line in rows.splitlines():
        parts = line.split("\t")
        if len(parts) != 7:
            continue
        run_id, sha, phase, deploy_type, policy, files, flags = parts
        if not run_id.isdigit() or not re.fullmatch(r"[0-9a-fA-F]{7,40}", sha):
            continue
        items.append(QueueItem(int(run_id), sha.lower(), phase, deploy_type, policy, _decode_json(files), _decode_json(flags)))
    return items


def _apply_decision(representative: QueueItem, included: list[QueueItem], reason: str, lane: tuple[str, str, str]) -> None:
    project, component, target_env = lane
    all_ids = [representative.run_id, *(item.run_id for item in included)]
    ids_sql = ",".join(str(value) for value in all_ids)
    included_sql = ",".join(str(item.run_id) for item in included) or "0"
    inclusion_values = ",".join(
        "(" + ",".join((
            str(representative.run_id), str(item.run_id), f"'{project}'", f"'{component}'", f"'{target_env}'",
            f"'{item.release_sha}'", f"'{representative.release_sha}'", "'ancestor'", f"'{reason}'", "'host_git_batcher'",
        )) + ")"
        for item in included
    )
    inclusion_sql = ""
    if inclusion_values:
        inclusion_sql = f"""
        INSERT INTO deploy_batch_inclusions(
            representative_run_id, included_run_id, project, component, target_env,
            included_sha, representative_sha, relationship, compatibility_reason, resolved_by
        ) VALUES {inclusion_values}
        ON CONFLICT (included_run_id) DO NOTHING;
        UPDATE deploy_runs
           SET status='superseded', phase='included_in_release_batch', phase_completed_at=NOW(), updated_at=NOW(),
               error_summary=CONCAT_WS('; ', NULLIF(error_summary, ''), 'included in deploy run {representative.run_id}')
         WHERE id IN ({included_sql}) AND status='queued';
        """
    _psql(f"""
        BEGIN;
        SELECT pg_advisory_xact_lock(hashtext('deploy-intake:{project}:{component}:{target_env}'));
        DO $batch$
        BEGIN
            IF (SELECT count(*) FROM deploy_runs WHERE id IN ({ids_sql}) AND status='queued'
                    AND phase IN ('queued_for_deploy', 'waiting_batch_predecessor')) <> {len(all_ids)} THEN
                RAISE EXCEPTION 'deploy queue changed during batch classification';
            END IF;
        END $batch$;
        {inclusion_sql}
        UPDATE deploy_runs
           SET phase = CASE WHEN id={representative.run_id} THEN 'queued_for_deploy' ELSE 'waiting_batch_predecessor' END,
               updated_at=NOW(),
               error_summary=CASE WHEN id={representative.run_id}
                    THEN CONCAT_WS('; ', NULLIF(error_summary, ''), 'host batch decision: {reason}')
                    ELSE error_summary END
         WHERE id IN ({ids_sql}) AND status='queued';
        COMMIT;
    """, capture=False)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--project", default="AADS")
    parser.add_argument("--component", default="api")
    parser.add_argument("--target-env", default="production")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    items = _load_items(args.project, args.component, args.target_env)
    if not items:
        print("batch queue empty")
        return 0
    representative, included, reason = classify_batch(items, args.repo)
    print(f"batch representative={representative.run_id} included={len(included)} reason={reason}")
    if not args.dry_run:
        _apply_decision(representative, included, reason, (args.project, args.component, args.target_env))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
