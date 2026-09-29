#!/usr/bin/env python3
"""Read-only M1 runtime baseline; print evidence and fail closed on missing probes."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from urllib.request import urlopen

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.claude_model_contract import EXACT_MODEL_IDS

SLOTS = ("aads-server", "aads-server-green")
MAX_DISCOVERY_AGE_MINUTES = 24 * 60
CATALOG_SQL = """SELECT json_build_object(
  'tier_column_present', EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = current_schema() AND table_name = 'llm_models' AND column_name = 'tier'),
  'fallback_group_column_present', EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = current_schema() AND table_name = 'llm_models' AND column_name = 'fallback_group'),
  'selectable_unverified', count(*) FILTER (
    WHERE is_selectable IS TRUE AND COALESCE(verification_status, '') <> 'verified'),
  'active_null_tier', count(*) FILTER (WHERE is_active IS TRUE AND to_jsonb(m)->>'tier' IS NULL),
  'active_null_fallback_group', count(*) FILTER (WHERE is_active IS TRUE AND to_jsonb(m)->>'fallback_group' IS NULL)
) FROM llm_models AS m"""
DISCOVERY_SQL = """WITH providers AS (
  SELECT provider FROM llm_model_discovery_runs
  UNION SELECT provider FROM llm_models WHERE is_active IS TRUE
), latest AS (
  SELECT DISTINCT ON (provider) provider, status, error, created_at
  FROM llm_model_discovery_runs ORDER BY provider, created_at DESC, id DESC
), success AS (
  SELECT provider, max(created_at) AS last_success_at
  FROM llm_model_discovery_runs WHERE status = 'ok' GROUP BY provider
)
SELECT COALESCE(json_agg(json_build_object(
  'provider', p.provider, 'status', l.status,
  'error', CASE WHEN l.error IS NULL THEN NULL ELSE '[redacted]' END,
  'last_run_at', l.created_at, 'last_success_at', s.last_success_at
) ORDER BY p.provider), '[]'::json)
FROM providers p LEFT JOIN latest l USING (provider) LEFT JOIN success s USING (provider)"""


def _command(argv: list[str]) -> str:
    result = subprocess.run(argv, capture_output=True, text=True, timeout=15, check=True)
    return result.stdout.strip()


def read_relay() -> dict:
    with urlopen("http://127.0.0.1:8199/health", timeout=5) as response:
        return json.load(response)


def read_slot(name: str) -> dict:
    digest = _command(["docker", "inspect", "--format", "{{.Image}}", name])
    version = _command(["docker", "exec", name, "/usr/local/bin/claude-aads", "--version"])
    if not digest or not version:
        raise ValueError("empty digest or CLI version")
    return {"image_digest": digest, "cli_version": version}


def read_database(sql: str):
    # Only SELECT statements supplied by this module; psql runs in the existing DB container.
    output = _command(["docker", "exec", "aads-postgres", "psql", "-X", "-q", "-t", "-A",
                       "-v", "ON_ERROR_STOP=1", "-U", "aads", "-d", "aads", "-c", sql])
    return json.loads(output)


def _timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("discovery timestamp has no timezone")
    return parsed


def check_relay(relay: dict, exact_ids=EXACT_MODEL_IDS) -> dict:
    runtime_models = (relay.get("claude_model_contract") or {}).get("models")
    if not isinstance(runtime_models, list) or not all(isinstance(model, str) for model in runtime_models):
        raise ValueError("relay contract models missing or invalid")
    stale = sorted(set(exact_ids) - set(runtime_models))
    return {"file_models": sorted(exact_ids), "runtime_models": sorted(runtime_models),
            "stale_models": stale, "ok": not stale}


def check_slots(slots: dict) -> dict:
    if set(slots) != set(SLOTS):
        raise ValueError("both API slots are required")
    digests = [slots[name]["image_digest"] for name in SLOTS]
    versions = [slots[name]["cli_version"] for name in SLOTS]
    slot_result = {"containers": slots, "digest_match": bool(digests[0]) and digests[0] == digests[1],
                   "cli_version_match": bool(versions[0]) and versions[0] == versions[1]}
    slot_result["ok"] = bool(slot_result["digest_match"] and slot_result["cli_version_match"])
    return slot_result


def check_catalog(catalog: dict) -> dict:
    count_keys = ("selectable_unverified", "active_null_tier", "active_null_fallback_group")
    counts = {key: int(catalog[key]) for key in count_keys}
    for key in ("tier_column_present", "fallback_group_column_present"):
        counts[key] = catalog[key] is True
    counts["ok"] = not any(counts[key] for key in count_keys) and all(
        counts[key] for key in ("tier_column_present", "fallback_group_column_present"))
    return counts


def check_discovery(discovery: list, now: datetime) -> dict:
    provider_rows = []
    for row in discovery:
        success = _timestamp(row.get("last_success_at"))
        age = round((now - success).total_seconds() / 60, 2) if success else None
        safe_row = {key: row.get(key) for key in ("provider", "status", "last_run_at", "last_success_at")}
        safe_row["error"] = "[redacted]" if row.get("error") is not None else None
        provider_rows.append({**safe_row, "age_minutes": age,
                              "ok": row.get("status") == "ok" and age is not None
                              and 0 <= age <= MAX_DISCOVERY_AGE_MINUTES})
    return {"max_age_minutes": MAX_DISCOVERY_AGE_MINUTES,
            "providers": provider_rows,
            "ok": bool(provider_rows) and all(row["ok"] for row in provider_rows)}


def evaluate(*, relay: dict, slots: dict, catalog: dict, discovery: list,
             now: datetime, exact_ids=EXACT_MODEL_IDS) -> dict:
    checks = {"relay_contract": check_relay(relay, exact_ids),
              "slots": check_slots(slots), "catalog": check_catalog(catalog),
              "discovery": check_discovery(discovery, now)}
    incomplete = [name for name, value in checks.items() if not value["ok"]]
    return {"checked_at": now.isoformat(), "checks": checks,
            "incomplete": incomplete, "ok": not incomplete}


def collect(*, relay_reader=read_relay, slot_reader=read_slot, db_reader=read_database,
            now=None) -> dict:
    now = now or datetime.now(timezone.utc)
    evidence = {}
    errors = {}
    for name, reader in (("relay_contract", relay_reader),
                         ("slots", lambda: {slot: slot_reader(slot) for slot in SLOTS}),
                         ("catalog", lambda: db_reader(CATALOG_SQL)),
                         ("discovery", lambda: db_reader(DISCOVERY_SQL))):
        try:
            evidence[name] = reader()
        except Exception as exc:
            # Command stderr and DB DSNs can contain secrets; emit only exception types.
            errors[name] = type(exc).__name__
    checks = {}
    evaluators = {"relay_contract": lambda: check_relay(evidence["relay_contract"]),
                  "slots": lambda: check_slots(evidence["slots"]),
                  "catalog": lambda: check_catalog(evidence["catalog"]),
                  "discovery": lambda: check_discovery(evidence["discovery"], now)}
    for name, evaluator in evaluators.items():
        if name in errors:
            continue
        try:
            checks[name] = evaluator()
        except Exception as exc:
            errors[name] = type(exc).__name__
    incomplete = sorted(set(errors) | {name for name, value in checks.items() if not value["ok"]})
    return {"checked_at": now.isoformat(), "checks": checks, "errors": errors,
            "incomplete": incomplete, "ok": not incomplete}


def main() -> int:
    result = collect()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, default=str))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
