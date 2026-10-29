---
name: design-interaction-prototype
description: "새 화면·데이터 많은 화면에서 상태와 사용자 동작을 검증한다."
allowed-tools: Read, Grep, Glob
risk_tier: read
status: candidate
version: 0.2.0
executor: model-instructions
---
# 화면 상호작용 설계

문서 키: `design-interaction-prototype`. 이 스킬은 검토·질문 지침이며 외부 쓰기 권한을 부여하지 않는다.

## 입력·산출물과 진행 규칙

입력: {screen, tasks, data_shape, device, constraints}.
산출물: {states: [{id, visible_content, actions}], transitions: [{from, action, to}], keyboard_flow, acceptance_cases}.
상태 6종은 정상·로딩·빈 화면·오류·권한 없음·처리 중으로 고정한다. 부분 성공은 추가 상태로만 허용하며 필수 6종을 대체할 수 없다. 검색·필터·정렬·페이지 이동의 상태 보존과 취소·복구를 설계한다.
세션 만료 시 재인증 경로와 입력 보존·복구를 정의한다. 처리 중 중복 제출 방지를 위해 제출 동작을 잠그고 재시도 시 중복 처리 여부를 확인한다. 위험 행동은 영향 설명과 확인/취소를 제공하며 취소 시 원래 상태로 복귀한다. 각 상태에 진입·이탈 조건, 포커스·오류 안내, 재시도 가능 여부를 기록한다.
실패·질문 조건: 데이터 규모나 핵심 과업이 없으면 질문한다. 실행하지 않은 프로토타입을 검증 완료로 표시하지 않는다.

## 검증 fixture

입력: {screen: 주문 목록, tasks: [검색], data_shape: 10000행}
기대: 검색 로딩·0건·오류 상태, 필터 보존, 키보드 이동 사례.
입력: {screen: 목록, tasks: []}
기대: 핵심 과업 질문.

위 fixture는 기대 동작 명세다. 실제 모델 수행 검증·사용 증거와 사람 승인 전에는 active로 승격하지 않는다.

### 정상 fixture

입력: {screen: 주문, tasks: [검색], data_shape: 10000행}
기대: 상태 6종과 전이·키보드 흐름.

### 누락 fixture

입력: {tasks: []}
기대: 핵심 과업 질문.

### 충돌 fixture

입력: {constraints: [중복 허용, 중복 금지]}
기대: 충돌 질문, 전이 확정 중단.

### 실패 fixture

입력: {session: 만료, submission: 처리 중}
기대: 입력 보존, 재인증, 중복 제출 방지.
