# FLOW 프레임워크 규칙

## 4단계
1. Find(발견): 시장분석, 자료분석, 연구. 산출물: {PROJECT}-FIND-{SEQ}_{제목}.md
2. Lay out(설계): 기획서, 아키텍처. 산출물: {PROJECT}-LAYOUT-{SEQ}_{제목}.md
3. Operate(실행): 작업지시서. 산출물: {PROJECT}-{SEQ}_{제목}.md. parent 필드 필수.
4. Wrap up(마무리): 검증, 회고, 교훈. 산출물: {PROJECT}-WRAP-{SEQ}_{제목}.md

## Wrap up 의무 수준
- P0/P1: WRAP 파일 필수. 체크리스트 전항목. 미완료 시 다음 작업 차단.
- P2(15분 초과): 5분 모니터링 + HTTP 200 확인 필수.
- P2(15분 이하)/P3: claude_exec.sh 자동 health-check. 실패 시 WRAP 자동 생성.

## 작업 전
- _todo/ 에서 관련 TPP 확인. 있으면 /tpp 스킬로 이어서 진행.
- docs/shared-lessons/INDEX.md에서 관련 교훈 확인.

## 작업 후
- 다른 프로젝트에도 적용 가능한 교훈 → shared/lessons/ 등록
- 결과 파일에 ## 교훈 섹션 작성 시 자동 등록됨
- 컨텍스트 부족 시 /handoff 스킬 실행
