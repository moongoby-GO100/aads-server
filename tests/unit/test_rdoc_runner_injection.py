"""R-DOC 러너 지시서 주입·준수 판정 회귀(AADS-RDOC-UNIFIED-STORAGE-20261006)."""
from __future__ import annotations

import hashlib
import json

import pytest

from app.api import pipeline_runner as pr
from app.services import document_refs as dr

INSTR = (
    "TASK_ID: X-1\nTARGET_FILES:\n- app/api/foo.py\nREAD_ONLY_FILES: docs/HANDOVER.md\n"
    "app/services/bar.py 를 고친다.\n"
)


def test_block_is_appended_once_and_idempotent():
    once = dr.with_rdoc_block(INSTR)
    assert once.startswith(INSTR.rstrip("\n"))
    assert once.count(dr.RDOC_BLOCK_MARKER) == 1
    assert dr.with_rdoc_block(once) == once


def test_write_scope_unchanged_by_injection():
    before = pr._parse_write_scope(INSTR)
    after = pr._parse_write_scope(dr.with_rdoc_block(INSTR))
    assert after == before


def test_scanned_paths_unchanged_by_injection():
    assert pr._scan_instruction_paths(dr.with_rdoc_block(INSTR)) == pr._scan_instruction_paths(INSTR)


def test_block_has_no_path_like_tokens_for_legacy_scope():
    legacy = INSTR.replace("TARGET_FILES:\n- app/api/foo.py\n", "")
    assert pr._parse_write_scope(dr.with_rdoc_block(legacy)) == pr._parse_write_scope(legacy)


def test_empty_instruction_gets_block_only():
    assert dr.with_rdoc_block("").strip() == dr.RDOC_PROMPT_BLOCK.strip()


def test_submit_paths_hash_from_original_instruction():
    src = open(pr.__file__, encoding="utf-8").read()
    assert src.count("with_rdoc_block(req.instruction)") == 1
    assert src.count("with_rdoc_block(item.instruction)") == 1
    # 해시는 원문에서 계산한다 — 주입이 중복 판정(instruction_hash)을 바꾸면 안 된다.
    assert hashlib.sha256(INSTR.encode()).hexdigest() != hashlib.sha256(dr.with_rdoc_block(INSTR).encode()).hexdigest()


def test_subagent_default_prompt_carries_rdoc_block():
    import inspect

    from app.services import subagent_service

    assert "RDOC_PROMPT_BLOCK" in inspect.getsource(subagent_service)


class _Conn:
    def __init__(self, files, registered):
        self.files, self.registered = files, registered

    async def fetchrow(self, sql, *a):
        return {"tenant_id": "t-1", "actual_changed_files": json.dumps(self.files)}

    async def fetch(self, sql, tenant, paths):
        return [{"source_path": p} for p in paths if p in self.registered]


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class _Ctx:
            async def __aenter__(self_inner):
                return conn

            async def __aexit__(self_inner, *a):
                return False

        return _Ctx()


NEW_DOC = "reports/20261006_AADS_문서 저장 결과.md"


@pytest.mark.asyncio
async def test_job_compliance_incomplete_when_report_not_registered():
    res = await dr.job_rdoc_compliance(_Pool(_Conn([NEW_DOC, "app/x.py"], set())), "j1")
    assert res["status"] == "incomplete" and res["missing"] == [NEW_DOC]
    assert dr.INCOMPLETE_MARK in dr.format_rdoc_lines(res)[0]


@pytest.mark.asyncio
async def test_job_compliance_complete_when_registered_and_scoped_by_tenant():
    res = await dr.job_rdoc_compliance(_Pool(_Conn([NEW_DOC], {NEW_DOC})), "j1")
    assert res["status"] == "complete"


@pytest.mark.asyncio
async def test_job_compliance_not_applicable_for_code_only():
    res = await dr.job_rdoc_compliance(_Pool(_Conn(["app/x.py"], set())), "j1")
    assert res["status"] == "not_applicable"
    assert dr.format_rdoc_lines(res) == []


@pytest.mark.asyncio
async def test_job_compliance_fail_open_on_db_error():
    class Boom:
        def acquire(self):
            raise RuntimeError("db down")

    assert await dr.job_rdoc_compliance(Boom(), "j1") is None


def test_terminal_followup_message_includes_incomplete_line():
    row = {"job_id": "j1", "project": "AADS", "status": "done", "instruction_preview": "x", "output_preview": "ok"}
    res = dr.evaluate_rdoc_compliance([NEW_DOC], [])
    try:
        msg = pr._terminal_followup_message(row, kind="completed", commit_sha="abc1234", rdoc=res)
    except Exception as exc:  # 시그니처 변화 감지용
        pytest.fail(f"followup message failed: {exc}")
    assert dr.INCOMPLETE_MARK in msg
