# GO100 시스템 프롬프트 인덱스 보고서

작성 시각: 2026-04-22 14:31:10 KST (실측)  
대상: GO100(백억이) 워크스페이스

## 1. 바로 열어볼 파일

- 현재 매 턴 주입되는 시스템 컨텍스트 전문:
  [GO100_current_system_context_and_prompt_review_20260422_1212.md](/root/aads/aads-server/reports/GO100_current_system_context_and_prompt_review_20260422_1212.md:1)
- GO100 서비스 내부 프롬프트 전문:
  [GO100_system_prompt_full_text_and_improvement.md](/root/aads/aads-server/reports/GO100_system_prompt_full_text_and_improvement.md:1)
- AADS 조립 구조 + 개선안 감사 보고:
  [GO100_prompt_context_audit_20260422_1307.md](/root/aads/aads-server/reports/GO100_prompt_context_audit_20260422_1307.md:1)
- 이전 통합 보고:
  [GO100_system_prompt_and_current_context_20260422_1015.md](/root/aads/aads-server/reports/GO100_system_prompt_and_current_context_20260422_1015.md:1)

## 2. 실제 소스 위치

- GO100 역할 정의:
  [system_prompt_v2.py](/root/aads/aads-server/app/core/prompts/system_prompt_v2.py:89)
- GO100 capabilities 정의:
  [system_prompt_v2.py](/root/aads/aads-server/app/core/prompts/system_prompt_v2.py:176)
- Layer 1 조립 함수:
  [system_prompt_v2.py](/root/aads/aads-server/app/core/prompts/system_prompt_v2.py:458)
- 현재 시각 + Layer 1/2/메모리/Preload/Auto-RAG 결합:
  [context_builder.py](/root/aads/aads-server/app/services/context_builder.py:350)
- Workspace preload 생성:
  [workspace_preloader.py](/root/aads/aads-server/app/services/workspace_preloader.py:18)
- Auto-RAG 생성:
  [auto_rag.py](/root/aads/aads-server/app/services/auto_rag.py:25)

## 3. 확인 결과

1. GO100 시스템 프롬프트는 2계층입니다.
   - AADS가 GO100 워크스페이스에 매 턴 주입하는 상위 시스템 프롬프트
   - GO100 서비스 내부 투자분석 프롬프트 `go100/ai/prompts.py`
2. AADS 상위 프롬프트는 `behavior/rules/tools/response_guidelines` 중심이며, 동적 컨텍스트는 `context_builder.py`가 매 턴 조립합니다.
3. 현재 턴 시스템 컨텍스트 전문은 이미 문서화돼 있으며, 위 첫 번째 링크에서 클릭해서 전부 확인할 수 있습니다.
4. GO100 서비스 내부 프롬프트 최신 원문은 이번 세션에서 원격 재실측에 실패했습니다.
   - `read_remote_file(project="GO100", file_path="backend/app/services/go100/ai/prompts.py")`
   - 결과: `user cancelled MCP tool call`
   - 따라서 서비스 프롬프트 전문은 기존 로컬 아카이브 기준으로 확인했습니다.

## 4. GO100 서비스 기준 개선 포인트

1. 인텐트 체계 통합
   - AADS 상위 분류(`stock_analysis`, `strategy_design` 등)와 GO100 내부 분류(`stock_info`, `goal_setup` 등)가 다릅니다.
   - 라우터, 프롬프트, 응답 포맷이 같은 enum을 쓰도록 통합하는 것이 우선입니다.
2. REPLY 단계의 근거 강제 강화
   - 투자 서비스 응답은 문장 단위로 `DB 조회 / 백테스트 / 코드 주석 / 미측정` 중 하나의 근거 구분이 붙어야 합니다.
3. 공통 분석 입력 블록 도입
   - `portfolio_snapshot`, `goal_context`, `market_regime`, `recent_backtests`, `conversation_summary`를 UNDERSTAND~REPLY 전 단계에 공통 주입하는 편이 맞습니다.
4. 장문 스펙의 메타데이터화
   - 필터 목록, 전략 제약, 템플릿 예시는 프롬프트 본문에서 분리하고 코드/메타데이터 참조 방식으로 바꾸는 편이 효율적입니다.
5. GO100 전용 CKP 강화
   - 현재 상위 시스템 프롬프트는 운영 규칙은 강하지만 GO100 서비스 흐름(Goal → Strategy → Backtest → Hypothesis → Live)에 대한 구조 설명이 약합니다.

## 5. 권장 다음 작업

1. 원하시면 제가 `system_prompt_v2.py`의 GO100 섹션과 GO100 내부 프롬프트 개선안을 실제 패치안 형태로 바로 작성하겠습니다.
2. 원하시면 GO100 서버211 원격 프롬프트를 다시 읽기 가능한 다른 경로로 재확인해 아카이브가 아니라 최신 원문 기준으로 다시 정리하겠습니다.
