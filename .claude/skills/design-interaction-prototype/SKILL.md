---
name: design-interaction-prototype
description: "새 화면·데이터 많은 화면에서 상태와 사용자 동작을 검증한다."
allowed-tools: Read, Grep, Glob
risk_tier: read
status: candidate
executor: model-instructions
---
# 화면 상호작용 설계

문서 키: `design-interaction-prototype`. 이 스킬은 검토·질문 지침이며 외부 쓰기 권한을 부여하지 않는다.

## 입력·산출물과 진행 규칙

입력: {screen, tasks, data_shape, device, constraints}.
산출물: {states: [{id, visible_content, actions}], transitions: [{from, action, to}], keyboard_flow, acceptance_cases}.
기본·로딩·빈값·오류·부분 성공 상태를 정의한다. 검색·필터·정렬·페이지 이동의 상태 보존과 취소·복구를 설계한다.
실패·질문 조건: 데이터 규모나 핵심 과업이 없으면 질문한다. 실행하지 않은 프로토타입을 검증 완료로 표시하지 않는다.

## 검증 fixture

입력: {screen: 주문 목록, tasks: [검색], data_shape: 10000행}
기대: 검색 로딩·0건·오류 상태, 필터 보존, 키보드 이동 사례.
입력: {screen: 목록, tasks: []}
기대: 핵심 과업 질문.

위 fixture는 기대 동작 명세다. 실제 모델 수행 검증·사용 증거와 사람 승인 전에는 active로 승격하지 않는다.
