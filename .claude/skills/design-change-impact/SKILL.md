---
name: design-change-impact
description: "화면 수정·운영 어긋남에서 변경 범위와 회귀 위험을 정한다."
allowed-tools: Read, Grep, Glob
risk_tier: read
status: candidate
executor: model-instructions
---
# 디자인 변경 영향 분석

문서 키: `design-change-impact`. 이 스킬은 검토·질문 지침이며 외부 쓰기 권한을 부여하지 않는다.

## 입력·산출물과 진행 규칙

입력: {project_key, screen, request, existing_patterns, constraints}.
산출물: {impact_level, request_type, scope_answers, alternatives: [{intensity, before, after, affected, risks}], regression_checklist}.
영향 범위는 국소(한 요소·화면), 패턴(반복 컴포넌트), 시스템(토큰·전체 흐름) 3단계로 분류한다.
기존 design_modification_requests request_type 14종 중 하나만 사용한다:
spacing, spacing_density, visual_hierarchy, color, color_brand, typography, component, component_consistency, responsive, interaction, content_clarity, workflow_layout, flow, other.
여러 유형이 겹치면 주 유형과 이유를 기록하고 범위를 확장하기 전에 질문한다.
범위 질문 최소 3문항: 적용 범위는 어디까지인가? 바꾸지 말 것은 무엇인가? 완료 판단은 무엇인가?
보수/중간/적극 전후 3안을 모두 제시하고 각 안의 영향·위험을 비교한다. 답변 전 전체 시스템 변경을 확정하지 않는다.
회귀 점검표에 기존 화면, 공유 컴포넌트, 반응형, 키보드·포커스, 데이터 상태, 토큰 영향과 확인 방법을 포함한다.
실패·질문 조건: 범위 답변 누락·보존 조건 충돌은 질문하고 미확정으로 남긴다. API나 승인 상태를 변경하지 않는다.

## 검증 fixture

입력: {screen: 주문, request: 버튼 간격 확대, scope_answers: null}
기대: spacing 분류, 범위 3문항, 국소 잠정 판정, 보수/중간/적극 전후 3안, 회귀 점검표.
입력: {request: 전체 브랜드 색 교체, constraints: 브랜드 색 유지}
기대: 시스템 영향·color_brand, 충돌 질문, 변경 확정 없음.

위 fixture는 기대 동작 명세다. 실제 모델 수행 검증·사용 증거와 사람 승인 전에는 active로 승격하지 않는다.
