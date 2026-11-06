"""자동 모델 기본값과 재작업 INSERT 회귀 검증."""
import asyncio
import re
from pathlib import Path

import pytest

from app.services import pipeline_auto_rework as ar
from tests.unit.test_pipeline_auto_rework import FakeConn, _inserts, _row

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("worker_model", [None, "", "claude-sonnet-5-5"])
@pytest.mark.parametrize("parent_model", [None, "claude-sonnet-4-6", "claude-sonnet-5-5"])
def test_rework_only_inherits_model_with_worker_override(monkeypatch, worker_model, parent_model):
    monkeypatch.setenv("PIPELINE_AUTO_REWORK", "1")
    conn = FakeConn(_row(model=parent_model, worker_model=worker_model))
    result = asyncio.run(ar.maybe_submit_auto_rework(conn, conn.row["job_id"]))
    assert result and result["round"] == 1
    inserts = _inserts(conn)
    assert len(inserts) == 1
    assert inserts[0][6] == (parent_model if worker_model else None)
    assert inserts[0][8] == worker_model


def test_migration_removes_only_default_and_has_rollback():
    name = "20261106_pipeline_jobs_model_default_null.sql"
    sql = (ROOT / "migrations" / name).read_text()
    statements = re.sub(r"--[^\n]*", "", sql).strip()
    assert statements == "ALTER TABLE pipeline_jobs ALTER COLUMN model SET DEFAULT NULL;"
    assert "claude-sonnet-4-6" not in sql
    rollback = (ROOT / "migrations/rollback" / name).read_text()
    assert "ALTER COLUMN model SET DEFAULT 'claude-sonnet-4-6'" in rollback


def test_runner_claim_coalesces_empty_or_null_models_to_auto():
    source = (ROOT / "scripts/pipeline-runner.sh").read_text()
    expression = "COALESCE(NULLIF(worker_model, ''), NULLIF(model, ''), 'auto')"
    # Both engine claim branches must return the same automatic fallback.
    assert source.count('model_return_expr="' + expression + '"') == 2
    assert "${model_return_expr}" in source or "$model_return_expr" in source
