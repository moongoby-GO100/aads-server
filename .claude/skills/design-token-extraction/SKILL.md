---
name: design-token-extraction
description: "외부 시안·기존 코드에서 근거가 있는 토큰 후보를 추출한다."
allowed-tools: Read, Grep, Glob
risk_tier: read
status: candidate
version: 0.2.0
executor: model-instructions
---
# 디자인 토큰 추출

문서 키: `design-token-extraction`. 이 스킬은 검토·질문 지침이며 외부 쓰기 권한을 부여하지 않는다.

## 입력·산출물과 진행 규칙

입력: {artifacts, existing_tokens, mode, provenance}.
산출물: {tokens: [{name, value, unit, evidence, confidence}], aliases, conflicts, unknowns}.
색·서체·간격·반경을 읽고 원본 위치를 증거로 남긴다. 기존 의미 토큰을 우선 대응하고 픽셀 관측값과 추정값을 구분한다.
실패·질문 조건: 이미지에서 정확한 값 계산이 불가능하면 unknowns로 남기고 코드·원본을 요청한다. 추정값을 확정 토큰으로 발행하지 않는다.

의미 역할·원본 위치·단위·추출 방법·신뢰도를 함께 기록한다. 기존 의미 토큰과 충돌하면 자동 교체하지 않고 질문한다. 승인 전 코드나 공용 토큰에 적용하지 않는다.

## 검증 fixture

입력: {artifacts: ["button {border-radius: 8px}"], existing_tokens: {radius_button: 8px}}
기대: 8px 근거와 기존 radius_button 별칭, 중복 토큰 없음.
입력: {artifacts: [흐린 스크린샷]}
기대: 정확한 색 unknowns, 원본 요청.

위 fixture는 기대 동작 명세다. 실제 모델 수행 검증·사용 증거와 사람 승인 전에는 active로 승격하지 않는다.

### 정상 fixture

입력: {artifacts: radius 8px 코드, existing_tokens: radius_button 8px}
기대: 근거와 별칭, 중복 없음.

### 누락 fixture

입력: {provenance: null}
기대: 출처 요청, 확정 중단.

### 충돌 fixture

입력: {existing_tokens: radius_button 4px, artifacts: 8px}
기대: 충돌 기록, 교체 승인 질문.

### 실패 fixture

입력: {artifacts: 흐린 이미지}
기대: unknowns, 원본 요청.
