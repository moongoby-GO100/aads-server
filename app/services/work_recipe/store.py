"""work_recipes 저장/조회 + 실행 기록기.

레시피는 **덮어쓰지 않는다**. 같은 (tenant, domain, name) 으로 저장하면 버전이
하나 올라간 새 행이 생긴다. 재생 중 깨진 레시피를 고쳤을 때 직전 버전으로
되돌릴 수 있어야 하고, recipe_runs 가 가리키는 버전이 나중에 바뀌면 실행 이력이
거짓말을 하기 때문이다.

DB 스키마: scripts/sql/20260917_work_recipe_schema.sql
"""
from __future__ import annotations

import json
import uuid
from typing import Any, Mapping
from urllib.parse import urlparse

from app.core.db_pool import get_pool
from app.services.work_recipe.schema import WorkRecipe, parse_recipe

# tenant_id 가 NULL 인 전역 레시피를 유니크 인덱스에서 한 값으로 묶기 위한 상수.
GLOBAL_TENANT_SENTINEL = uuid.UUID("00000000-0000-0000-0000-000000000000")


def normalize_domain(value: str) -> str:
    """URL 이든 호스트든 비교 가능한 도메인 문자열로 만든다."""
    text = str(value or "").strip().lower()
    if not text:
        return ""
    if "//" in text:
        parsed = urlparse(text)
        text = parsed.netloc or parsed.path
    text = text.split("/", 1)[0]
    if "@" in text:
        text = text.rsplit("@", 1)[-1]
    if text.startswith("[") and "]" in text:  # IPv6
        return text.split("]", 1)[0] + "]"
    text = text.split(":", 1)[0]
    if text.startswith("www."):
        text = text[4:]
    return text


def _tenant_uuid(tenant_id: Any) -> uuid.UUID | None:
    if tenant_id in (None, "", "null"):
        return None
    if isinstance(tenant_id, uuid.UUID):
        return tenant_id
    return uuid.UUID(str(tenant_id))


def _json_value(raw: Any) -> Any:
    if isinstance(raw, (dict, list)) or raw is None:
        return raw
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "replace")
    if isinstance(raw, str) and raw.strip():
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None
    return None


def row_to_dict(row: Any) -> dict[str, Any]:
    """asyncpg Record → 평범한 사전 (jsonb 는 풀어서)."""
    data = dict(row)
    for key in ("spec", "inputs", "output", "result"):
        if key in data:
            data[key] = _json_value(data[key])
    for key in ("id", "tenant_id", "recipe_id", "run_id"):
        if key in data and data[key] is not None:
            data[key] = str(data[key])
    return data


def row_to_recipe(row: Any) -> WorkRecipe | None:
    """work_recipes 행에서 WorkRecipe 를 복원한다."""
    if row is None:
        return None
    spec = _json_value(dict(row).get("spec"))
    if not isinstance(spec, Mapping):
        return None
    return parse_recipe(spec)


# ---------------------------------------------------------------- 레시피


async def next_version(*, name: str, domain: str, tenant_id: Any = None) -> int:
    """다음 저장에 쓸 버전 번호."""
    async with get_pool().acquire() as conn:
        current = await conn.fetchval(
            """
            SELECT MAX(version)
              FROM work_recipes
             WHERE COALESCE(tenant_id, $1) = COALESCE($2::uuid, $1)
               AND domain = $3
               AND name = $4
            """,
            GLOBAL_TENANT_SENTINEL,
            _tenant_uuid(tenant_id),
            normalize_domain(domain),
            name,
        )
    return int(current or 0) + 1


async def save_recipe(
    recipe: WorkRecipe,
    *,
    tenant_id: Any = None,
    created_by: str = "",
    yaml_source: str | None = None,
) -> dict[str, Any]:
    """레시피를 새 버전으로 저장한다. 기존 행은 건드리지 않는다."""
    domain = normalize_domain(recipe.domain)
    version = await next_version(name=recipe.name, domain=domain, tenant_id=tenant_id)
    spec = recipe.to_dict()
    spec["version"] = version
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO work_recipes (
                tenant_id, name, domain, version, description,
                spec, yaml_source, max_risk, created_by
            ) VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8, $9)
            RETURNING *
            """,
            _tenant_uuid(tenant_id),
            recipe.name,
            domain,
            version,
            recipe.description,
            json.dumps(spec, ensure_ascii=False),
            yaml_source if yaml_source is not None else recipe.to_yaml(),
            recipe.max_risk(),
            created_by,
        )
    return row_to_dict(row)


async def get_recipe_row(
    *,
    name: str,
    domain: str,
    tenant_id: Any = None,
    version: int | None = None,
    enabled_only: bool = True,
) -> dict[str, Any] | None:
    """domain+name(+version) 으로 레시피 행을 찾는다. version 이 없으면 최신."""
    args: list[Any] = [
        GLOBAL_TENANT_SENTINEL,
        _tenant_uuid(tenant_id),
        normalize_domain(domain),
        name,
    ]
    where = [
        "COALESCE(tenant_id, $1) = COALESCE($2::uuid, $1)",
        "domain = $3",
        "name = $4",
    ]
    if enabled_only:
        where.append("enabled = TRUE")
    if version is not None:
        args.append(int(version))
        where.append(f"version = ${len(args)}")
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            f"""
            SELECT *
              FROM work_recipes
             WHERE {' AND '.join(where)}
             ORDER BY version DESC
             LIMIT 1
            """,
            *args,
        )
    return row_to_dict(row) if row else None


async def get_recipe(
    *,
    name: str,
    domain: str,
    tenant_id: Any = None,
    version: int | None = None,
    enabled_only: bool = True,
) -> WorkRecipe | None:
    """재생에 바로 쓸 수 있는 WorkRecipe 를 돌려준다."""
    row = await get_recipe_row(
        name=name,
        domain=domain,
        tenant_id=tenant_id,
        version=version,
        enabled_only=enabled_only,
    )
    if not row:
        return None
    spec = row.get("spec")
    return parse_recipe(spec) if isinstance(spec, Mapping) else None


async def list_recipes(
    *,
    tenant_id: Any = None,
    domain: str | None = None,
    enabled_only: bool = True,
    latest_only: bool = True,
) -> list[dict[str, Any]]:
    """레시피 목록. latest_only 면 (domain, name) 당 최신 버전만."""
    args: list[Any] = [GLOBAL_TENANT_SENTINEL, _tenant_uuid(tenant_id)]
    where = ["COALESCE(tenant_id, $1) = COALESCE($2::uuid, $1)"]
    if domain:
        args.append(normalize_domain(domain))
        where.append(f"domain = ${len(args)}")
    if enabled_only:
        where.append("enabled = TRUE")
    clause = " AND ".join(where)
    if latest_only:
        sql = f"""
            SELECT DISTINCT ON (domain, name) *
              FROM work_recipes
             WHERE {clause}
             ORDER BY domain, name, version DESC
        """
    else:
        sql = f"""
            SELECT *
              FROM work_recipes
             WHERE {clause}
             ORDER BY domain, name, version DESC
        """
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(sql, *args)
    return [row_to_dict(row) for row in rows]


# ------------------------------------------------------------ 실행 기록


class DatabaseRunRecorder:
    """RecipePlayer 가 쓰는 recipe_runs / recipe_run_steps 기록기.

    player 는 recorder 를 선택 사항으로 본다 — DB 가 없는 테스트에서는 주입하지
    않고, 운영에서는 이 클래스를 넘긴다.
    """

    def __init__(self, *, tenant_id: Any = None, triggered_by: str = "", task_id: str | None = None) -> None:
        self.tenant_id = tenant_id
        self.triggered_by = triggered_by
        self.task_id = task_id

    async def start_run(
        self,
        *,
        recipe: WorkRecipe,
        inputs: Mapping[str, Any],
        recipe_id: Any = None,
    ) -> str:
        async with get_pool().acquire() as conn:
            run_id = await conn.fetchval(
                """
                INSERT INTO recipe_runs (
                    tenant_id, recipe_id, recipe_name, domain, recipe_version,
                    status, inputs, triggered_by, task_id, started_at
                ) VALUES ($1, $2::uuid, $3, $4, $5, 'running', $6::jsonb, $7, $8, NOW())
                RETURNING id
                """,
                _tenant_uuid(self.tenant_id),
                str(recipe_id) if recipe_id else None,
                recipe.name,
                normalize_domain(recipe.domain),
                recipe.version,
                json.dumps(_maskable(recipe, inputs), ensure_ascii=False),
                self.triggered_by,
                self.task_id,
            )
        return str(run_id)

    async def record_step(self, *, run_id: Any, step: Any) -> None:
        if run_id is None:
            return
        async with get_pool().acquire() as conn:
            await conn.execute(
                """
                INSERT INTO recipe_run_steps (
                    run_id, seq, phase, action, risk, status,
                    attempts, duration_ms, error, output, llm_calls
                ) VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb, $11)
                """,
                str(run_id),
                step.seq,
                step.phase,
                step.action,
                step.risk,
                step.status,
                step.attempts,
                step.duration_ms,
                step.error,
                json.dumps(_safe_json(step.output), ensure_ascii=False),
                step.llm_calls,
            )

    async def finish_run(self, *, run_id: Any, result: Any) -> None:
        if run_id is None:
            return
        async with get_pool().acquire() as conn:
            await conn.execute(
                """
                UPDATE recipe_runs
                   SET status = $2,
                       llm_calls = $3,
                       error = $4,
                       failed_step_seq = $5,
                       blocked_step_seq = $6,
                       blocked_risk = $7,
                       duration_ms = $8,
                       finished_at = NOW(),
                       updated_at = NOW()
                 WHERE id = $1::uuid
                """,
                str(run_id),
                result.status,
                result.llm_calls,
                result.error,
                result.failed_step_seq,
                result.blocked_step_seq,
                result.blocked_risk,
                result.duration_ms,
            )


def _maskable(recipe: WorkRecipe, inputs: Mapping[str, Any]) -> dict[str, Any]:
    """secret 로 선언된 입력은 값 대신 마스크를 기록한다."""
    secrets = {item.name for item in recipe.inputs if item.secret}
    return {
        key: ("***" if key in secrets else _safe_json(value))
        for key, value in inputs.items()
    }


def _safe_json(value: Any) -> Any:
    """jsonb 로 넣을 수 있는 형태인지 확인하고, 아니면 문자열로 떨어뜨린다."""
    try:
        json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)
    return value
