"""AAG snapshot readers and session-tool formatting helpers."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import tempfile
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import asyncpg

from app.core.db_pool import get_pool
from app.services.aag_brief_v2 import build_brief_v2
from app.services.aag_query_v2 import (
    AAGQueryError,
    load_authoritative_snapshot,
    query_findings_page,
    record_consumer_event,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _v2_consumers_enabled() -> bool:
    return os.getenv("AAG_V2_CONSUMERS_ENABLED", "0").strip().lower() in {
        "1", "true", "yes", "on",
    }


def stale_minutes(generated_at: datetime, now: datetime | None = None) -> float:
    """Return non-negative snapshot age in minutes."""
    now = now or datetime.now(UTC)
    if generated_at.tzinfo is None:
        generated_at = generated_at.replace(tzinfo=UTC)
    return round(max(0.0, (now - generated_at).total_seconds() / 60), 2)


def filter_findings(
    findings: Iterable[dict[str, Any]], *, rule: str | None = None,
    severity: str | None = None, path_prefix: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Filter snapshot findings without moving AAG judgement rules into the app."""
    result = []
    for finding in findings:
        if rule and str(finding.get("rule", "")).lower() != rule.lower():
            continue
        if severity and str(finding.get("severity", "")).lower() != severity.lower():
            continue
        if path_prefix:
            paths = [finding.get(key) for key in ("path", "module", "file", "key", "namespace")]
            paths += list(finding.get("modules") or [])
            paths += list(finding.get("owners") or [])
            if not any(str(path or "").startswith(path_prefix) for path in paths):
                continue
        result.append(finding)
        if len(result) >= max(1, min(int(limit), 200)):
            break
    return result


def _json_value(value: Any, fallback: Any) -> Any:
    if value is None:
        return fallback
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return fallback
    return value


async def latest_snapshot(project: str) -> dict[str, Any] | None:
    try:
        async with get_pool().acquire() as conn:
            row = await conn.fetchrow(
                """SELECT project, host, generated_at, commit_sha, stats, findings,
                          unresolved, node_count, edge_count, finding_count
                     FROM aag_graph_snapshots
                    WHERE lower(project) = lower($1)
                    ORDER BY generated_at DESC LIMIT 1""",
                project,
            )
    except (asyncpg.PostgresError, RuntimeError):
        return None
    if not row:
        return None
    data = dict(row)
    data["stats"] = _json_value(data.get("stats"), {})
    data["findings"] = _json_value(data.get("findings"), [])
    data["unresolved"] = _json_value(data.get("unresolved"), [])
    return data


def local_graph(project: str) -> tuple[dict[str, Any] | None, Path | None, str]:
    project_name = project.strip().lower()
    candidates = [REPO_ROOT / "reports" / "aag" / f"{project_name}-graph.json"]
    for path in candidates:
        try:
            graph = json.loads(path.read_text(encoding="utf-8"))
            return graph, path, ""
        except FileNotFoundError:
            continue
        except (OSError, json.JSONDecodeError) as exc:
            return None, None, f"로컬 그래프를 읽지 못했습니다: {exc}"
    return None, None, f"{project.upper()} 스냅샷과 로컬 그래프가 없습니다."


async def get_findings_data(
    project: str, *, rule: str | None = None, severity: str | None = None,
    path_prefix: str | None = None, limit: int = 50,
    repository_id: str | None = None, target_ref: str | None = None,
    governance_scope: str = "default",
) -> dict[str, Any]:
    if _v2_consumers_enabled():
        try:
            page = await query_findings_page(
                project=project, repository_id=repository_id, target_ref=target_ref,
                governance_scope=governance_scope, page_size=limit,
                rule=rule, severity=severity, path_prefix=path_prefix,
            )
        except AAGQueryError as exc:
            await record_consumer_event(
                consumer="session-tool:aag_findings", api_version="v2",
                endpoint="tool:aag_findings", project=project,
                repository_id=repository_id, target_ref=target_ref,
                outcome="query_error",
            )
            return {
                "project": project.upper(), "findings": [],
                "source": "central_db_v2", "authoritative": False,
                "reason": str(exc),
            }
        await record_consumer_event(
            consumer="session-tool:aag_findings", api_version="v2",
            endpoint="tool:aag_findings", project=project,
            repository_id=page["ref"]["repository_id"],
            target_ref=page["ref"]["target_ref"],
            snapshot_id=page["snapshot_id"], outcome="success",
        )
        return {
            "project": page["ref"]["project"],
            "repository_id": page["ref"]["repository_id"],
            "target_ref": page["ref"]["target_ref"],
            "snapshot_id": page["snapshot_id"],
            "commit_sha": page["commit_sha"],
            "generated_at": page["generated_at"].isoformat(),
            "stale_minutes": stale_minutes(page["verified_at"]),
            "source": "central_db_v2",
            "authoritative": True,
            "findings": page["findings"],
            "pagination": page["pagination"],
            "reason": "스냅샷에 결함이 없습니다." if not page["findings"] else "",
        }
    snapshot = await latest_snapshot(project)
    reason = ""
    if snapshot:
        findings = snapshot["findings"]
        generated_at = snapshot["generated_at"]
        source = "database"
    else:
        graph, _, reason = local_graph(project)
        if not graph:
            await record_consumer_event(
                consumer="session-tool:aag_findings", api_version="v1",
                endpoint="tool:aag_findings", project=project, outcome="not_found",
            )
            return {"project": project.upper(), "findings": [], "reason": reason}
        findings = graph.get("findings") or []
        try:
            generated_at = datetime.fromisoformat(str(graph["generated_at"]))
        except (KeyError, TypeError, ValueError):
            await record_consumer_event(
                consumer="session-tool:aag_findings", api_version="v1",
                endpoint="tool:aag_findings", project=project, outcome="invalid_graph",
            )
            return {
                "project": project.upper(), "findings": [],
                "reason": "로컬 그래프의 generated_at이 비어 있거나 올바르지 않습니다.",
            }
        source = "local_graph"
    result = {
        "project": project.upper(),
        "generated_at": generated_at.isoformat(),
        "stale_minutes": stale_minutes(generated_at),
        "source": source,
        "findings": filter_findings(
            findings, rule=rule, severity=severity,
            path_prefix=path_prefix, limit=limit,
        ),
        "reason": reason or ("스냅샷에 결함이 없습니다." if not findings else ""),
    }
    await record_consumer_event(
        consumer="session-tool:aag_findings", api_version="v1",
        endpoint="tool:aag_findings", project=project, outcome="success",
    )
    return result


async def aag_findings_text(**params: Any) -> str:
    data = await get_findings_data(**params)
    findings = data.get("findings", [])
    header = (
        f"AAG {data['project']} | generated_at={data.get('generated_at', '-')} | "
        f"stale_minutes={data.get('stale_minutes', '-')} | source={data.get('source', '-')} | "
        f"snapshot_id={data.get('snapshot_id', '-')} | commit={data.get('commit_sha', '-')}"
    )
    if not findings:
        return f"{header}\n결과 0건. 사유: {data.get('reason') or '필터 조건에 맞는 결함이 없습니다.'}"
    lines = [header, "severity | rule | location | detail", "--- | --- | --- | ---"]
    for finding in findings:
        location = finding.get("path") or finding.get("module") or finding.get("file") or "-"
        detail = str(finding.get("detail", "")).replace("\n", " ")
        lines.append(f"{finding.get('severity', '-')} | {finding.get('rule', '-')} | {location} | {detail}")
    return "\n".join(lines)


async def aag_brief_text(
    project: str, target: str, *, repository_id: str | None = None,
    target_ref: str | None = None, governance_scope: str = "default",
) -> str:
    """Invoke the canonical brief renderer and return stdout unchanged."""
    if _v2_consumers_enabled():
        snapshot = await load_authoritative_snapshot(
            project=project, repository_id=repository_id, target_ref=target_ref,
            governance_scope=governance_scope,
        )
        if not snapshot:
            await record_consumer_event(
                consumer="session-tool:aag_brief", api_version="v2",
                endpoint="tool:aag_brief", project=project,
                repository_id=repository_id, target_ref=target_ref,
                outcome="not_found",
            )
            return "결과 0건. 사유: authoritative AAG v2 snapshot이 없습니다."
        target_key = target.strip().replace("\\", "/")
        matches = []
        for finding in snapshot.get("findings") or []:
            identities = (
                finding.get("file"), finding.get("module"), finding.get("path"),
                finding.get("key"), finding.get("semantic_target"),
            )
            if any(
                target_key and target_key in str(value or "").replace("\\", "/")
                for value in identities
            ):
                matches.append(finding)
        state = "detected" if matches else (
            "partial" if snapshot.get("unresolved") else "not_detected"
        )
        result = build_brief_v2(
            snapshot={
                "snapshot_id": str(snapshot["snapshot_id"]),
                "observation_id": str(snapshot["observation_id"]),
                "run_id": str(snapshot["run_id"]),
                "project": snapshot["project"],
                "repository_id": snapshot["repository_id"],
                "target_ref": snapshot["target_ref"],
                "governance_scope": snapshot["governance_scope"],
                "resolved_commit_sha": snapshot["resolved_commit_sha"],
                "expected_target_ref_head_sha": snapshot["expected_target_ref_head_sha"],
                "source": "central_db", "authoritative": True,
                "verified_at": snapshot["verified_at"],
                "analyzer": {
                    "name": "scan_aads", "version": snapshot["scanner_version"],
                    "ruleset_digest": snapshot["ruleset_digest"],
                    "scan_scope_digest": snapshot["scan_scope_digest"],
                },
                "stats": snapshot.get("stats") or {}, "truncated": False,
            },
            target=target_key, findings=matches, status=state,
        )
        await record_consumer_event(
            consumer="session-tool:aag_brief", api_version="v2",
            endpoint="tool:aag_brief", project=project,
            repository_id=snapshot["repository_id"], target_ref=snapshot["target_ref"],
            snapshot_id=str(snapshot["snapshot_id"]), outcome="success",
        )
        return json.dumps(result, ensure_ascii=False, default=str, indent=2)
    script = REPO_ROOT / "tools" / "aag" / "brief.py"
    target_path = Path(target)
    temp_name: str | None = None
    try:
        target_is_file = target_path.is_file()
    except OSError:
        target_is_file = False
    if target_is_file:
        instruction_path = target_path
    else:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", suffix=".txt", delete=False,
        ) as tmp:
            tmp.write(target)
            temp_name = tmp.name
        instruction_path = Path(tmp.name)
    command = ["python3", str(script), "--instruction-file", str(instruction_path), "--project", project.upper()]
    if project.upper() != "AADS":
        _, graph_path, reason = local_graph(project)
        if not graph_path:
            if temp_name:
                Path(temp_name).unlink(missing_ok=True)
            return f"결과 0건. 사유: {reason}"
        command += ["--graph", str(graph_path)]
    try:
        result = await asyncio.to_thread(
            subprocess.run, command, cwd=REPO_ROOT, capture_output=True, text=True, timeout=20, check=False,
        )
        await record_consumer_event(
            consumer="session-tool:aag_brief", api_version="v1",
            endpoint="tool:aag_brief", project=project,
            outcome="success" if result.stdout else "empty",
        )
        return result.stdout if result.stdout else f"결과 0건. 사유: {result.stderr.strip() or '관련 사실이 없습니다.'}"
    except (OSError, subprocess.TimeoutExpired) as exc:
        await record_consumer_event(
            consumer="session-tool:aag_brief", api_version="v1",
            endpoint="tool:aag_brief", project=project, outcome="error",
        )
        return f"결과 0건. 사유: brief.py 호출 실패: {exc}"
    finally:
        if temp_name:
            Path(temp_name).unlink(missing_ok=True)
