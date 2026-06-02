from app.services.output_validator import validate_response


def test_confirmation_question_allows_short_direct_answer():
    result = validate_response(
        "네. 전 세션에 적용됩니다.",
        tools_called=True,
        intent="status_check",
        user_message="전세션에 적용되는거지?",
    )

    assert result.is_valid is True


def test_report_intent_rejects_too_short_weak_report():
    result = validate_response(
        "문제 확인했습니다. 조치가 필요합니다.",
        tools_called=True,
        intent="report",
        user_message="문제점과 개선안 보고해",
    )

    assert result.is_valid is False
    assert result.violation_type == "REPORT_STRUCTURE_WEAK"


def test_report_with_quantified_claims_requires_structure_and_sources():
    weak_report = """
요약: 최근 24시간 실행은 99건이고 완료율은 47.5%입니다.

문제점: 완료율이 낮고 중단 건이 많습니다.
원인: 프론트 SSE, polling, dedup 경로가 분리되어 있습니다.
개선안: 상태 동기화 계층을 분리합니다.
검증: 완료율과 중단 버블 수를 재측정합니다.

본문을 길게 만들어 500자 이상으로 확장합니다. 이 보고는 수치와 날짜를 포함하지만
근거 태그가 없고 비교 표도 없습니다. 따라서 CEO 보고서 기준에서는 출처와 표가
누락된 상태입니다. 채팅 스트리밍 문제는 사용자 화면에서 응답이 멈춘 것처럼 보이는
체감 장애를 만들 수 있으므로, 보고에는 DB 조회, 코드 확인, 명령 결과 같은 근거가
명시되어야 합니다. 또한 비교 가능한 항목이 셋 이상이면 표로 정리해야 합니다.
2026-05-29 KST 기준이라는 표현도 실제 명령 근거 없이 쓰면 안 됩니다.
추가 설명입니다. 운영 보고는 숫자를 많이 포함할수록 출처를 함께 붙여야 하며,
그렇지 않으면 실제 DB 조회인지 추정인지 CEO가 구분할 수 없습니다. 스트리밍
완료율, 중단 건수, active running 수, stale placeholder 수처럼 비교 가능한 항목은
표로 묶어야 후속 판단이 가능합니다. 이 문장은 테스트가 장문 보고 조건을 확실히
만족하도록 길이를 보강하기 위한 내용입니다.
"""

    result = validate_response(
        weak_report,
        tools_called=True,
        intent="report",
        user_message="채팅 스트리밍 전수 조사 보고해",
    )

    assert result.is_valid is False
    assert result.violation_type == "REPORT_STRUCTURE_WEAK"
    assert "source_tags" in result.message


def test_structured_report_with_sources_passes():
    strong_report = """
요약: 현재 stale 실행은 0건이고, 문제는 라이브 렌더와 새로고침 렌더의 상태 동기화 차이입니다. [DB 조회]

| 항목 | 결과 | 근거 |
|---|---:|---|
| active running | 3건 | [DB 조회] |
| stale running | 0건 | [DB 조회] |
| streaming placeholder | 3건 | [DB 조회] |

문제점/리스크: 응답 중 화면에는 중단/이어서 버블이 보였지만, 저장 전 취소되면 강력 새로고침 후 복원되지 않을 수 있습니다.
원인/근거: 백엔드 partial flush와 프론트 dedup/filter 경로가 서로 다른 시점에 동작합니다. [코드 확인]
개선 권장안: P0은 partial flush 보존, P1은 SSE/polling 상태 계층 분리, P2는 관측 지표 대시보드화입니다.
검증 방법/완료기준: 24시간 기준 stale running 0건, 완료 후 새로고침 복원 성공, 중단 버블 중복 0건을 재측정합니다. [명령]

→ 다음 단계: P1 상태 동기화 계층 분리 설계를 코드 단위로 진행합니다.
"""

    result = validate_response(
        strong_report,
        tools_called=True,
        intent="report",
        user_message="채팅 스트리밍 전수 조사 보고해",
    )

    assert result.is_valid is True


def test_pipeline_runner_rejects_short_progress_log_as_final_response():
    progress_log = (
        "이전 커밋 `44f4eb67` 반영 상태와 5개 항목별 완료/미완료를 실측으로 확정하겠습니다."
        "서버211에서 git 상태, 최근 커밋 내용, 서비스 상태, 카드 DB 상태를 병렬 실측합니다."
        "이전 커밋 `44f4eb67`의 변경 내용과 현재 적용된 코드를 확인합니다."
        "도구 로드 완료. 이전 작업 반영 여부를 항목별로 실측합니다."
        "SSH 연결이 일시 끊겼습니다. 핵심 미완료 항목을 재확인합니다."
        "서비스가 커밋 후 재시작되어 코드가 반영됐습니다. 남은 항목을 확정합니다."
    )

    result = validate_response(
        progress_log,
        tools_called=True,
        intent="pipeline_runner",
        user_message="이어서 진행해",
    )

    assert result.is_valid is False
    assert result.violation_type == "PROGRESS_ONLY_RESPONSE"


def test_pipeline_runner_rejects_long_tool_backed_progress_tail_as_final_response():
    progress_log = (
        "핵심 확인 완료. #119는 LIVE 상태이고 실매매 차단 조건도 확인했습니다. "
        "근본 원인 요약: bet_amount가 현재가보다 작아 quantity=0이 되었습니다. "
        "포트폴리오 잔고와 fund_pool 상태를 확인했고, signal_processor.py의 bet_size 계산 흐름도 확인했습니다. "
        "추가 설명을 길게 작성하여 700자를 넘깁니다. 도구 호출이 있었더라도 마지막 문장이 실행 예고라면 "
        "최종 완료 보고로 저장되면 안 됩니다. 사용자는 조치와 보고를 요청했으므로 실제 조치 결과, 검증 결과, "
        "남은 리스크가 마지막에 포함되어야 합니다. 이 응답은 중간 실측 로그와 원인 요약은 포함하지만 "
        "아직 DB 수정과 코드 패치를 끝냈다는 보고가 없습니다. 따라서 스트리밍이 정상 done을 내더라도 "
        "completed bubble로 표시되면 안 됩니다. "
        "이제 DB 수정과 코드 패치를 병렬 실행합니다."
    )

    result = validate_response(
        progress_log,
        tools_called=True,
        intent="pipeline_runner",
        user_message="즉시 권장조치 진행해 그리고 119카드 실매매 활성화 하고 보고해",
    )

    assert result.is_valid is False
    assert result.violation_type == "PROGRESS_ONLY_RESPONSE"
