---
name: design-art-direction
description: "신규 프로젝트·전면 개편에서 확인된 요구를 시각 방향 후보로 바꾼다."
allowed-tools: Read, Grep, Glob
risk_tier: read
status: candidate
version: 0.2.0
executor: model-instructions
---
# 디자인 표현 방향

문서 키: `design-art-direction`. 이 스킬은 검토·질문 지침이며 외부 쓰기 권한을 부여하지 않는다.

## 입력·산출물과 진행 규칙

입력: {confirmed_brief, discovery, brand_tokens, constraints}.
산출물: {directions: [{name, typography, color_roles, spacing, rationale, risks}], recommended, confirmation}.
요구와 연결된 세 방향을 비교하고 기존 토큰으로 구현 가능한지 표시한다. 색 이름만 나열하지 말고 정보 위계·밀도·타이포 역할을 설명한다.
실패·질문 조건: 브리프 미확인이면 인터뷰로 돌린다. 브랜드 제약이 충돌하면 선택을 질문한다. 선택 전 한 방향을 확정하지 않는다.

각 방향은 사용자 과업·정보 위계·밀도·가독성·브랜드 제약과 연결하고 고르면 달라지는 것과 포기하는 것을 설명한다. 권장안도 CEO 선택 전 확정하지 않으며 브리프 확인 전 시안 생성 금지.

## 검증 fixture

입력: {confirmed_brief: 업무 집중, brand_tokens: 기존 파랑}
기대: 세 방향, 기존 파랑 역할 유지 여부와 추천 이유.
입력: {confirmed_brief: false}
기대: 인터뷰 요청, 시안 없음.

위 fixture는 기대 동작 명세다. 실제 모델 수행 검증·사용 증거와 사람 승인 전에는 active로 승격하지 않는다.

### 정상 fixture

입력: {confirmed_brief: true, constraints: 확인됨}
기대: 3방향 비교, 권장 이유와 선택 요청.

### 누락 fixture

입력: {confirmed_brief: false}
기대: intake 반환, 시안 0개.

### 충돌 fixture

입력: {brand_tokens: 파랑 유지, constraints: 파랑 금지}
기대: 충돌 질문, 확정 중단.

### 실패 fixture

입력: {brand_tokens: 읽기 실패}
기대: 근거 요청, 검증 완료 주장 금지.
