"""서버 후등록 문서의 리뷰 시점 등록 증거 순환 회귀."""
import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from app.services import code_reviewer as cr
from app.services import document_refs as dr


@pytest.mark.parametrize("job_id", [None, "runner-test"])
def test_server_registration_block(job_id):
    instruction = "문서 수정\nDOC_REVISION_FILE: docs/검토.md\nDOC_CHANGE_SUMMARY: 지적 사항 반영"
    result = dr.with_rdoc_block(instruction, job_id)
    assert "새 문서는 정본(project_document_heads)에 초안으로 등록한다" not in result
    assert "정본 등록은 서버가 push 이후 수행한다" in result
    assert "러너는 DOC_REVISION_FILE/DOC_CHANGE_SUMMARY 줄만 남긴다" in result
    assert "정본 등록 시 작성 출처" not in result
    for line in dr.RDOC_PROMPT_BLOCK.splitlines():
        if "초안으로 등록한다" not in line:
            assert line in result
    assert dr.with_rdoc_block(result, job_id) == result


@pytest.mark.parametrize("instruction", ["", "일반 코드 작업\n\n"])
@pytest.mark.parametrize("job_id", [None, "runner-test"])
def test_general_instruction_output_unchanged(instruction, job_id):
    block = (
        "[R-DOC 문서 저장 규칙 — 모든 프로젝트 공통]\n"
        "- 새 문서·리포트는 화면 제목·첫 제목(H1)·파일명의 제목 부분을 한글로 쓰고, 영문 kebab-case document_key 를 붙인다.\n"
        "- 새 문서는 정본(project_document_heads)에 초안으로 등록한다. 기존 문서의 key·경로는 바꾸지 않는다.\n"
        "- 보고에는 파일 경로를 링크처럼 적지 말고, 등록 결과의 열람 참조(view/report_line)를 그대로 쓴다.\n"
        "  열람 참조를 못 받았으면 '정본 미등록(미완료)' 또는 '열람 링크 미제공'이라고 적는다.\n"
    )
    if job_id:
        block += (
            f'- 정본 등록 시 작성 출처 source_task_id="{job_id}" 와 작성 사유 change_summary(왜 이 문서를 쓰는지 한 줄, 10자 이상)를 반드시 넣는다. '
            "둘 중 하나라도 없으면 등록이 거절된다.\n"
        )
    assert dr.with_rdoc_block(instruction, job_id) == instruction.rstrip("\n") + "\n\n" + block


@pytest.mark.parametrize("document_task", [True, False])
def test_review_prompt_registration_timing(monkeypatch, document_task):
    from app.api import canonical_documents
    from app.services import spec_drift

    # 모든 외부 호출을 차단하고 실제 review_code_diff 의 최종 프롬프트를 수집한다.
    artifacts = AsyncMock(return_value=[])
    monkeypatch.setattr(canonical_documents, "runner_revision_artifacts", artifacts)
    monkeypatch.setattr(cr, "_precheck_preservation_gate", lambda *a, **k: None)
    monkeypatch.setattr(cr, "_job_review_context", AsyncMock(return_value={}))
    monkeypatch.setattr(cr, "_get_review_models", AsyncMock(return_value=["claude-cli"]))
    monkeypatch.setattr(cr, "_save_review_result", AsyncMock())
    monkeypatch.setattr(spec_drift, "review_spec_prompt", AsyncMock(return_value=""))
    model = AsyncMock(return_value=json.dumps({
        "verdict": "APPROVE", "issues": [], "summary": "확인",
        "correctness": 0.95, "security": 0.95, "scope_compliance": 0.95,
        "preservation": 0.95, "quality": 0.95,
    }))
    monkeypatch.setattr(cr, "_call_review_model", model)
    instruction = "문서 수정" if document_task else "코드 수정"
    if document_task:
        # 지시서 발췌 한도 밖에 있어도 전체 지시서의 표식을 인식한다.
        instruction += "가" * 7000 + "\nDOC_REVISION_FILE: docs/review.md"
    diff = "diff --git a/docs/review.md b/docs/review.md\n--- a/docs/review.md\n+++ b/docs/review.md\n@@ -1 +1 @@\n-old\n+new\n"
    asyncio.run(cr.review_code_diff("AADS", "runner-test", diff, instruction))
    prompt = model.call_args.kwargs["prompt"]
    for text in (
        "정본 등록은 승인·done 이후 서버가 수행하므로 등록 결과 부재를 반려 사유로 삼지 않는다",
        "(a) diff 에 해당 문서 파일이 있는지",
        "(b) 문서 내용이 지시·지적 사항을 반영했는지",
        "(c) DOC_REVISION_FILE 경로가 diff 의 파일과 일치하는지",
        "코드 변경에 대한 기존 리뷰 기준과 하드 게이트는 그대로 적용한다",
    ):
        assert (text in prompt) is document_task
    artifacts.assert_not_awaited()
