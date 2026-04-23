# AADS 프로젝트별·역할별 세션/프롬프트/메모리 운영 체계 기획 실행 보고서

작성 시각: 2026-04-23 10:31 KST 실측  
작성 목적: AADS 채팅창에서 프로젝트별·역할별 세션을 안정적으로 생성하고, 모델별 시스템 프롬프트·도구·메모리 정책을 AI가 제안하되 사용자가 검토·수정·승인할 수 있는 운영 체계를 설계한다.

## 1. 요약

현재 AADS는 `system_prompt_v2.py` + `context_builder.py` + `memory_recall.py` 조합으로 꽤 진화한 상태입니다. 다만 실제 운영 단위는 아직 `워크스페이스 + base system_prompt` 중심이고, `역할`, `모델`, `도구 정책`, `메모리 정책`, `세션 생성 규칙`, `변경 승인 흐름`이 1급 엔터티로 분리돼 있지 않습니다.

따라서 다음 방향으로 재설계하는 것이 맞습니다.

1. `프로젝트`, `역할`, `모델`, `도구`, `메모리`, `세션 템플릿`을 각각 버전 관리 가능한 자산으로 분리한다.
2. 실제 채팅 세션은 이 자산들을 조합해 “컴파일된 시스템 프롬프트”로 생성한다.
3. AI는 새 역할/정책/프롬프트 개정안을 제안할 수 있지만, 운영 반영은 반드시 사용자 검토와 승인 후에만 한다.
4. 프롬프트 최적화는 감(感)으로 하지 말고, 버전·평가·비용·품질 지표를 남기는 운영 체계로 바꾼다.

## 2. 현재 상태 실측

### 2.1 코드 기준 현재 구조

- 정적 Layer 1은 `app/core/prompts/system_prompt_v2.py`에서 조립됩니다.
- 동적 컨텍스트는 `app/services/context_builder.py`가 `<currentTime>`, 런타임 상태, 메모리, preload, auto-rag, artifact, layer4를 붙여 만듭니다.
- 메모리 리콜은 `app/core/memory_recall.py`가 세션 요약, CEO 선호, 도구 전략, 활성 작업, 발견 사항, correction 지시 등을 조립합니다.
- 도구 전달은 `app/services/tool_registry.py`의 `_CORE_TOOLS`, `_INTENT_TOOL_MAP`, `get_tools_for_intent()`로 이미 1차 필터링이 들어가 있습니다.
- 실제 세션별 추가 지시는 `chat_workspaces.system_prompt`를 통해 `base_system_prompt`로 Layer 1 뒤에 붙습니다.
- 프롬프트 캐시는 `app/services/model_selector.py`의 `_build_system_with_cache()`에서 정적/준동적/동적 블록으로 분리합니다.

### 2.2 확인된 강점

- 프로젝트별 역할 분리와 인텐트별 경량 프롬프트가 이미 있습니다.
- Anthropic prompt caching을 고려한 구조가 존재합니다.
- 메모리 구조가 단순 대화 저장 수준을 넘어 correction/pattern/discovery까지 포함합니다.
- 인텐트별 도구 축소가 반영되어 “모든 도구를 매번 다 넣는 구조”에서 일부 벗어났습니다.

### 2.3 확인된 한계

- 워크스페이스는 있으나 “역할 프로필”이 독립 엔터티가 아닙니다.
- 모델별 프롬프트 정책은 실질적으로 라우터 레벨에 흩어져 있고 중앙 버전 관리가 없습니다.
- `chat_workspaces.system_prompt`는 존재하지만 버전, 승인 이력, diff, 롤백 체계가 약합니다.
- `prompt_templates` 테이블은 존재하지만, 운영 질문 템플릿용에 가깝고 시스템 프롬프트 거버넌스 용도는 아닙니다.
- 메모리는 강하지만 “working / factual / experiential / policy” 정책 구분과 TTL/승격/폐기 규칙이 아직 명확히 관리되지 않습니다.
- AI가 새 프로젝트/역할을 스스로 제안하고 사람이 검토하는 공식 Change Request 흐름이 없습니다.

## 3. 최신 자료 기반 설계 원칙

### 3.1 공통 원칙

- 시스템 프롬프트는 긴 한 덩어리 텍스트보다 “고정 규칙 + 가변 프로필 + 런타임 컨텍스트” 조합형 자산으로 관리해야 합니다.
- 프롬프트 최적화는 반드시 버전 관리와 평가를 동반해야 합니다.
- 긴 컨텍스트를 쓸 수 있다고 해서 모든 메모리를 매 턴 넣으면 품질이 오르지 않습니다. 작업 메모리와 장기 메모리를 분리해야 합니다.
- 도구는 많이 주는 것보다, 현재 작업에 필요한 최소 집합만 정확한 스키마와 설명으로 주는 편이 낫습니다.
- AI의 자기 진화는 “자동 반영”이 아니라 “자동 제안 + 인간 승인”이 기본이어야 합니다.

### 3.2 모델별 공식 권고에서 바로 가져와야 할 점

- Anthropic:
  - 정적 prefix를 앞에 두고 cache breakpoint를 둬야 비용과 TTFT가 줄어듭니다.
  - 도구 설명과 스키마는 구체적이어야 하며, tool set은 작을수록 유리합니다.
  - 프로젝트 메모리는 계층적으로 관리하는 편이 안정적입니다.
- OpenAI:
  - 프롬프트는 버전과 템플릿을 가진 재사용 자산으로 관리하는 방향이 공식화되어 있습니다.
  - reasoning 계열 모델은 지시를 짧고 직접적으로 주는 편이 좋고, 불필요한 chain-of-thought 유도는 피하는 것이 좋습니다.
  - 모델 변경 시 프롬프트 eval이 필수입니다.
- Google Gemini:
  - 공통 prefix는 context caching으로 비용을 줄일 수 있습니다.
  - function calling은 함수명/파라미터 설명의 품질이 결과 품질에 직접 영향을 줍니다.
  - stateless 호출 구조를 전제로 대화 상태를 직접 관리해야 합니다.
- MCP:
  - prompts/resources/tools를 명시적으로 노출하는 구조가 표준화되어 있어, 장기적으로 “프롬프트 자산”과 “도구 자산”을 AADS 내부 MCP로 노출하는 방식이 적합합니다.

### 3.3 최근 연구에서 반영할 점

- 최신 agent memory 연구는 메모리를 단순 RAG가 아니라 `factual / experiential / working`으로 구분할 것을 권장합니다.
- MemInsight류 연구는 과거 기록을 그대로 많이 넣는 것보다, 요약/정규화/재구성된 메모리가 더 효과적일 수 있음을 보여줍니다.
- 장기 대화 메모리 평가 연구는 긴 대화를 저장하는 것만으로는 부족하고, 세션 간 회상 정확도와 시간적 일관성을 별도 평가해야 함을 보여줍니다.

## 4. 목표 아키텍처

### 4.1 핵심 개념

운영 단위를 아래처럼 분리합니다.

- `Common Policy`: 전역 안전/응답/보안/운영 원칙
- `Project Profile`: AADS, KIS, GO100, SF, NTV2, NAS 등 프로젝트별 사실/책임/자원
- `Role Profile`: CTO, PM, Developer, QA, Researcher, Trader, COO 등 역할별 목표/언어/판단 기준
- `Model Profile`: Claude, GPT, Gemini, 로컬 모델별 지시 스타일/도구 방식/토큰 정책
- `Tool Policy`: 인텐트별 허용 도구, 필수 도구, 금지 도구, schema 힌트
- `Memory Policy`: 어떤 메모리를 언제 저장·승격·주입·폐기할지 규칙
- `Session Blueprint`: 세션 생성 템플릿. 어떤 프로젝트/역할/모델/정책 조합을 쓸지 선언
- `Compiled Prompt`: 실제 호출 시 위 자산을 합성해 만든 최종 system/developer prompt

### 4.2 권장 데이터 모델

신규 테이블 기준 제안:

- `project_profiles`
  - `project_key`, `name`, `description`, `facts_json`, `default_tools_policy_id`, `default_memory_policy_id`, `status`
- `role_profiles`
  - `role_key`, `name`, `goal`, `responsibilities`, `decision_style`, `report_style`, `allowed_actions`, `status`
- `model_profiles`
  - `model_key`, `provider`, `instruction_style`, `reasoning_mode`, `tool_mode`, `cache_strategy`, `max_context_policy`, `status`
- `prompt_assets`
  - `asset_key`, `asset_type(common/project/role/model/tool/memory/session)`, `title`, `status`
- `prompt_asset_versions`
  - `asset_id`, `version`, `content`, `format(xml/markdown/json)`, `created_by`, `approved_by`, `eval_score`, `is_current`
- `tool_policies`
  - `policy_key`, `intent_map_json`, `required_tools_json`, `forbidden_tools_json`, `notes`
- `memory_policies`
  - `policy_key`, `working_rules_json`, `episodic_rules_json`, `semantic_rules_json`, `policy_rules_json`, `ttl_rules_json`
- `session_blueprints`
  - `blueprint_key`, `project_key`, `role_key`, `model_key`, `tool_policy_id`, `memory_policy_id`, `default_prompt_asset_ids`
- `session_profile_bindings`
  - 특정 세션이 어떤 blueprint/version 조합으로 생성됐는지 기록
- `prompt_change_requests`
  - AI 또는 사용자가 제안한 변경안, diff, 근거, 영향 범위, 승인 상태
- `prompt_eval_runs`
  - 버전별 평가 점수, 비용, TTFT, tool success rate, human review 결과

### 4.3 세션 생성 흐름

1. 사용자가 프로젝트/역할을 고르거나, AI가 기존 맥락에서 추천합니다.
2. 시스템이 `session_blueprint`를 선택합니다.
3. 현재 모델 라우팅 결과에 따라 `model_profile`이 붙습니다.
4. `common + project + role + model + tool policy + memory policy + workspace override`를 합성합니다.
5. 컴파일 결과를 저장하고, 사용자는 관리자 화면에서 해당 세션이 어떤 버전 조합으로 생성됐는지 확인할 수 있습니다.

### 4.4 런타임 조립 원칙

- Layer 0: 모델별 instruction wrapper
- Layer 1: 공통 정책
- Layer 2: 프로젝트 프로필
- Layer 3: 역할 프로필
- Layer 4: 도구 정책
- Layer 5: 메모리 정책
- Layer 6: 워크스페이스/세션 오버라이드
- Layer 7: 런타임 상태, 현재 시간, 활성 작업, 선택 메모리, 사용자 입력

즉, 지금의 `system_prompt_v2.py`를 완전히 버리는 것이 아니라, “공통 Layer 자산”으로 축소하고 나머지를 DB/버전 자산으로 빼는 방향이 맞습니다.

## 5. 프로젝트별·역할별 운영 설계

### 5.1 프로젝트 축

프로젝트별로 아래만 별도 관리해야 합니다.

- 도메인 사실과 금지 사항
- 서버/DB/레포/배포 경로
- KPI와 실패 비용
- 자주 쓰는 도구/쿼리/체크리스트
- 프로젝트 특화 메모리 승격 규칙

예시:

- AADS: 프롬프트, 러너, 메모리, 라우터, 대시보드
- KIS: 실거래 위험도, 시장 시간, 주문/리스크 정책
- GO100: 투자분석 파이프라인, 가설 검증, 리포트 품질
- SF: 배치, 미디어 파이프라인, 외부 API quota
- NTV2: 마이그레이션/레거시 공존

### 5.2 역할 축

역할은 워크스페이스 이름 안에 섞어 넣지 말고 독립 프로필로 관리해야 합니다.

기본 권장 역할:

- `CEO-Orchestrator`
- `CTO-Orchestrator`
- `PM`
- `Developer`
- `QA`
- `Researcher`
- `DevOps`
- `Trader`
- `Analyst`
- `COO`

역할별로 달라져야 하는 항목:

- 목표 함수
- 허용 행동 범위
- 보고 형식
- 필요한 근거 강도
- 도구 우선순위
- 기억해야 할 상시 규칙

## 6. 모델별 시스템 프롬프트 최적화 전략

### 6.1 Anthropic 계열

권장:

- XML 섹션 유지
- 정적 prefix를 최대한 앞에 고정
- prompt caching breakpoint를 `공통 정책 / 프로젝트·역할 / 동적 메모리` 순으로 둠
- tool set은 인텐트별 최소 집합만 제공
- extended thinking은 복잡 인텐트에서만 활성화

주의:

- 장문 규칙 나열보다 우선순위가 분명한 규칙 계층이 낫습니다.
- 메모리를 많이 넣는 것보다 correction/policy 메모리를 우선 주입하는 편이 안전합니다.

### 6.2 OpenAI 계열

권장:

- `developer` 성격의 간결하고 직접적인 지시로 재작성
- 모델별 prompt asset version을 분리
- reasoning 모델에는 “생각을 길게 설명하라” 같은 지시를 줄이지 말고 목표와 제약만 명확히 기술
- prompt ID/버전/eval 관점의 관리 체계를 내부적으로 모방

주의:

- Anthropic용 XML 프롬프트를 그대로 복붙하면 장황해질 수 있습니다.
- model snapshot 교체 시 반드시 eval 묶음 실행이 필요합니다.

### 6.3 Gemini 계열

권장:

- routing/search/summarization/fact grounding 쪽에 우선 배치
- function declarations 설명을 더 엄격하게 작성
- 공통 prefix는 context caching 대상으로 분리

주의:

- stateless 전제라 세션 상태와 tool roundtrip을 애플리케이션이 더 엄격히 관리해야 합니다.

### 6.4 로컬/경량 모델

권장:

- 짧은 프롬프트
- 더 강한 schema 제약
- tool-first 설계
- 역할보다 출력 형식 중심 지시

## 7. 도구 운영 정책

### 7.1 원칙

- “모든 모델에 동일한 도구 풀”이 아니라 “모델별 공통 + 인텐트별 추가” 구조로 갑니다.
- 필수 도구와 선택 도구를 구분합니다.
- 도구 결과 없는 주장 금지 규칙은 유지합니다.

### 7.2 정책 계층

- 공통 필수: 시간 실측, 상태 조회, 파일 읽기, DB 조회
- 프로젝트 필수: 프로젝트별 주력 도구
- 역할 필수: 예를 들어 QA는 검증 도구 우선, DevOps는 배포 상태 도구 우선
- 모델 제한: 모델별 잘하는 도구 패턴 반영

### 7.3 관리자 기능

관리자에서 아래를 보여줘야 합니다.

- 인텐트별 노출 도구
- 최근 도구 성공률
- 평균 응답 비용
- tool selection drift
- 모델별 도구 사용 편차

## 8. 메모리 운영 정책

### 8.1 4계층 메모리로 명확히 분리

- `Working Memory`
  - 현재 세션에서만 필요한 상태
  - TTL 짧음
  - 매 턴 또는 수 분 단위 갱신
- `Episodic Memory`
  - 특정 작업/사건/실패/성공 경험
  - 요약 후 저장
  - retrieval 조건 엄격
- `Semantic Memory`
  - 프로젝트 사실, 규칙, 아키텍처, 상시 원칙
  - 자주 참조
  - 사람이 검토한 내용 위주
- `Policy Memory`
  - correction, 금지사항, CEO 선호, 보고 규칙
  - 최우선 주입
  - 삭제보다 supersede 중심 관리

### 8.2 메모리 쓰기 규칙

- 대화 원문 전체 저장과 별개로, 메모리는 “승격 조건”이 있어야 합니다.
- 승격 후보:
  - 반복 지시
  - 실패 후 교정
  - 프로젝트 구조 사실
  - 장기 선호
  - 운영 리스크
- 승격 금지:
  - 일회성 잡담
  - 감정적 표현
  - 미검증 추정
  - 현재 턴에만 유효한 임시 수치

### 8.3 메모리 평가

반드시 측정해야 할 지표:

- recall precision
- recall hit rate
- contradiction rate
- stale memory rate
- correction memory adoption rate

## 9. AI 자율 관리 + 사용자 검토 체계

### 9.1 자동 반영이 아니라 Change Request 체계

AI가 아래를 감지하면 `prompt_change_requests`를 생성합니다.

- 새 프로젝트 필요
- 새 역할 필요
- 현재 역할 설명이 실제 작업과 불일치
- 메모리 노이즈 누적
- 특정 모델에서 같은 프롬프트 실패 반복
- 도구 선택 오류율 상승

Change Request에는 다음이 포함되어야 합니다.

- 변경 이유
- 영향 범위
- 제안 diff
- 예상 이득/위험
- 관련 세션/평가 링크

### 9.2 사용자 승인 흐름

1. AI 제안 생성
2. 관리자 화면 diff 검토
3. 사용자가 수정 가능
4. 임시 스테이징 버전으로 eval 실행
5. 통과 시 current 승격
6. 실패 시 reject 또는 재수정

## 10. 관리자 UI 권장 메뉴

- `Prompt Assets`
  - 공통/프로젝트/역할/모델 자산 목록
- `Session Blueprints`
  - 어떤 조합으로 세션을 만들지 관리
- `Tool Policies`
  - 인텐트별 도구 정책 관리
- `Memory Policies`
  - 승격/주입/TTL 규칙 관리
- `Preview`
  - 특정 프로젝트+역할+모델 조합의 최종 컴파일 프롬프트 확인
- `Diff / Versions`
  - 버전 비교, 롤백
- `Evals`
  - 품질/비용/TTFT/tool success 비교
- `Change Requests`
  - AI 제안 검토/승인
- `Audit Log`
  - 누가 무엇을 언제 바꿨는지 기록

## 11. 단계별 실행 계획

### Phase 0. 인벤토리 정리

목표:

- 현재 `system_prompt_v2.py`, `chat_workspaces.system_prompt`, 메모리 테이블, 라우터/도구 정책의 실제 책임 경계를 문서화

산출물:

- 자산 분류표
- 중복 제거 대상 목록

예상:

- 1~2일

### Phase 1. 자산 스키마 도입

목표:

- `project_profiles`, `role_profiles`, `model_profiles`, `prompt_assets`, `prompt_asset_versions`, `session_blueprints` 도입

산출물:

- 마이그레이션
- CRUD API
- 기존 프롬프트를 새 자산으로 백필

예상:

- 3~5일

### Phase 2. Prompt Compiler 도입

목표:

- 현재 `build_layer1()` / `context_builder.build()` 앞단에 자산 조합 컴파일러 추가

산출물:

- `prompt_compiler.py`
- cache key 규칙
- compiled prompt provenance 기록

예상:

- 3~4일

### Phase 3. 관리자 UI

목표:

- 자산/버전/preview/diff/eval/change request UI 제공

산출물:

- Admin 화면
- 승인 플로우
- 롤백 기능

예상:

- 4~6일

### Phase 4. 메모리 정책 분리

목표:

- memory_recall을 `working / episodic / semantic / policy` 단위로 재구성

산출물:

- 메모리 policy 엔진
- retrieval scoring 개선
- stale/contradiction 관리

예상:

- 4~5일

### Phase 5. 평가 자동화

목표:

- 프롬프트 버전별 eval suite와 비용/도구 성공률 대시보드 구축

산출물:

- `prompt_eval_runs`
- golden task set
- 모델별 비교 리포트

예상:

- 3~5일

### Phase 6. AI 제안 자동화

목표:

- AI가 새 역할/정책/프롬프트 수정안을 제안하되 자동 반영 없이 CR 생성

산출물:

- proposal generator
- impact analyzer
- 승인 큐

예상:

- 4~6일

## 12. 우선순위

### P0

- 프롬프트 자산 분리 스키마 설계
- 세션 blueprint 개념 도입
- 관리자 preview/diff 화면

### P1

- 메모리 정책 분리
- 모델별 프롬프트 프로필화
- 평가 자동화

### P2

- AI change request 자동 제안
- 역할 진화 점수화
- MCP prompt/resource 노출

## 13. 바로 실행할 권장 작업

1. 현재 `system_prompt_v2.py` 내용을 공통/프로젝트/역할 자산으로 분해하는 인벤토리 작업부터 시작하십시오.
2. `chat_workspaces.system_prompt`는 유지하되, 앞으로는 직접 원본 저장이 아니라 “override asset”로 취급하십시오.
3. `prompt_templates`는 일반 실행 템플릿 전용으로 두고, 시스템 프롬프트 거버넌스용 테이블을 별도로 만드십시오.
4. 관리자에서 “최종 컴파일 프롬프트 미리보기”가 먼저 나와야 합니다. 이게 없으면 이후 수정 이력과 승인 흐름이 불투명합니다.
5. 메모리는 우선 `policy memory`를 최상단으로 분리하십시오. correction/금지사항이 묻히는 것을 먼저 막아야 합니다.

## 14. CEO 확인이 필요한 질문

- 역할 축의 기본 세트를 어디까지 둘지:
  - 최소 6개만 운영할지
  - 프로젝트별 특화 역할까지 넓힐지
- 프롬프트 변경 승인 권한을 CEO만 가질지, PM/CTO 워크스페이스에도 위임할지
- 새 프로젝트 생성 시 기본 세션 세트를 자동 생성할지
- 관리자에서 “AI 제안 자동 생성”을 기본 ON으로 둘지, 수동 트리거로 둘지

## 15. 참고 소스

현재 코드/문서:

- `app/core/prompts/system_prompt_v2.py`
- `app/services/context_builder.py`
- `app/core/memory_recall.py`
- `app/services/tool_registry.py`
- `app/services/model_selector.py`
- `docs/SYSTEM_PROMPT_OPTIMIZATION_REPORT.md`
- `docs/SYSTEM_PROMPT_ARCHITECTURE.md`
- `docs/MEMORY_EVOLUTION_ARCHITECTURE.md`
- `docs/chat/CHAT-DB-SCHEMA.md`
- `migrations/031_memory_upgrade.sql`
- `migrations/036_prompt_templates.sql`

외부 공식 자료 및 연구:

- Anthropic Prompt Caching: https://docs.anthropic.com/en/docs/build-with-claude/prompt-caching
- Anthropic Tool Use: https://docs.anthropic.com/en/docs/agents-and-tools/tool-use/implement-tool-use
- Anthropic Claude Code Memory: https://code.claude.com/docs/en/memory
- Anthropic Context Windows / Extended Thinking: https://docs.anthropic.com/en/docs/build-with-claude/context-windows
- Anthropic Evals: https://docs.anthropic.com/en/docs/build-with-claude/develop-tests
- OpenAI Prompting: https://developers.openai.com/api/docs/guides/prompting
- OpenAI Reasoning Best Practices: https://developers.openai.com/api/docs/guides/reasoning-best-practices
- Google Gemini Context Caching: https://ai.google.dev/gemini-api/docs/caching
- Google Gemini Function Calling: https://ai.google.dev/gemini-api/docs/function-calling
- MCP Architecture: https://modelcontextprotocol.io/specification/2025-06-18/architecture
- MCP Prompts: https://modelcontextprotocol.io/specification/2025-03-26/server/prompts
- Memory in the Age of AI Agents, arXiv 2512.13564: https://arxiv.org/abs/2512.13564
- MemInsight: Autonomous Memory Augmentation for LLM Agents, arXiv 2503.21760: https://arxiv.org/abs/2503.21760
- A Survey on the Memory Mechanism of Large Language Model based Agents, arXiv 2404.13501: https://arxiv.org/abs/2404.13501
- Evaluating Very Long-Term Conversational Memory of LLM Agents, arXiv 2402.17753: https://arxiv.org/abs/2402.17753

