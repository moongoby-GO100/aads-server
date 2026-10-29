---
name: design-intake-interview
description: "신규 프로젝트·화면·전면 개편에서 질문과 브리프 확인을 강제한다."
allowed-tools: Read, Grep, Glob
risk_tier: read
status: candidate
executor: model-instructions
---
# 디자인 요구 인터뷰

문서 키: `design-intake-interview`. 이 스킬은 검토·질문 지침이며 외부 쓰기 권한을 부여하지 않는다.

## 입력·산출물과 진행 규칙

입력: {project_key, situation, project_context, prior_answers: [{question, value, source}], brief_confirmed: bool}.
산출물: {questions: [{id, prompt, examples, recommended, reason}], answers: [{id, value, source}], brief, brief_confirmed, next_action}.
질문 단계 건너뛰기 금지. 정보가 충분해도 답을 확인하고 출처를 기록한다.
문항마다 프로젝트 기준 선택 예시 3개 이상을 제공한다. '기타(직접 입력)'와 '잘 모름—권장값'은 이 3개에 미포함이다.
선택 예시는 프로젝트 맥락으로 구체화하고 권장 선택지 1개와 이유를 표시한다.
한 번에 5~6문항, 최대 3회 왕복으로 진행한다. 1회 Q1~Q5, 2회 Q6~Q10, 3회 미확인 답과 브리프 확인으로 구성한다.
답변 출처는 CEO 선택 / 직접 입력 / 권장값 수락 / 추정값 확인 네 종류만 쓴다. 추정은 확인 전 확정 답으로 넣지 않는다.
질문 영역과 업무 대시보드 예시(각 영역에서 프로젝트에 맞춰 다시 작성):
- Q1 목적: 매출 추적 / 장애 대응 / 업무 배정. 권장: 업무 배정(반복 작업 단축).
- Q2 사용자: 운영 담당 / 팀장 / 외부 고객. 권장: 운영 담당(주 사용자).
- Q3 사용 상황·빈도: 매일 사무실 / 주간 모바일 / 긴급 현장. 권장: 매일 사무실(주 업무 흐름).
- Q4 핵심 과업·성공 기준: 처리 시간 / 오류 감소 / 완료 건수. 권장: 처리 시간(측정 가능).
- Q5 정보·데이터: 실시간 지표 / 목록·검색 / 상세 기록. 권장: 목록·검색(업무 진입점).
- Q6 브랜드·분위기: 신뢰·절제 / 친근·경쾌 / 고급·강조. 권장: 신뢰·절제(업무 집중).
- Q7 참고·기피: 기존 패턴 유지 / 경쟁사 흐름 참고 / 새 방향 탐색. 권장: 기존 패턴 유지(학습 비용).
- Q8 기기·접근성: 데스크톱 / 모바일 우선 / 키보드 우선. 권장: 키보드 우선(반복 입력).
- Q9 범위·제약: 단일 화면 / 공통 패턴 / 전체 시스템. 권장: 단일 화면(검증 범위 제한).
- Q10 우선순위·완료 판단: 과업 성공 / 시각 정합 / 성능 우선. 권장: 과업 성공(목적과 연결).
실패·질문 조건: 맥락 부족이면 질문부터 시작한다. 3회 후에도 모순이 남으면 unresolved로 정지한다.
브리프 확인 전 시안 생성 금지. 확인 답변을 받으면 next_action=art_direction으로 전환한다.

## 검증 fixture

입력: {project_key: ops, situation: S1, project_context: 업무 배정, prior_answers: [], brief_confirmed: false}
기대: Q1~Q5 5문항, 각 3개 이상의 업무 예시와 기타·잘 모름, 권장 1개·이유, next_action=ask, 시안 없음.
입력: {prior_answers: 10개 답변, brief_confirmed: false}
기대: 출처별 브리프 확인 요청, 시안 없음.
입력: {brief_confirmed: true, prior_answers: 확인된 10개 답변}
기대: next_action=art_direction, 추정 미확인 답변 없음.

위 fixture는 기대 동작 명세다. 실제 모델 수행 검증·사용 증거와 사람 승인 전에는 active로 승격하지 않는다.
