import asyncio
import json

import pytest

from app.services.e2e_verify import (
    assert_screen_evidence_gate,
    evidence_passes_gate,
    screen_verification_required,
)


class _Conn:
    def __init__(self, metadata=None):
        self.metadata = metadata

    async def fetchrow(self, query, job_id):
        assert "e2e_evidence" in query
        return {"metadata": self.metadata} if self.metadata is not None else None


def _evidence(*, passed=True):
    return {
        "schema": "aads.e2e_verify.v1",
        "passed": passed,
        "stages": {
            "dom_assertion": {"passed": passed},
            "screenshot": {"success": passed},
        },
    }


def test_screen_gate_classifies_ui_but_not_backend_work():
    assert screen_verification_required("로그인 필요 화면 Visual QA")
    assert screen_verification_required("component update", ["frontend/components/Login.tsx"])
    assert not screen_verification_required("DB 인덱스와 백엔드 API 수정", ["app/services/report.py"])


def test_evidence_contract_requires_dom_and_capture_success():
    assert evidence_passes_gate(_evidence())
    assert not evidence_passes_gate(_evidence(passed=False))
    assert not evidence_passes_gate({"passed": True, "stages": {}})


def test_missing_evidence_blocks_screen_job_completion_and_approval():
    with pytest.raises(ValueError, match="screen_e2e_evidence_required"):
        asyncio.run(
            assert_screen_evidence_gate(
                _Conn(), job_id="runner-screen", instruction="UI 변경 및 스크린샷 검수", changed_files=[]
            )
        )


def test_passing_evidence_allows_screen_job():
    asyncio.run(
        assert_screen_evidence_gate(
            _Conn({"evidence": json.dumps(_evidence())}),
            job_id="runner-screen",
            instruction="로그인 화면 변경",
            changed_files=[],
        )
    )


def test_backend_job_keeps_existing_flow_without_evidence():
    asyncio.run(
        assert_screen_evidence_gate(
            _Conn(), job_id="runner-api", instruction="백엔드 API 수정", changed_files=["app/services/api.py"]
        )
    )
