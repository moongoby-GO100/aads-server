from app.services.response_completion_contract import evaluate_completion_contract


def test_completion_contract_appends_missing_pending_disclosure():
    result = evaluate_completion_contract(
        response_text="수정 반영했습니다. 검증도 통과했습니다.",
        user_msg="이 파일 수정하고 보고해",
        intent="code_modify",
        changes=[
            {
                "project": "AADS",
                "repo": "aads-server",
                "file_path": "app/services/example.py",
                "status": "dirty",
            }
        ],
    )

    assert result.adjusted is True
    assert "missing_commit_push_disclosure" in result.violation_types
    assert "완료 상태 보정" in result.response_text
    assert "app/services/example.py" in result.response_text
    assert "미커밋" in result.response_text


def test_completion_contract_blocks_false_push_done():
    result = evaluate_completion_contract(
        response_text="커밋/푸시 완료했습니다.",
        user_msg="커밋 푸시 진행해",
        intent="git_operation",
        changes=[
            {
                "project": "AADS",
                "repo": "aads-dashboard",
                "file_path": "src/app/page.tsx",
                "status": "committed",
            }
        ],
    )

    assert result.adjusted is True
    assert "push_report_conflicts_with_ledger" in result.violation_types
    assert "커밋됨/미푸시" in result.response_text


def test_completion_contract_accepts_explicit_pending_disclosure():
    response = "수정은 완료했습니다. 커밋/푸시는 아직 하지 않았고 작업트리에 남아 있습니다."
    result = evaluate_completion_contract(
        response_text=response,
        user_msg="수정해",
        intent="code_modify",
        changes=[
            {
                "project": "AADS",
                "repo": "aads-server",
                "file_path": "app/main.py",
                "status": "dirty",
            }
        ],
    )

    assert result.adjusted is False
    assert result.response_text == response


def test_completion_contract_blocks_unverified_document_done():
    result = evaluate_completion_contract(
        response_text="코드 수정과 문서기록 완료했습니다.",
        user_msg="수정하고 문서기록까지 해",
        intent="code_modify",
        changes=[
            {
                "project": "AADS",
                "repo": "aads-server",
                "file_path": "app/services/example.py",
                "status": "deployed",
            }
        ],
    )

    assert result.adjusted is True
    assert "document_report_unverified_by_ledger" in result.violation_types
    assert "완료 상태 보정" in result.response_text


def test_completion_contract_blocks_pending_document_done():
    result = evaluate_completion_contract(
        response_text="HANDOVER 문서 업데이트 완료했습니다.",
        user_msg="문서기록해",
        intent="code_modify",
        changes=[
            {
                "project": "AADS",
                "repo": "aads-server",
                "file_path": "HANDOVER.md",
                "status": "dirty",
            }
        ],
    )

    assert result.adjusted is True
    assert "document_report_conflicts_with_ledger" in result.violation_types
    assert "HANDOVER.md" in result.response_text
