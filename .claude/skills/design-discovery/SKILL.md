---
name: design-discovery
description: "신규 프로젝트·전면 개편에서 사용자 과업과 기존 근거를 조사한다."
allowed-tools: Read, Grep, Glob
risk_tier: read
status: candidate
version: 0.2.0
executor: model-instructions
---
# 디자인 맥락 탐색

문서 키: `design-discovery`. 이 스킬은 검토·질문 지침이며 외부 쓰기 권한을 부여하지 않는다.

## 입력·산출물과 진행 규칙

입력: {project_key, confirmed_brief, references, existing_screens}.
산출물: {tasks: [{actor, goal, evidence}], constraints, unknowns, hypotheses}.
확인된 브리프에서 핵심 과업을 뽑고 기존 화면과 연결한다. 참고 자료마다 출처와 적용 가능 범위를 기록한다.
가설을 사실과 구분하고 빈 근거를 지어내지 않는다. 스타일 선택은 design-art-direction으로 넘긴다.
실패·질문 조건: 브리프 미확인 또는 출처 접근 불가이면 필요한 자료를 질문하고 unknowns에 기록한다.

모든 조사 결론은 확인 브리프의 과업과 출처 위치에 연결한다. 사용자 사실·가설·미확인을 구분하고 출처끼리 충돌하면 CEO 확인 전 결론을 확정하지 않는다.

## 검증 fixture

입력: {confirmed_brief: 매일 업무 배정, existing_screens: [담당자 목록]}
기대: 배정 과업과 담당자 목록 근거 연결, 미확인 처리량은 unknowns.
입력: {confirmed_brief: null}
기대: 브리프 확인 요청, 조사 확정 결론 없음.

위 fixture는 기대 동작 명세다. 실제 모델 수행 검증·사용 증거와 사람 승인 전에는 active로 승격하지 않는다.

### 정상 fixture

입력: {confirmed_brief: true, references: 과업별 출처}
기대: 과업과 근거 연결.

### 누락 fixture

입력: {confirmed_brief: null}
기대: intake 확인 요청.

### 충돌 fixture

입력: {references: 서로 다른 사용자 주장}
기대: 가설 분리, 충돌 질문.

### 실패 fixture

입력: {references: 접근 실패}
기대: unknowns 기록, 조사 결론 중단.
