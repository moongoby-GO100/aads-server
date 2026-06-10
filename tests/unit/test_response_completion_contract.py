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


def test_completion_contract_note_is_compact_for_large_dirty_ledger():
    changes = [
        {
            "project": "AADS",
            "repo": "aads-server",
            "file_path": f"app/file_{idx}.py",
            "status": "dirty",
        }
        for idx in range(12)
    ]
    result = evaluate_completion_contract(
        response_text="조치 완료했습니다.",
        user_msg="모두 조치해",
        intent="code_modify",
        changes=changes,
    )

    assert result.adjusted is True
    assert "미완료 변경: 12건" in result.response_text
    assert "외 7건" in result.response_text
    assert "app/file_0.py" in result.response_text
    assert "app/file_5.py" not in result.response_text


def test_completion_contract_blocks_short_progress_log_without_final_report():
    progress_log = (
        "이전 커밋 `44f4eb67` 반영 상태와 5개 항목별 완료/미완료를 실측으로 확정하겠습니다."
        "서버211에서 git 상태, 최근 커밋 내용, 서비스 상태, 카드 DB 상태를 병렬 실측합니다."
        "이전 커밋 `44f4eb67`의 변경 내용과 현재 적용된 코드를 확인합니다."
        "도구 로드 완료. 이전 작업 반영 여부를 항목별로 실측합니다."
        "SSH 연결이 일시 끊겼습니다. 핵심 미완료 항목을 재확인합니다."
        "서비스가 커밋 후 재시작되어 코드가 반영됐습니다. 남은 항목을 확정합니다."
    )

    result = evaluate_completion_contract(
        response_text=progress_log,
        user_msg="이어서 진행해",
        intent="pipeline_runner",
        changes=[],
    )

    assert result.adjusted is True
    assert "final_report_missing" in result.violation_types
    assert "최종 완료보고가 아니라 진행 안내/중간 로그" in result.response_text


def test_completion_contract_blocks_long_progress_tail_without_final_report():
    progress_log = (
        "핵심 확인 완료. #119는 LIVE 상태이고 실매매 차단 조건도 확인했습니다. "
        "근본 원인 요약: bet_amount가 현재가보다 작아 quantity=0이 되었습니다. "
        "포트폴리오 잔고와 fund_pool 상태를 확인했고, signal_processor.py의 bet_size 계산 흐름도 확인했습니다. "
        "추가 설명을 길게 작성하여 900자를 넘깁니다. 완료보고 계약은 짧은 응답뿐 아니라 긴 진행 로그도 "
        "마지막 문장이 실행 예고이면 완료로 인정하면 안 됩니다. 사용자는 조치와 보고를 요청했으므로 "
        "조치 결과, 검증 결과, 남은 리스크가 마지막에 포함되어야 합니다. 이 응답은 중간 실측 로그와 "
        "원인 요약은 포함하지만 아직 DB 수정과 코드 패치를 끝냈다는 보고가 없습니다. "
        "따라서 스트리밍이 정상 done을 내더라도 completed bubble로 표시되면 안 됩니다. "
        "문장을 더 늘려 길이 예외를 확실히 우회합니다. 운영 채팅에서는 도구 호출 이벤트가 많이 누적되더라도 "
        "최종 완료보고가 없으면 미완료 상태로 보존되어야 합니다. "
        "이제 DB 수정과 코드 패치를 병렬 실행합니다."
    )

    result = evaluate_completion_contract(
        response_text=progress_log,
        user_msg="즉시 권장조치 진행해 그리고 119카드 실매매 활성화 하고 보고해",
        intent="pipeline_runner",
        changes=[],
    )

    assert result.adjusted is True
    assert "final_report_missing" in result.violation_types


def test_completion_contract_blocks_awaiting_user_decision_with_incomplete_items():
    response = (
        "이전 작업 상태를 실측하고, 상품 기반 숏츠 테스트 영상 계획을 수립하겠습니다."
        "\n\n**완료된 항목**\n"
        "- Kling image2video 숏츠 테스트: 7건 중 5건 영상 생성 성공\n\n"
        "**보고된 개선 필요사항 3건**\n\n"
        "| 우선순위 | 항목 | 상태 |\n"
        "|---------|------|------|\n"
        "| P0 | 백그라운드 폴링 스케줄러 | 미구현 |\n"
        "| P1 | 이미지 URL 전처리 | 미구현 |\n"
        "| P2 | 생성 영상 자체 스토리지 저장 | 미구현 |\n\n"
        "→ 어떤 항목부터 진행할까요? P0 폴링 스케줄러 구현을 권장합니다."
    )

    result = evaluate_completion_contract(
        response_text=response,
        user_msg="이어서 진행해",
        intent="pipeline_runner",
        changes=[],
    )

    assert result.adjusted is True
    assert "awaiting_user_decision_without_completion" in result.violation_types
    assert "최종 완료보고가 아니라 진행 안내/중간 로그" in result.response_text


def test_completion_contract_allows_structured_report_next_action_tail():
    response = """
요약: stale 실행은 0건이고, 남은 리스크는 프론트 복원 경로의 상태 동기화 차이입니다. [DB 조회]

| 항목 | 결과 | 근거 |
|---|---:|---|
| active running | 3건 | [DB 조회] |
| stale running | 0건 | [DB 조회] |
| streaming placeholder | 3건 | [DB 조회] |

문제점/리스크: 저장 전 취소되면 강력 새로고침 후 복원 타이밍이 달라질 수 있습니다.
원인/근거: 백엔드 partial flush와 프론트 dedup/filter 경로가 서로 다른 시점에 동작합니다. [코드 확인]
개선 권장안: P0은 partial flush 보존, P1은 SSE/polling 상태 계층 분리입니다.
검증 결과: 회귀 테스트와 DB 조회 기준으로 stale running 0건을 확인했습니다. [검증]

→ 다음 단계: P1 상태 동기화 계층 분리 설계를 코드 단위로 진행합니다.
"""

    result = evaluate_completion_contract(
        response_text=response,
        user_msg="채팅 스트리밍 전수 조사 보고해",
        intent="report",
        changes=[],
    )

    assert result.adjusted is False
    assert result.violation_types == []
