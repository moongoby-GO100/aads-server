# GO100 시스템 프롬프트 및 현재 주입 컨텍스트 점검 보고서

작성 시각: 2026-04-22 13:07:36 KST [실측]  
대상 워크스페이스: GO100(백억이)

## 1. 결론

- GO100 관련 시스템 프롬프트는 실제로 2계층입니다.
  - AADS가 매 턴 주입하는 워크스페이스 시스템 프롬프트
  - GO100 서비스 내부 투자분석 프롬프트
- AADS 쪽 조립 경로는 로컬 코드로 확인했습니다.
- GO100 내부 프롬프트 원문은 이번 세션에서 `read_remote_file` 호출이 `user cancelled MCP tool call`로 실패해, 기존 로컬 아카이브를 근거로 확인했습니다.
- 클릭해서 바로 볼 전문 파일은 아래 2개입니다.

## 2. 전문 확인 파일

- 현재 매 턴 주입되는 시스템 컨텍스트 전문:
  [GO100_current_system_context_and_prompt_review_20260422_1212.md](/root/aads/aads-server/reports/GO100_current_system_context_and_prompt_review_20260422_1212.md:1)
- GO100 서비스 내부 프롬프트 전문:
  [GO100_system_prompt_full_text_and_improvement.md](/root/aads/aads-server/reports/GO100_system_prompt_full_text_and_improvement.md:1)

## 3. 실제 조립 경로

### 3-1. AADS 정적 프롬프트 템플릿

- 정적 텍스트 정의:
  [system_prompt_v2.py](/root/aads/aads-server/app/core/prompts/system_prompt_v2.py:51)
- GO100 역할 정의:
  [system_prompt_v2.py](/root/aads/aads-server/app/core/prompts/system_prompt_v2.py:89)
- GO100 capabilities 정의:
  [system_prompt_v2.py](/root/aads/aads-server/app/core/prompts/system_prompt_v2.py:176)
- 도구/규칙/응답가이드 정의:
  [system_prompt_v2.py](/root/aads/aads-server/app/core/prompts/system_prompt_v2.py:219)
  [system_prompt_v2.py](/root/aads/aads-server/app/core/prompts/system_prompt_v2.py:253)
  [system_prompt_v2.py](/root/aads/aads-server/app/core/prompts/system_prompt_v2.py:289)

### 3-2. 매 턴 동적 주입 조립기

- Layer 1 캐시 + 정적 프롬프트 로드:
  [context_builder.py](/root/aads/aads-server/app/services/context_builder.py:41)
- 현재 시각/대기/실행중 상태 주입:
  [context_builder.py](/root/aads/aads-server/app/services/context_builder.py:56)
- 메모리 레이어 주입:
  [context_builder.py](/root/aads/aads-server/app/services/context_builder.py:137)
- Workspace preload 주입:
  [context_builder.py](/root/aads/aads-server/app/services/context_builder.py:176)
- Auto-RAG 주입:
  [context_builder.py](/root/aads/aads-server/app/services/context_builder.py:157)
- 최근 아티팩트 주입:
  [context_builder.py](/root/aads/aads-server/app/services/context_builder.py:190)
- 최종 `system_prompt` 결합:
  [context_builder.py](/root/aads/aads-server/app/services/context_builder.py:325)

### 3-3. 동적 섹션별 데이터 소스

- `<workspace_preload>` 생성:
  [workspace_preloader.py](/root/aads/aads-server/app/services/workspace_preloader.py:18)
- 에러 패턴/최근 사실/마지막 세션 요약 소스:
  [workspace_preloader.py](/root/aads/aads-server/app/services/workspace_preloader.py:94)
  [workspace_preloader.py](/root/aads/aads-server/app/services/workspace_preloader.py:127)
  [workspace_preloader.py](/root/aads/aads-server/app/services/workspace_preloader.py:165)
- `<auto_rag_context>` 생성:
  [auto_rag.py](/root/aads/aads-server/app/services/auto_rag.py:25)

## 4. 현재 매 턴 주입 컨텍스트는 어떤 구조인가

AADS는 GO100 워크스페이스에 대해 아래 순서로 시스템 컨텍스트를 조립합니다.

1. `<currentTime>`
2. `<behavior_principles>`
3. `<role>`
4. `<ceo_communication_guide>`
5. `<capabilities>`
6. `<tools_available>`
7. `<rules>`
8. `<response_guidelines>`
9. `## 현재 상태 (동적)`
10. 메모리 회상 블록
11. `<workspace_preload>`
12. `<auto_rag_context>`
13. `<recent_artifacts>`
14. 진화 상태 및 도구 오류율 블록
15. 워크스페이스 추가 지시와 세션별 보강 문맥

현재 세션에 실제로 붙는 원문 전문은 위 2절의 파일에서 확인할 수 있습니다.

## 5. GO100 서비스 관점 개선안

### 5-1. 가장 시급한 문제

- AADS 상위 인텐트 체계와 GO100 내부 인텐트 체계가 분리되어 있습니다.
  AADS는 `stock_analysis/strategy_design/backtest/...` 계열이고, GO100 내부 프롬프트 아카이브는 `stock_info/goal_setup/market_briefing/...` 계열을 사용합니다.
- 상위 시스템 프롬프트는 “도구 사용/보고 규율”에 강하지만, GO100 내부 REPLY 프롬프트는 “출처 강제/수치 근거 표기”가 상대적으로 약합니다.
- `<workspace_preload>`와 `<auto_rag_context>`는 붙지만, GO100 내부 투자분석 프롬프트 쪽에서 이를 명시적으로 소비하는 규칙이 약합니다.
- 투자 서비스인데도 “보유 포지션, 목표, 레짐, 최근 백테스트 결과”가 모든 단계에서 공통 입력으로 강제되지 않습니다.

### 5-2. 권장 개선

1. 인텐트 사전 단일화
   AADS 상위 분류와 GO100 내부 분류를 1개 enum으로 합치고, 라우터와 프롬프트가 같은 값을 쓰게 정리해야 합니다.

2. REPLY에 출처 강제 규칙 추가
   종목, 수익률, 변동성, 뉴스 영향, 포트폴리오 판단을 답할 때 `DB 조회/백테스트/미측정` 중 하나를 문장 단위로 붙이게 해야 합니다.

3. 공통 분석 입력 블록 도입
   `conversation_summary`, `portfolio_snapshot`, `market_regime`, `goal_context`, `recent_backtests`를 UNDERSTAND/DESIGN/EVALUATE/OPTIMIZE/REPLY 전 단계에 공통 주입해야 합니다.

4. 프롬프트 장문 스펙 분리
   필터 목록, 전략 제약, 템플릿 예시 같은 장문 텍스트는 코드 상수나 메타데이터 테이블로 빼고, 프롬프트에는 참조 규칙만 남기는 편이 효율적입니다.

5. 실패 시 응답 프로토콜 고정
   데이터 부재, API 실패, 도구 실패, 백테스트 미실행의 4가지 경우를 분리해서 같은 형식으로 답하도록 고정해야 합니다.

### 5-3. 우선순위 제안

- P0: REPLY 출처 강제 + 인텐트 enum 통합
- P1: 공통 분석 입력 블록 도입
- P2: 프롬프트 장문 스펙 메타데이터화

## 6. 이번 확인에서의 제약

- `mcp__aads_tools__.read_remote_file(project=\"GO100\", file_path=\"go100/ai/prompts.py\")` 호출 결과:
  `user cancelled MCP tool call`
- 따라서 GO100 원격 최신 원문은 이번 세션에서 재실측하지 못했고, 로컬 아카이브 보고서와 AADS 조립 코드를 근거로 정리했습니다.

## 7. 바로 열어볼 파일

- 종합 인덱스:
  [GO100_prompt_context_audit_20260422_1307.md](/root/aads/aads-server/reports/GO100_prompt_context_audit_20260422_1307.md:1)
- 현재 시스템 컨텍스트 전문:
  [GO100_current_system_context_and_prompt_review_20260422_1212.md](/root/aads/aads-server/reports/GO100_current_system_context_and_prompt_review_20260422_1212.md:1)
- GO100 내부 프롬프트 전문:
  [GO100_system_prompt_full_text_and_improvement.md](/root/aads/aads-server/reports/GO100_system_prompt_full_text_and_improvement.md:1)
