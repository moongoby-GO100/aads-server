# GO100 시스템 프롬프트 랜딩 페이지

작성 시각: 2026-04-22 15:43:13 KST (실측)  
대상: GO100(백억이) 워크스페이스

## 바로 열어볼 문서

- 현재 매 턴 주입되는 시스템 컨텍스트 전문:
  [GO100_current_system_context_and_prompt_review_20260422_1212.md](/root/aads/aads-server/reports/GO100_current_system_context_and_prompt_review_20260422_1212.md:1)
- 현재 턴 시스템 컨텍스트 + 조립 경로 + 개선안 점검:
  [GO100_prompt_context_audit_20260422_1307.md](/root/aads/aads-server/reports/GO100_prompt_context_audit_20260422_1307.md:1)
- GO100 서비스 내부 투자분석 프롬프트 전문:
  [GO100_system_prompt_full_text_and_improvement.md](/root/aads/aads-server/reports/GO100_system_prompt_full_text_and_improvement.md:1)
- GO100 서비스 구조/운영 OS 관점 설계 보고:
  [GO100_P0_design_report_2026-04-20.md](/root/aads/aads-server/docs/reports/GO100_P0_design_report_2026-04-20.md:1)

## 실제 소스 위치

- GO100 역할 정의:
  [system_prompt_v2.py](/root/aads/aads-server/app/core/prompts/system_prompt_v2.py:89)
- GO100 capabilities 정의:
  [system_prompt_v2.py](/root/aads/aads-server/app/core/prompts/system_prompt_v2.py:176)
- Layer 1 조립:
  [system_prompt_v2.py](/root/aads/aads-server/app/core/prompts/system_prompt_v2.py:458)
- 매 턴 system prompt 결합:
  [context_builder.py](/root/aads/aads-server/app/services/context_builder.py:325)
- 메모리 레이어 조립:
  [memory_recall.py](/root/aads/aads-server/app/core/memory_recall.py:535)
- workspace preload 조립:
  [workspace_preloader.py](/root/aads/aads-server/app/services/workspace_preloader.py:18)
- 모델 identity 주입:
  [model_selector.py](/root/aads/aads-server/app/services/model_selector.py:472)

## 핵심 확인 결과

1. GO100 시스템 프롬프트는 2계층입니다.
   - AADS가 매 턴 주입하는 상위 워크스페이스 프롬프트
   - GO100 서비스 내부 투자분석 프롬프트
2. 매 턴 주입 컨텍스트는 `system_prompt_v2.py`의 정적 레이어에 `context_builder.py`가 동적 상태, 메모리, preload, auto-rag, 모델 identity를 붙이는 구조입니다.
3. 서비스 개선 우선순위는 다음 3가지입니다.
   - AADS 상위 인텐트와 GO100 내부 인텐트 체계 통합
   - REPLY 단계의 출처 강제
   - Goal/Portfolio/Regime/Backtest 공통 입력 블록 도입
