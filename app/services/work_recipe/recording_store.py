"""테넌트 소유 녹화 상태. 행 잠금은 슬롯 간 단계 추가/종료를 직렬화한다."""
import json
from contextlib import asynccontextmanager
from pathlib import Path

from asyncpg import InterfaceError, PostgresConnectionError, UndefinedTableError

from fastapi import HTTPException

from app.core.db_pool import get_pool
from app.services.work_recipe.recorder import WorkRecipeRecorder
from app.services.work_recipe.store import _json_value, _tenant_uuid


async def ensure_recording_schema() -> None:
    """기존 startup ensure 경로에서 멱등 마이그레이션을 적용한다."""
    schema = Path(__file__).resolve().parents[3] / "migrations/20261029_work_recipe_recordings.sql"
    async with get_pool().acquire() as conn:
        await conn.execute(schema.read_text(encoding="utf-8"))


@asynccontextmanager
async def _available_connection():
    try:
        pool = get_pool()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="recording_store_unavailable") from exc
    try:
        async with pool.acquire() as conn:
            yield conn
    except (UndefinedTableError, PostgresConnectionError, InterfaceError, OSError, TimeoutError) as exc:
        raise HTTPException(status_code=503, detail="recording_store_unavailable") from exc


async def create(recording_id: str, recording: WorkRecipeRecorder) -> None:
    async with _available_connection() as conn:
        await conn.execute(
            "INSERT INTO work_recipe_recordings (id, tenant_id, state) VALUES ($1, $2, $3::jsonb)",
            recording_id, _tenant_uuid(recording.tenant_id), json.dumps(recording.to_state()),
        )


@asynccontextmanager
async def owned(recording_id: str, tenant_id: str):
    async with _available_connection() as conn, conn.transaction():
        row = await conn.fetchrow(
            "SELECT state FROM work_recipe_recordings WHERE id=$1 AND tenant_id=$2 FOR UPDATE",
            recording_id, _tenant_uuid(tenant_id),
        )
        if row is None:
            raise HTTPException(status_code=404, detail="recording_not_found")
        recording = WorkRecipeRecorder.from_state(_json_value(row["state"]), tenant_id)
        yield recording
        if recording._finished:
            await conn.execute(
                "DELETE FROM work_recipe_recordings WHERE id=$1 AND tenant_id=$2",
                recording_id, _tenant_uuid(tenant_id),
            )
        else:
            await conn.execute(
                "UPDATE work_recipe_recordings SET state=$3::jsonb, updated_at=now() WHERE id=$1 AND tenant_id=$2",
                recording_id, _tenant_uuid(tenant_id), json.dumps(recording.to_state()),
            )
