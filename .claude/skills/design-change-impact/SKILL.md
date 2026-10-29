---
name: design-change-impact
description: "화면 수정·운영 어긋남에서 변경 범위와 회귀 위험을 정한다."
allowed-tools: Read, Grep, Glob
risk_tier: read
status: candidate
version: 0.2.0
executor: model-instructions
---
# 디자인 변경 영향 분석

문서 키: `design-change-impact`. 이 스킬은 검토·질문 지침이며 외부 쓰기 권한을 부여하지 않는다.

## 입력·산출물과 진행 규칙

입력: {project_key, screen, request, existing_patterns, constraints, intake_answers, brief_confirmed, baseline_revision, baseline_hash, before_hash, after_hash, approved}.
산출물: {impact_level, request_type, scope_answers, alternatives: [{intensity, before, after, affected, risks}], regression_checklist, baseline_revision, baseline_hash, before_hash, after_hash, allowed_scope, forbidden_scope, acceptance_criteria}.
영향 범위는 국소(한 요소·화면), 패턴(반복 컴포넌트), 시스템(토큰·전체 흐름) 3단계로 분류한다.
기존 design_modification_requests request_type 14종 중 하나만 사용한다:
spacing, spacing_density, visual_hierarchy, color, color_brand, typography, component, component_consistency, responsive, interaction, content_clarity, workflow_layout, flow, other.
여러 유형이 겹치면 주 유형과 이유를 기록하고 범위를 확장하기 전에 질문한다.
질문은 소유하지 않는다. design-intake-interview 답변 기록(범위 질문 모드)을 참조한다. 적용 범위·보존 조건·완료 판단이 없으면 intake로 반환한다.
브리프 확인 후 보수/중간/적극 전후 3안을 모두 제시하고 각 안의 영향·위험을 비교한다. 답변 전 전체 시스템 변경을 확정하지 않는다.
산출물은 기준 승인본 revision/hash(baseline_revision/baseline_hash), 전후 판본 hash(before_hash/after_hash)를 기록하고 각 변경을 allowed_scope/forbidden_scope/acceptance_criteria와 연결한다. 기준 승인본 또는 hash 누락·불일치면 중단하고 기준을 확인한다. 브리프 확인 전 시안 생성 금지. 승인 전 인계 금지: approved=true 및 승인 대상 hash 일치 전 구현 담당자에게 넘기지 않는다.
회귀 점검표에 기존 화면, 공유 컴포넌트, 반응형, 키보드·포커스, 데이터 상태, 토큰 영향과 확인 방법을 포함한다.
실패·질문 조건: 범위 답변 누락·보존 조건 충돌은 질문하고 미확정으로 남긴다. API나 승인 상태를 변경하지 않는다.

## 검증 fixture

입력: {screen: 주문, request: 버튼 간격 확대, scope_answers: null}
기대: design-intake-interview 범위 질문 모드로 반환, next_action=ask, 시안 0개, 인계 중단.
입력: {request: 전체 브랜드 색 교체, constraints: 브랜드 색 유지}
기대: 시스템 영향·color_brand, 충돌 질문, 변경 확정 없음.

위 fixture는 기대 동작 명세다. 실제 모델 수행 검증·사용 증거와 사람 승인 전에는 active로 승격하지 않는다.

### 정상 fixture

입력: {scope_answers: 확인됨, brief_confirmed: true, baseline_revision: r1, baseline_hash: h1, before_hash: h1, after_hash: h2, approved: false}
기대: 3안과 범위·회귀 연결, 승인 전 인계 금지.

### 누락 fixture

입력: {scope_answers: null}
기대: intake 범위 질문 모드 반환, 시안 0개.

### 충돌 fixture

입력: {allowed_scope: 색 변경, forbidden_scope: 브랜드 색 유지}
기대: 충돌 질문, 시안·인계 중단.

### 실패 fixture

입력: {baseline_hash: 승인본과 불일치}
기대: 기준 판본 확인 요청, 인계 중단.
