# FLOW 프레임워크 규칙

## 4단계
1. Find(발견): 시장분석, 자료분석, 연구. 산출물: {PROJECT}-FIND-{SEQ}_{제목}.md
2. Lay out(설계): 기획서, 아키텍처. 산출물: {PROJECT}-LAYOUT-{SEQ}_{제목}.md
3. Operate(실행): 작업지시서. 산출물: {PROJECT}-{SEQ}_{제목}.md. parent 필드 필수.
4. Wrap up(마무리): 검증, 회고, 교훈. 산출물: {PROJECT}-WRAP-{SEQ}_{제목}.md

## 새 문서 파일명 (R-DOC 우선)
- 신규 문서의 파일명은 L1 규칙 R-DOC(`l1-doc-storage-rule`)를 따른다: `YYYYMMDD_{PROJECT}_{한글 제목}.md`, 화면 제목·첫 제목 한글, 영문 kebab-case document_key.
- 위 산출물 접두 형식은 기존 문서의 호환 표기다. 기존 파일명·document_key 는 바꾸지 않는다(일괄 개명 금지).

## Wrap up 의무 수준
- P0/P1: WRAP 파일 필수. 체크리스트 전항목. 미완료 시 다음 작업 차단.
- P2(15분 초과): 5분 모니터링 + HTTP 200 확인 필수.
- P2(15분 이하)/P3: claude_exec.sh 자동 health-check. 실패 시 WRAP 자동 생성.

## 작업 전
- _todo/ 에서 관련 TPP 확인. 있으면 /tpp 스킬로 이어서 진행.
- 오류 사전에서 알려진 원인 확인: `scripts/error_book.py match <오류파일|->`
  (2026-09-14 이관. 예전 `docs/shared-lessons/INDEX.md` 는 2026-03-06 에
  멈춘 뒤 반년간 아무도 갱신하지 않았다. 규칙이 죽은 색인을 가리키면
  규칙 자체가 무시된다 — 실제로 그렇게 됐다.)

## 작업 후
- 원인을 밝혔으면 오류 사전에 넣는다: `scripts/error_book.py promote` 또는
  `register`. 추측은 넣지 않고 확인한 것만 넣는다 (R-ERRBOOK).
- 결과 파일에 ## 교훈 섹션 작성 시 자동 등록됨
- 컨텍스트 부족 시 /handoff 스킬 실행
