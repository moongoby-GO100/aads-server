---
name: design-critique
description: "전면 개편·데이터 화면·반려 피드백에서 근거와 우선순위를 정한다."
allowed-tools: Read, Grep, Glob
risk_tier: read
status: candidate
version: 0.2.0
executor: model-instructions
---
# 디자인 비평과 재작업

문서 키: `design-critique`. 이 스킬은 검토·질문 지침이며 외부 쓰기 권한을 부여하지 않는다.

## 입력·산출물과 진행 규칙

입력: {brief, screens, feedback, acceptance_criteria}.
산출물: {findings: [{evidence, criterion, severity, suggestion}], priorities, unresolved, recheck_cases}.
취향과 과업 실패를 구분한다. 각 반려 의견을 기존 기준과 연결하고 서로 충돌하면 드러낸다.
높은 심각도부터 수정 제안을 정하고 재확인 사례를 남긴다. 문서 합의는 document-coauthoring으로 넘긴다.
실패·질문 조건: 기준이 없거나 피드백이 모순이면 결정을 질문한다. 계산 없는 접근성 통과 판정은 하지 않는다.

각 지적을 승인 기준·판본 hash·화면 위치·사용자 영향·심각도·재검증 조건에 연결한다. 취향은 제안으로 분리하고 충돌한 피드백은 승인자에게 질문한다. 근거 없는 점수·자동 승인·인계 금지.

## 검증 fixture

입력: {brief: 빠른 검색, screens: [검색 없는 목록], feedback: 찾기 어려움}
기대: 과업 근거가 있는 검색 누락 finding과 재확인 사례.
입력: {feedback: [밀도 높여라, 더 여백 줘라], acceptance_criteria: []}
기대: 충돌 unresolved와 우선순위 질문.

위 fixture는 기대 동작 명세다. 실제 모델 수행 검증·사용 증거와 사람 승인 전에는 active로 승격하지 않는다.

### 정상 fixture

입력: {brief: 빠른 검색, screens: 검색 없는 목록, acceptance_criteria: 검색 가능}
기대: 근거·심각도·재확인 사례.

### 누락 fixture

입력: {acceptance_criteria: []}
기대: 기준 질문, 확정 판정 중단.

### 충돌 fixture

입력: {feedback: [밀도 높임, 여백 확대]}
기대: unresolved, 우선순위 질문.

### 실패 fixture

입력: {screens: 접근 실패}
기대: 평가 중단, 화면 요청.
