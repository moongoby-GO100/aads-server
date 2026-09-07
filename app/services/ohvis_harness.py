"""OHVIS harness registry and read APIs.

This module makes the existing OHVIS pieces visible as one product contract:
execution harness, LangGraph runtime, LangChain tool surface,
LangSmith-compatible observability, LLM Wiki, Hermes patterns, and Skill Find.
It is intentionally read-oriented and degrades gracefully when the foundation
tables have not been migrated yet.
"""
from __future__ import annotations

import importlib.util
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PROJECTS = ("AADS", "KIS", "GO100", "SF", "NTV2", "NAS", "CEO")

FOUNDATION_TABLES = (
    "ops_skill_library",
    "ops_skill_versions",
    "ops_skill_runs",
    "ohvis_wiki_sources",
    "ohvis_wiki_pages",
    "ohvis_wiki_links",
    "ohvis_wiki_error_book",
    "ohvis_harness_traces",
)

RISK_POLICIES: dict[str, dict[str, Any]] = {
    "read": {
        "decision": "allow",
        "approval_required": False,
        "examples": ["file_read", "select_query", "health_check", "wiki_search"],
    },
    "write": {
        "decision": "approve",
        "approval_required": True,
        "examples": ["file_patch", "non_destructive_db_upsert", "docs_update"],
    },
    "deploy": {
        "decision": "approve",
        "approval_required": True,
        "examples": ["blue_green_deploy", "nginx_cutover", "standby_sync"],
    },
    "auth": {
        "decision": "respond",
        "approval_required": True,
        "examples": ["captcha", "otp", "account_login", "credential_vault"],
    },
    "financial": {
        "decision": "approve",
        "approval_required": True,
        "examples": ["order_submit", "broker_action", "cash_transfer"],
    },
    "destructive": {
        "decision": "reject",
        "approval_required": True,
        "examples": ["drop_table", "truncate", "force_push", "shutdown"],
    },
}


@dataclass(frozen=True)
class SkillSpec:
    slug: str
    title: str
    projects: tuple[str, ...]
    intents: tuple[str, ...]
    risk_tier: str
    source: str
    allowed_tools: tuple[str, ...]
    validation: tuple[str, ...]
    terms: tuple[str, ...]


BUILTIN_SKILLS: tuple[SkillSpec, ...] = (
    SkillSpec(
        slug="aads-bluegreen-release",
        title="AADS blue-green release",
        projects=("AADS",),
        intents=("deploy", "release", "ops"),
        risk_tier="deploy",
        source="builtin",
        allowed_tools=("git", "docker", "deploy.sh", "curl", "query_database"),
        validation=("clean release SHA", "candidate health", "same digest standby", "5m P0/P1 monitor"),
        terms=("aads", "bluegreen", "deploy", "release", "배포", "릴리스"),
    ),
    SkillSpec(
        slug="runner-recovery",
        title="Pipeline Runner recovery",
        projects=PROJECTS,
        intents=("task_query", "pipeline", "ops", "recovery"),
        risk_tier="write",
        source="builtin",
        allowed_tools=("pipeline_runner_status", "read_task_logs", "terminate_task"),
        validation=("status requery", "log evidence", "stale/error separation"),
        terms=("runner", "pipeline", "stale", "approval", "러너", "작업", "복구"),
    ),
    SkillSpec(
        slug="authenticated-site-collector",
        title="Authenticated site collector",
        projects=("AADS", "CEO"),
        intents=("browser_collection", "pc_agent", "auth", "marketing"),
        risk_tier="auth",
        source="builtin",
        allowed_tools=("pc_agent", "browser_bridge", "credential_vault", "browser_tasks"),
        validation=("captcha/otp bypass blocked", "same work_key resume", "dry-run before collection"),
        terms=("login", "collector", "captcha", "otp", "pc agent", "browser", "로그인", "수집"),
    ),
    SkillSpec(
        slug="store-assistant-channel-collector",
        title="Store assistant channel collector",
        projects=("AADS", "CEO"),
        intents=("browser_collection", "store_assistant", "marketing"),
        risk_tier="auth",
        source="builtin",
        allowed_tools=("pc_agent", "browser_bridge", "browser_recipes"),
        validation=("site profile", "account policy", "manual challenge resume"),
        terms=("매장비서", "배민", "스마트플레이스", "쿠팡이츠", "채널", "수집"),
    ),
    SkillSpec(
        slug="go100-market-open-check",
        title="GO100 market open check",
        projects=("GO100",),
        intents=("ops", "finance", "audit"),
        risk_tier="financial",
        source="builtin",
        allowed_tools=("query_project_database", "run_remote_command", "read_remote_file"),
        validation=("stock names included", "read-only first", "order gate respected"),
        terms=("go100", "장초반", "진입", "매매", "종목", "market"),
    ),
    SkillSpec(
        slug="kis-broker-health",
        title="KIS broker health and risk gate",
        projects=("KIS",),
        intents=("ops", "finance", "health"),
        risk_tier="financial",
        source="builtin",
        allowed_tools=("query_project_database", "run_remote_command"),
        validation=("broker session checked", "order risk gate", "read-only report"),
        terms=("kis", "브로커", "계좌", "주문", "체결", "broker"),
    ),
    SkillSpec(
        slug="ntv2-merchant-contract",
        title="NTV2 merchant contract workflow",
        projects=("NTV2",),
        intents=("contract", "merchant", "docs"),
        risk_tier="write",
        source="builtin",
        allowed_tools=("read_remote_file", "run_remote_command", "export_data"),
        validation=("template source", "tenant scope", "document preview"),
        terms=("ntv2", "newtalk", "입점", "계약서", "merchant", "contract"),
    ),
    SkillSpec(
        slug="sf-video-pipeline-health",
        title="ShortFlow video pipeline health",
        projects=("SF",),
        intents=("ops", "video", "health"),
        risk_tier="read",
        source="builtin",
        allowed_tools=("run_remote_command", "list_remote_dir", "read_remote_file"),
        validation=("queue count", "worker health", "latest error sample"),
        terms=("shortflow", "sf", "video", "queue", "worker", "영상"),
    ),
    SkillSpec(
        slug="nas-image-job-health",
        title="NAS image job health",
        projects=("NAS",),
        intents=("ops", "image", "health"),
        risk_tier="read",
        source="builtin",
        allowed_tools=("run_remote_command", "list_remote_dir"),
        validation=("storage capacity", "job queue", "recent failures"),
        terms=("nas", "image", "storage", "job", "이미지", "용량"),
    ),
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _module_available(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def _tokens(text: str) -> set[str]:
    return {token.lower() for token in re.findall(r"[A-Za-z0-9가-힣_.-]+", text or "")}


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return parsed
        except Exception:
            pass
        return [value]
    return [value]


async def _table_exists(conn: Any, table: str) -> bool:
    value = await conn.fetchval(
        """
        SELECT EXISTS (
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema='public' AND table_name=$1
        )
        """,
        table,
    )
    return bool(value)


async def _safe_count(conn: Any, table: str, where: str = "", *args: Any) -> int | None:
    if not await _table_exists(conn, table):
        return None
    query = f"SELECT COUNT(*)::int FROM {table}"
    if where:
        query += f" WHERE {where}"
    return await conn.fetchval(query, *args)


def _skill_to_dict(skill: SkillSpec, score: int = 0, match_reason: list[str] | None = None) -> dict[str, Any]:
    return {
        "slug": skill.slug,
        "title": skill.title,
        "projects": list(skill.projects),
        "intents": list(skill.intents),
        "risk_tier": skill.risk_tier,
        "policy": RISK_POLICIES.get(skill.risk_tier, RISK_POLICIES["read"]),
        "source": skill.source,
        "allowed_tools": list(skill.allowed_tools),
        "validation": list(skill.validation),
        "score": score,
        "match_reason": match_reason or [],
    }


def _score_skill(
    *,
    query_terms: set[str],
    project: str | None,
    intent: str | None,
    slug: str,
    title: str,
    projects: list[str] | tuple[str, ...],
    intents: list[str] | tuple[str, ...],
    terms: set[str] | None = None,
) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []
    project_set = {item.upper() for item in projects}
    if project and (project in project_set or "CEO" in project_set and project == "CEO"):
        score += 3
        reasons.append(f"project:{project}")
    if intent and intent in set(intents):
        score += 2
        reasons.append(f"intent:{intent}")
    haystack = terms or _tokens(" ".join([slug, title, " ".join(projects), " ".join(intents)]))
    matches = sorted(query_terms & haystack)
    if matches:
        score += len(matches) * 2
        reasons.append("terms:" + ",".join(matches[:5]))
    if not query_terms and not project and not intent:
        score = 1
        reasons.append("default")
    return score, reasons


async def _fetch_db_skills() -> list[dict[str, Any]]:
    try:
        from app.core.db_pool import get_pool

        async with get_pool().acquire() as conn:
            if not await _table_exists(conn, "ops_skill_library"):
                return []
            rows = await conn.fetch(
                """
                SELECT slug, title, description, projects, intents, risk_tier,
                       allowed_tools, validation, source_path
                FROM ops_skill_library
                WHERE enabled IS TRUE
                ORDER BY updated_at DESC, slug
                LIMIT 200
                """
            )
            return [dict(row) for row in rows]
    except Exception:
        return []


def scan_repository_skills() -> list[dict[str, Any]]:
    """Return local SKILL.md files as Skill Find candidates."""
    candidates: list[dict[str, Any]] = []
    for base in (".claude/skills", ".codex/skills"):
        root = _repo_root() / base
        if not root.exists():
            continue
        for path in sorted(root.glob("*/SKILL.md")):
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                content = ""
            slug = path.parent.name
            first_heading = next(
                (line.lstrip("# ").strip() for line in content.splitlines() if line.strip().startswith("#")),
                slug,
            )
            candidates.append({
                "slug": slug,
                "title": first_heading or slug,
                "projects": list(PROJECTS),
                "intents": ["skill", "ops"],
                "risk_tier": "write" if "deploy" in content.lower() else "read",
                "policy": RISK_POLICIES["write" if "deploy" in content.lower() else "read"],
                "source": str(path.relative_to(_repo_root())),
                "allowed_tools": [],
                "validation": ["read SKILL.md before action", "follow skill instructions"],
                "content_preview": content[:400],
            })
    return candidates


async def get_harness_status(project: str | None = None) -> dict[str, Any]:
    """Summarize OHVIS harness implementation and database readiness."""
    modules = {
        "langgraph": _module_available("langgraph"),
        "langchain_core": _module_available("langchain_core"),
        "langchain_mcp_adapters": _module_available("langchain_mcp_adapters"),
        "langsmith": _module_available("langsmith"),
        "langfuse": _module_available("langfuse"),
        "langchain": _module_available("langchain"),
    }

    db: dict[str, Any] = {"available": False}
    try:
        from app.core.db_pool import get_pool

        async with get_pool().acquire() as conn:
            table_state = {table: await _table_exists(conn, table) for table in FOUNDATION_TABLES}
            db = {
                "available": True,
                "foundation_tables": table_state,
                "ohvis_tasks": {
                    "total": await _safe_count(conn, "ohvis_tasks"),
                    "running": await _safe_count(conn, "ohvis_tasks", "status='running'"),
                    "stale": await _safe_count(conn, "ohvis_tasks", "status='stale'"),
                },
                "ohvis_loops": {
                    "total": await _safe_count(conn, "ohvis_loops"),
                    "active": await _safe_count(conn, "ohvis_loops", "status='active'"),
                },
                "memory_facts": {
                    "total": await _safe_count(conn, "memory_facts", "superseded_by IS NULL"),
                    "project": await _safe_count(
                        conn,
                        "memory_facts",
                        "project=$1 AND superseded_by IS NULL",
                        project,
                    )
                    if project
                    else None,
                },
                "prompt_assets": {
                    "enabled": await _safe_count(conn, "prompt_assets", "enabled IS TRUE"),
                },
            }
    except Exception as exc:
        db = {"available": False, "error": str(exc)[:200]}

    components = [
        {
            "key": "harness",
            "status": "implemented",
            "evidence": ["this service", "ohvis task API", "risk policy registry"],
            "gap": "graph_run_id is advisory until migration is applied",
        },
        {
            "key": "langgraph",
            "status": "implemented" if modules["langgraph"] else "missing_dependency",
            "evidence": ["pyproject dependency", "app.graph.builder StateGraph"],
            "gap": "task/runner/loop durable run linkage still partial",
        },
        {
            "key": "langchain",
            "status": "partial" if modules["langchain_core"] else "missing_dependency",
            "evidence": ["langchain_core/provider packages", "MCP adapter"],
            "gap": "middleware adapter is exposed as policy, not yet runtime-enforced everywhere",
        },
        {
            "key": "langsmith",
            "status": "compatible_foundation" if modules["langsmith"] else "missing_dependency",
            "evidence": ["langsmith import", "ohvis_harness_traces migration"],
            "gap": "external LangSmith export remains opt-in and not enabled here",
        },
        {
            "key": "llm_wiki",
            "status": "foundation_ready" if db.get("foundation_tables", {}).get("ohvis_wiki_pages") else "memory_only",
            "evidence": ["memory_facts", "wiki migration", "wiki search endpoint"],
            "gap": "automatic report compiler is not wired yet",
        },
        {
            "key": "hermes",
            "status": "pattern_foundation",
            "evidence": ["skill find", "risk policies", "recommendation endpoint"],
            "gap": "external Hermes Agent runtime is intentionally not embedded",
        },
        {
            "key": "skill_find",
            "status": "implemented",
            "evidence": ["builtin skill registry", "repository SKILL.md scanner", "search endpoint"],
            "gap": "DB-backed skill version sync requires migration application",
        },
    ]

    return {
        "project": project,
        "modules": modules,
        "db": db,
        "components": components,
        "risk_policies": RISK_POLICIES,
        "repository_skills": scan_repository_skills(),
    }


async def find_skills(
    query: str,
    project: str | None = None,
    intent: str | None = None,
    limit: int = 5,
) -> dict[str, Any]:
    """Return reusable OHVIS skills for a natural-language task."""
    query_terms = _tokens(query)
    project_norm = project.upper() if project else None
    scored: list[dict[str, Any]] = []
    seen_slugs: set[str] = set()

    for db_skill in await _fetch_db_skills():
        projects = [str(item) for item in _as_list(db_skill.get("projects"))]
        intents = [str(item) for item in _as_list(db_skill.get("intents"))]
        terms = _tokens(
            " ".join(
                str(db_skill.get(key, ""))
                for key in ("slug", "title", "description", "source_path")
            )
        )
        score, reasons = _score_skill(
            query_terms=query_terms,
            project=project_norm,
            intent=intent,
            slug=str(db_skill.get("slug") or ""),
            title=str(db_skill.get("title") or ""),
            projects=projects,
            intents=intents,
            terms=terms,
        )
        if score > 0:
            slug = str(db_skill.get("slug") or "")
            seen_slugs.add(slug)
            scored.append({
                "slug": slug,
                "title": db_skill.get("title"),
                "projects": projects,
                "intents": intents,
                "risk_tier": db_skill.get("risk_tier") or "read",
                "policy": RISK_POLICIES.get(db_skill.get("risk_tier") or "read", RISK_POLICIES["read"]),
                "source": db_skill.get("source_path") or "ops_skill_library",
                "allowed_tools": [str(item) for item in _as_list(db_skill.get("allowed_tools"))],
                "validation": [str(item) for item in _as_list(db_skill.get("validation"))],
                "score": score + 1,
                "match_reason": reasons + ["db_seed"],
            })

    for skill in BUILTIN_SKILLS:
        if skill.slug in seen_slugs:
            continue
        score, reasons = _score_skill(
            query_terms=query_terms,
            project=project_norm,
            intent=intent,
            slug=skill.slug,
            title=skill.title,
            projects=skill.projects,
            intents=skill.intents,
            terms=set(skill.terms),
        )
        if score > 0:
            scored.append(_skill_to_dict(skill, score, reasons))

    for repo_skill in scan_repository_skills():
        haystack = _tokens(
            " ".join(
                str(repo_skill.get(key, ""))
                for key in ("slug", "title", "source", "content_preview")
            )
        )
        matches = sorted(query_terms & haystack)
        score = len(matches) * 2
        reasons = ["repo_skill"] if score else []
        if project_norm and project_norm in repo_skill.get("projects", []):
            score += 1
            reasons.append(f"project:{project_norm}")
        if score > 0:
            item = dict(repo_skill)
            item["score"] = score
            item["match_reason"] = reasons + (["terms:" + ",".join(matches[:5])] if matches else [])
            scored.append(item)

    scored.sort(key=lambda item: (item.get("score", 0), item.get("slug", "")), reverse=True)
    return {
        "query": query,
        "project": project_norm,
        "intent": intent,
        "skills": scored[: max(1, min(limit, 20))],
        "policy_note": "High-risk skills return approval policy only; execution remains gated.",
    }


async def search_wiki(query: str, project: str | None = None, limit: int = 10) -> dict[str, Any]:
    """Search OHVIS wiki pages when migrated, then fall back to memory_facts."""
    limit = max(1, min(limit, 50))
    terms = [term for term in _tokens(query) if len(term) >= 2][:6]
    if not terms:
        return {"query": query, "project": project, "results": [], "source": "none"}

    try:
        from app.core.db_pool import get_pool

        async with get_pool().acquire() as conn:
            if await _table_exists(conn, "ohvis_wiki_pages"):
                pattern = "%" + "%".join(terms) + "%"
                rows = await conn.fetch(
                    """
                    SELECT id::text, project, slug, title, summary, updated_at
                    FROM ohvis_wiki_pages
                    WHERE ($1::text IS NULL OR project=$1)
                      AND (LOWER(title || ' ' || COALESCE(summary,'') || ' ' || COALESCE(body,'')) LIKE LOWER($2))
                    ORDER BY updated_at DESC
                    LIMIT $3
                    """,
                    project,
                    pattern,
                    limit,
                )
                if rows:
                    return {
                        "query": query,
                        "project": project,
                        "source": "ohvis_wiki_pages",
                        "results": [dict(row) for row in rows],
                    }

            pattern = "%" + "%".join(terms) + "%"
            rows = await conn.fetch(
                """
                SELECT id::text, project, category, subject, detail, confidence, created_at, updated_at
                FROM memory_facts
                WHERE superseded_by IS NULL
                  AND ($1::text IS NULL OR project=$1)
                  AND LOWER(COALESCE(subject,'') || ' ' || COALESCE(detail,'') || ' ' || COALESCE(context_snippet,'')) LIKE LOWER($2)
                ORDER BY confidence DESC NULLS LAST, updated_at DESC
                LIMIT $3
                """,
                project,
                pattern,
                limit,
            )
            return {
                "query": query,
                "project": project,
                "source": "memory_facts",
                "results": [dict(row) for row in rows],
            }
    except Exception as exc:
        return {"query": query, "project": project, "results": [], "source": "error", "error": str(exc)[:200]}


async def recommend_hermes_improvements(
    goal: str,
    project: str | None = None,
    recent_failure: str | None = None,
) -> dict[str, Any]:
    """Map a task goal to Hermes-style closed-loop improvement actions."""
    skill_matches = await find_skills(goal, project=project, limit=3)
    actions = [
        {
            "phase": "recall",
            "action": "Search OHVIS wiki/memory and previous task cards before execution.",
            "endpoint": "/api/v1/ohvis/harness/wiki/search",
        },
        {
            "phase": "select_skill",
            "action": "Use top Skill Find candidate and apply its risk policy before tools.",
            "endpoint": "/api/v1/ohvis/harness/skill-find",
        },
        {
            "phase": "execute_with_gate",
            "action": "Pause for approve/respond/reject where risk policy requires it.",
            "policy": RISK_POLICIES,
        },
        {
            "phase": "learn",
            "action": "Persist reusable procedure or error-book candidate after completion.",
            "tables": ["ops_skill_runs", "ohvis_wiki_error_book", "experience_memory"],
        },
    ]
    if recent_failure:
        actions.append({
            "phase": "self_improve",
            "action": "Open a skill improvement candidate from the failure and require replay validation.",
            "failure": recent_failure[:500],
        })
    return {
        "goal": goal,
        "project": project,
        "recommended_skills": skill_matches["skills"],
        "closed_loop_actions": actions,
        "guardrail": "Hermes Agent patterns are absorbed internally; external autonomous runtime is not granted deploy/DB/financial authority.",
    }
