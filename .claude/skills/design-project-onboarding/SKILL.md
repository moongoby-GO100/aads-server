---
name: design-project-onboarding
description: "신규 프로젝트에서 기존 자산과 담당자·승인 경계를 확인한다."
allowed-tools: Read, Grep, Glob
risk_tier: read
status: candidate
executor: model-instructions
---
# 디자인 프로젝트 시작 점검

문서 키: `design-project-onboarding`. 이 스킬은 검토·질문 지침이며 외부 쓰기 권한을 부여하지 않는다.

## 입력·산출물과 진행 규칙

입력: {project_key, owners, existing_assets, constraints, confirmed_brief}.
산출물: {inventory, ownership, review_steps, missing_inputs, ready}.
프로젝트 식별자와 담당자, 브리프, 기존 디자인 체계를 확인한다. 기존 자산 재사용 가능성을 표시하고 검토 담당자와 순서를 제안한다.
실패·질문 조건: 프로젝트·담당자·확인 브리프가 없으면 ready=false와 질문을 반환한다. 자동 승인하거나 프로젝트를 생성하지 않는다.

## 검증 fixture

입력: {project_key: ops, owners: [CEO], existing_assets: [공통 버튼], confirmed_brief: true}
기대: 공통 버튼 재사용과 CEO 검토 단계, ready=true.
입력: {project_key: ops, owners: []}
기대: ready=false, 담당자 질문.

위 fixture는 기대 동작 명세다. 실제 모델 수행 검증·사용 증거와 사람 승인 전에는 active로 승격하지 않는다.
