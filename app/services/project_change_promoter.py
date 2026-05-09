"""
Project Change Promoter.

Pipeline Runner/커밋 결과에 숨어 있는 중요한 프로젝트 변경을 memory_facts의
고신뢰 카테고리로 승격한다. 목적은 다음 세션의 workspace_preload가 아키텍처,
기능, API, 데이터 모델 변경을 자동으로 인지하게 만드는 것이다.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Iterable, Optional

import structlog

logger = structlog.get_logger(__name__)

STRATEGIC_CHANGE_CATEGORIES = (
    "architecture_decision",
    "feature_change",
    "api_contract",
    "data_model_change",
)

_DIFF_PATH_RE = re.compile(r"^diff --git a/(.*?) b/(.*?)$", re.MULTILINE)
_ENDPOINT_RE = re.compile(r"@(router|app)\.(get|post|put|patch|delete)\(([^)]*)\)")
RAW_CHANGE_CATEGORIES = (
    "file_change",
    "config_change",
    "error_resolution",
    "project_pattern",
    "decision",
    "ceo_instruction",
)


@dataclass(frozen=True)
class PromotedChange:
    project: str
    category: str
    subject: str
    detail: str
    confidence: float
    tags: list[str]
    source_job_id: str


def _field(row: Any, name: str, default: Any = None) -> Any:
    try:
        value = row[name]
    except (KeyError, TypeError):
        value = getattr(row, name, default)
    return default if value is None else value


def _normalize_project(project: Any) -> str:
    value = str(project or "AADS").strip().upper()
    return value[:20] or "AADS"


def _trim(text: Any, limit: int) -> str:
    clean = " ".join(str(text or "").split())
    return clean[:limit]


def extract_changed_files(git_diff: str) -> list[str]:
    """git diff에서 변경 파일 경로를 안정적으로 추출한다."""
    files: list[str] = []
    for left, right in _DIFF_PATH_RE.findall(git_diff or ""):
        path = right if right != "/dev/null" else left
        if path and path not in files:
            files.append(path)
    return files


def _has_any(text: str, keywords: Iterable[str]) -> bool:
    low = text.lower()
    return any(keyword.lower() in low for keyword in keywords)


def _detect_categories(instruction: str, result_output: str, review_feedback: str, git_diff: str, files: list[str]) -> list[str]:
    text = "\n".join([instruction, result_output, review_feedback, git_diff[:4000]])
    paths = "\n".join(files)
    categories: list[str] = []

    if (
        _has_any(text, ["architecture", "아키텍처", "구조", "컨텍스트", "context", "prompt", "프롬프트", "harness", "orchestr", "scheduler", "pipeline", "runner", "agent"])
        or _has_any(paths, ["app/services/", "app/core/", "app/main.py", "docs/SYSTEM_PROMPT_ARCHITECTURE", "pipeline-runner"])
    ):
        categories.append("architecture_decision")

    if (
        _has_any(text, ["feature", "기능", "추가", "구현", "개선", "enable", "support", "페이지", "ui", "dashboard"])
        or any(path.startswith(("app/api/", "app/services/", "scripts/")) for path in files)
    ):
        categories.append("feature_change")

    if (
        _has_any(text, ["endpoint", "api", "router", "contract", "schema", "request", "response", "payload"])
        or any(path.startswith("app/api/") for path in files)
        or bool(_ENDPOINT_RE.search(git_diff or ""))
    ):
        categories.append("api_contract")

    if (
        _has_any(text, ["migration", "migrations/", "create table", "alter table", "index", "schema", "db", "database", "postgres"])
        or any(path.startswith("migrations/") for path in files)
    ):
        categories.append("data_model_change")

    return [category for category in STRATEGIC_CHANGE_CATEGORIES if category in categories]


def classify_pipeline_job(row: Any) -> list[PromotedChange]:
    """pipeline_jobs row 하나를 세션 주입용 전략 변경 facts로 변환한다."""
    job_id = str(_field(row, "job_id", "") or "")
    project = _normalize_project(_field(row, "project", "AADS"))
    status = str(_field(row, "status", "") or "")
    if not job_id or status != "done":
        return []

    instruction = str(_field(row, "instruction", "") or "")
    result_output = str(_field(row, "result_output", "") or "")
    review_feedback = str(_field(row, "review_feedback", "") or "")
    git_diff = str(_field(row, "git_diff", "") or "")
    files = extract_changed_files(git_diff)
    categories = _detect_categories(instruction, result_output, review_feedback, git_diff, files)
    if not categories:
        return []

    instruction_preview = _trim(instruction, 180)
    file_preview = files[:8]
    evidence = {
        "source": "pipeline_jobs",
        "job_id": job_id,
        "status": status,
        "instruction": instruction_preview,
        "files": file_preview,
        "review": _trim(review_feedback, 220),
        "result": _trim(result_output, 220),
    }
    detail = json.dumps(evidence, ensure_ascii=False, sort_keys=True)

    changes: list[PromotedChange] = []
    for category in categories:
        if category == "architecture_decision":
            subject = f"{project} 구조 변경 감지: {instruction_preview or job_id}"
        elif category == "feature_change":
            subject = f"{project} 기능 변경 감지: {instruction_preview or job_id}"
        elif category == "api_contract":
            subject = f"{project} API 계약 변경 감지: {instruction_preview or job_id}"
        else:
            subject = f"{project} 데이터 모델 변경 감지: {instruction_preview or job_id}"

        confidence = 0.9 if files else 0.78
        changes.append(
            PromotedChange(
                project=project,
                category=category,
                subject=subject[:300],
                detail=detail,
                confidence=confidence,
                tags=["project_change_promoter", "pipeline_job", job_id, category, project],
                source_job_id=job_id,
            )
        )

    return changes


def classify_memory_fact(row: Any) -> list[PromotedChange]:
    """기존 원시 memory_facts 이벤트를 전략 변경 facts로 승격한다."""
    source_id = str(_field(row, "id", "") or "")
    project = _normalize_project(_field(row, "project", "AADS"))
    raw_category = str(_field(row, "category", "") or "")
    subject = str(_field(row, "subject", "") or "")
    detail_text = str(_field(row, "detail", "") or "")
    if not source_id or raw_category in STRATEGIC_CHANGE_CATEGORIES:
        return []

    text = f"{raw_category}\n{subject}\n{detail_text}"
    files = re.findall(r"[\w./-]+\.(?:py|ts|tsx|js|jsx|php|sql|md|yml|yaml|json)", text)
    categories = _detect_categories(subject, detail_text, "", text[:4000], files)
    if not categories:
        return []

    evidence = {
        "source": "memory_facts",
        "source_fact_id": source_id,
        "source_category": raw_category,
        "subject": _trim(subject, 220),
        "detail": _trim(detail_text, 500),
        "files": files[:8],
    }
    detail = json.dumps(evidence, ensure_ascii=False, sort_keys=True)

    changes: list[PromotedChange] = []
    for category in categories:
        label = {
            "architecture_decision": "구조",
            "feature_change": "기능",
            "api_contract": "API",
            "data_model_change": "데이터 모델",
        }[category]
        changes.append(
            PromotedChange(
                project=project,
                category=category,
                subject=f"{project} {label} 변경 승격: {_trim(subject, 180) or source_id}"[:300],
                detail=detail,
                confidence=0.82,
                tags=["project_change_promoter", "memory_fact", source_id, category, project],
                source_job_id=source_id,
            )
        )
    return changes


async def _embed_memory_fact(conn: Any, fact_id: Any, change: PromotedChange) -> None:
    try:
        from app.services.chat_embedding_service import embed_texts

        embedding_text = f"{change.project} {change.category} {change.subject}\n{change.detail}"
        embeddings = await embed_texts([embedding_text[:2000]])
        if embeddings and embeddings[0]:
            await conn.execute(
                "UPDATE memory_facts SET embedding = $1::vector WHERE id = $2",
                str(embeddings[0]),
                fact_id,
            )
    except Exception as exc:
        logger.debug("project_change_embedding_failed", fact_id=str(fact_id), error=str(exc))


async def _insert_change(conn: Any, change: PromotedChange, *, embed: bool) -> bool:
    existing_id = await conn.fetchval(
        """
        SELECT id
        FROM memory_facts
        WHERE project = $1
          AND category = $2
          AND superseded_by IS NULL
          AND tags @> ARRAY[$3]::text[]
        LIMIT 1
        """,
        change.project,
        change.category,
        change.source_job_id,
    )
    if existing_id:
        return False

    fact_id = await conn.fetchval(
        """
        INSERT INTO memory_facts
            (project, category, subject, detail, confidence, tags)
        VALUES ($1, $2, $3, $4, $5, $6)
        RETURNING id
        """,
        change.project,
        change.category,
        change.subject,
        change.detail,
        change.confidence,
        change.tags,
    )
    if embed and fact_id:
        await _embed_memory_fact(conn, fact_id, change)
    return True


async def promote_completed_project_changes(
    pool: Any,
    *,
    project: Optional[str] = None,
    days: int = 14,
    limit: int = 20,
    dry_run: bool = False,
    embed: bool = True,
) -> dict[str, Any]:
    """최근 완료 runner 작업을 중요한 프로젝트 변경 memory_facts로 승격한다."""
    project_filter = _normalize_project(project) if project else None
    result: dict[str, Any] = {
        "status": "dry_run" if dry_run else "ok",
        "project": project_filter,
        "days": days,
        "limit": limit,
        "jobs_scanned": 0,
        "raw_facts_scanned": 0,
        "candidate_changes": 0,
        "inserted": 0,
        "skipped_duplicates": 0,
        "changes": [],
        "errors": [],
    }

    where_project = "AND j.project = $3" if project_filter else ""
    params: list[Any] = [days, limit]
    if project_filter:
        params.append(project_filter)
    category_param = len(params) + 1

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT j.job_id, j.project, j.instruction, j.status,
                   j.result_output, j.git_diff, j.review_feedback,
                   j.created_at, j.updated_at
            FROM pipeline_jobs j
            WHERE j.status = 'done'
              AND j.created_at >= NOW() - ($1::int * interval '1 day')
              {where_project}
              AND NOT EXISTS (
                  SELECT 1
                  FROM memory_facts mf
                  WHERE mf.project = j.project
                    AND mf.category = ANY(${category_param}::text[])
                    AND mf.superseded_by IS NULL
                    AND mf.tags @> ARRAY[j.job_id]::text[]
              )
            ORDER BY j.updated_at DESC NULLS LAST, j.created_at DESC
            LIMIT $2
            """,
            *params,
            list(STRATEGIC_CHANGE_CATEGORIES),
        )

        result["jobs_scanned"] = len(rows)
        for row in rows:
            changes = classify_pipeline_job(row)
            result["candidate_changes"] += len(changes)
            for change in changes:
                change_dict = asdict(change)
                result["changes"].append({
                    "project": change.project,
                    "category": change.category,
                    "subject": change.subject,
                    "source_job_id": change.source_job_id,
                })
                if dry_run:
                    continue
                try:
                    inserted = await _insert_change(conn, change, embed=embed)
                    if inserted:
                        result["inserted"] += 1
                    else:
                        result["skipped_duplicates"] += 1
                except Exception as exc:
                    result["errors"].append({
                        "job_id": change.source_job_id,
                        "category": change.category,
                        "error": str(exc)[:300],
                        "change": change_dict,
                    })

        raw_where_project = "AND mf.project = $3" if project_filter else ""
        raw_params: list[Any] = [days, limit]
        if project_filter:
            raw_params.append(project_filter)
        strategic_param = len(raw_params) + 1
        raw_category_param = len(raw_params) + 2
        raw_rows = await conn.fetch(
            f"""
            SELECT mf.id, mf.project, mf.category, mf.subject, mf.detail,
                   mf.created_at, mf.updated_at, mf.confidence
            FROM memory_facts mf
            WHERE mf.created_at >= NOW() - ($1::int * interval '1 day')
              {raw_where_project}
              AND mf.superseded_by IS NULL
              AND mf.confidence >= 0.55
              AND mf.category = ANY(${raw_category_param}::text[])
              AND NOT EXISTS (
                  SELECT 1
                  FROM memory_facts promoted
                  WHERE promoted.project = mf.project
                    AND promoted.category = ANY(${strategic_param}::text[])
                    AND promoted.superseded_by IS NULL
                    AND promoted.tags @> ARRAY[mf.id::text]::text[]
              )
            ORDER BY mf.updated_at DESC, mf.created_at DESC
            LIMIT $2
            """,
            *raw_params,
            list(STRATEGIC_CHANGE_CATEGORIES),
            list(RAW_CHANGE_CATEGORIES),
        )

        result["raw_facts_scanned"] = len(raw_rows)
        for row in raw_rows:
            changes = classify_memory_fact(row)
            result["candidate_changes"] += len(changes)
            for change in changes:
                change_dict = asdict(change)
                result["changes"].append({
                    "project": change.project,
                    "category": change.category,
                    "subject": change.subject,
                    "source_job_id": change.source_job_id,
                })
                if dry_run:
                    continue
                try:
                    inserted = await _insert_change(conn, change, embed=embed)
                    if inserted:
                        result["inserted"] += 1
                    else:
                        result["skipped_duplicates"] += 1
                except Exception as exc:
                    result["errors"].append({
                        "source": change.source_job_id,
                        "category": change.category,
                        "error": str(exc)[:300],
                        "change": change_dict,
                    })

    if result["errors"]:
        result["status"] = "partial_error"

    if result["inserted"]:
        logger.info(
            "project_change_promoter_done",
            inserted=result["inserted"],
            jobs_scanned=result["jobs_scanned"],
            project=project_filter,
        )
    return result
