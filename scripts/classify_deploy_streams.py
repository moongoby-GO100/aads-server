#!/usr/bin/env python3
"""Classify slot-owned chat executions for blue/green deploy drain decisions."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from typing import Any, Iterable


ACTIVE_STATUSES = {"running", "retrying"}
STALE_CLASSES = {"stale_placeholder", "stale_recovery_retry", "orphan_owner"}


@dataclass(frozen=True)
class StreamPolicy:
    heartbeat_ttl_seconds: int = 90


def _int_value(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _bool_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).lower() in {"1", "true", "t", "yes", "y"}


def classify_stream(row: dict[str, Any], policy: StreamPolicy | None = None) -> str:
    """Return a conservative deploy classification for one execution row."""
    policy = policy or StreamPolicy()
    status = str(row.get("status") or "")
    if status not in ACTIVE_STATUSES:
        return "not_active"

    heartbeat_missing = _bool_value(row.get("heartbeat_missing"))
    lease_missing = _bool_value(row.get("lease_missing"))
    lease_active = _bool_value(row.get("lease_active"))
    heartbeat_age = row.get("heartbeat_age_seconds")
    heartbeat_fresh = (
        heartbeat_age is not None
        and _int_value(heartbeat_age, policy.heartbeat_ttl_seconds + 1)
        <= policy.heartbeat_ttl_seconds
    )
    hidden_placeholders = _int_value(row.get("hidden_placeholder_count"))
    visible_placeholders = _int_value(row.get("visible_placeholder_count"))
    assistant_chars = _int_value(row.get("assistant_content_chars"))
    error_message = str(row.get("error_message") or "")
    owner_epoch_missing = row.get("owner_epoch") is None

    if lease_active or heartbeat_fresh:
        if assistant_chars > 0 or visible_placeholders > 0:
            return "live_user_stream"
        return "live_tool_wait"

    if hidden_placeholders > 0 and assistant_chars == 0:
        if error_message == "recovery_auto_retry_scheduled":
            return "stale_recovery_retry"
        return "stale_placeholder"

    if owner_epoch_missing and lease_missing and not heartbeat_missing:
        return "orphan_owner"

    if heartbeat_missing and lease_missing:
        return "unknown"

    if assistant_chars > 0 or visible_placeholders > 0:
        return "live_user_stream"

    return "unknown"


def summarize(rows: Iterable[dict[str, Any]], policy: StreamPolicy | None = None) -> dict[str, Any]:
    policy = policy or StreamPolicy()
    items: list[dict[str, Any]] = []
    classes: dict[str, int] = {}
    for row in rows:
        classification = classify_stream(row, policy)
        enriched = dict(row)
        enriched["classification"] = classification
        items.append(enriched)
        classes[classification] = classes.get(classification, 0) + 1

    live_count = sum(
        1
        for item in items
        if item["classification"] in {"live_user_stream", "live_tool_wait", "unknown"}
    )
    stale_cancel_candidates = [
        str(item["id"])
        for item in items
        if item["classification"] in STALE_CLASSES
        and _int_value(item.get("assistant_content_chars")) == 0
    ]
    return {
        "live_count": live_count,
        "total_count": len(items),
        "classes": classes,
        "stale_cancel_candidates": stale_cancel_candidates,
        "items": items,
    }


def _run_psql(sql: str) -> str:
    proc = subprocess.run(
        ["docker", "exec", "aads-postgres", "psql", "-U", "aads", "-d", "aads", "-qAtc", sql],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "psql failed")
    return proc.stdout.strip()


def fetch_rows(owner_instance: str) -> list[dict[str, Any]]:
    owner_sql = owner_instance.replace("'", "''")
    sql = f"""
        WITH rows AS (
            SELECT te.id::text,
                   te.status,
                   te.owner_instance,
                   te.owner_epoch,
                   COALESCE(te.error_message, '') AS error_message,
                   CASE
                       WHEN te.heartbeat_at IS NULL THEN NULL
                       ELSE EXTRACT(EPOCH FROM (NOW() - te.heartbeat_at))::bigint
                   END AS heartbeat_age_seconds,
                   (te.heartbeat_at IS NULL) AS heartbeat_missing,
                   (te.lease_expires_at IS NULL) AS lease_missing,
                   (te.lease_expires_at > NOW()) AS lease_active,
                   COALESCE(msg.hidden_placeholder_count, 0)::int AS hidden_placeholder_count,
                   COALESCE(msg.visible_placeholder_count, 0)::int AS visible_placeholder_count,
                   COALESCE(msg.assistant_content_chars, 0)::int AS assistant_content_chars
            FROM chat_turn_executions te
            LEFT JOIN LATERAL (
                SELECT COUNT(*) FILTER (
                           WHERE m.intent = 'streaming_placeholder'
                             AND COALESCE(m.is_hidden, false) = true
                       ) AS hidden_placeholder_count,
                       COUNT(*) FILTER (
                           WHERE m.intent = 'streaming_placeholder'
                             AND COALESCE(m.is_hidden, false) = false
                       ) AS visible_placeholder_count,
                       COALESCE(MAX(length(COALESCE(m.content, ''))) FILTER (
                           WHERE m.role = 'assistant'
                             AND COALESCE(m.is_hidden, false) = false
                       ), 0) AS assistant_content_chars
                FROM chat_messages m
                WHERE m.execution_id = te.id
            ) msg ON true
            WHERE te.owner_instance = '{owner_sql}'
              AND te.status IN ('running', 'retrying')
              AND te.completed_at IS NULL
        )
        SELECT COALESCE(jsonb_agg(to_jsonb(rows)), '[]'::jsonb)::text
        FROM rows;
    """
    raw = _run_psql(sql)
    if not raw:
        return []
    return json.loads(raw)


def apply_reconcile(candidate_ids: list[str], reason: str) -> int:
    if not candidate_ids:
        return 0
    ids = ",".join("'" + item.replace("'", "''") + "'::uuid" for item in candidate_ids)
    reason_sql = reason.replace("'", "''")
    sql = f"""
        WITH candidates(id) AS (
            VALUES {",".join(f"({value})" for value in ids.split(","))}
        ),
        safe AS (
            SELECT te.id
            FROM chat_turn_executions te
            JOIN candidates c ON c.id = te.id
            WHERE te.status IN ('running', 'retrying')
              AND te.completed_at IS NULL
              AND NOT EXISTS (
                  SELECT 1
                  FROM chat_messages m
                  WHERE m.execution_id = te.id
                    AND m.role = 'assistant'
                    AND COALESCE(m.is_hidden, false) = false
                    AND length(COALESCE(m.content, '')) > 0
              )
        ),
        updated AS (
            UPDATE chat_turn_executions te
               SET status = 'cancelled',
                   completed_at = NOW(),
                   updated_at = NOW(),
                   lease_expires_at = NOW(),
                   error_message = '{reason_sql}'
              FROM safe s
             WHERE te.id = s.id
             RETURNING te.id
        )
        SELECT COUNT(*)::int FROM updated;
    """
    raw = _run_psql(sql)
    return _int_value(raw.strip())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--owner-instance", required=True)
    parser.add_argument("--ttl-seconds", type=int, default=90)
    parser.add_argument("--mode", choices=["json", "live-count", "reconcile"], default="json")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--rows-json", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    rows = json.loads(args.rows_json) if args.rows_json else fetch_rows(args.owner_instance)
    result = summarize(rows, StreamPolicy(heartbeat_ttl_seconds=max(30, args.ttl_seconds)))
    result["owner_instance"] = args.owner_instance
    result["applied"] = False
    result["cancelled_count"] = 0

    if args.mode == "live-count":
        print(result["live_count"])
        return 0

    if args.mode == "reconcile" and args.apply:
        result["cancelled_count"] = apply_reconcile(
            result["stale_cancel_candidates"],
            "inactive slot stale stream reconciled before deploy",
        )
        result["applied"] = True

    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"error": str(exc), "live_count": "unknown"}), file=sys.stderr)
        raise SystemExit(2)
