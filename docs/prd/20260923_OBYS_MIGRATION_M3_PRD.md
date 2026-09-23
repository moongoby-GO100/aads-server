# 오비서 이전 M3 PRD — 업무 DB·파일·자동수집 이전 검증

| 항목 | 내용 |
|---|---|
| 문서 ID | OBYS-MIGRATION-JINAH-M3 |
| 상위 PRD | `docs/prd/20260923_OBYS_FULL_MIGRATION_JINAH_PRD.md` (정본 v1.2.0, goal_documents id=214) |
| 목표/마일스톤 | goal `df479771-f250-4a11-90a3-220432da2bfa` / M3 `1c22b882-abf4-4744-943f-8df8ea0b7278` |
| 담당 역할 | Developer (owner_session_id 미배정) |
| 상태 | 초안 |
| 작성 근거 | M0 기준선(커밋 `24e151b7`, `docs/reports/20260923_OBYS_M0_MIGRATION_BASELINE.md`) |

## 1. 목적과 범위
업무 DB(`obys`, 39MB·40테이블)·파일(952개·62.8MiB)·자동수집 워커를 이전하고 동등성을 검증한다.

## 2. M0에서 물려받은 차단 요인
| # | 차단 요인 | 근거 |
|---|---|---|
| 1 | `delivery_sales.json` 등 5개 파일이 동일 사실을 `yeoljeong_delivery_*` 테이블과 이중 보관 — 정본 미확정 | M0 2.4절 |
| 2 | `.bak_*` 백업 파일 포함 여부 미결정 — 제외 시 manifest digest(`c28c59d7…`)가 바뀜 | M0 2.4절 |
| 3 | 자동수집 워커가 별도 컨테이너 없이 API 프로세스 내부에서 파일 lock(`.bank_auto_collect.lock`)으로만 배타 제어 — 두 호스트 병행 구간에서 상호배제 실패 | M0 2.6절 |
| 4 | 기존 결함(이전과 무관, baseline으로 분리): 신한은행 수집이 `PC_AGENT_LOGIN_REQUIRED`/`LOGIN_SUCCESS_NOT_OBSERVED`로 이미 실패 중 | M0 2.6절 |

## 3. 완료 기준 (DB 정본)
동일 필터 건수·금액·PK/FK·파일 해시 불일치 0, 중복 적재 0, 신규 저장→재조회 일치, 승인된 쓰기 역복귀 리허설. 수치는 목표 기준.

## 4. 작업 항목
1. JSON vs DB 정본 확정(권장안: DB 정본화 후 JSON은 파생 캐시로 재정의)
2. `.bak_*` 이전 포함 여부 결정 및 manifest 재계산
3. 자동수집 워커를 파일 lock에서 분산 lock(예: DB advisory lock)으로 교체
4. 신한은행 수집 결함은 별도 이슈로 분리 트래킹(이 마일스톤 완료 기준에서 제외)

## 5. 완료 판정 방법
해시 manifest 재비교 불일치 0, 필터별 건수·금액 대조 일치, 승인된 쓰기 역복귀 리허설 성공.

## 6. 리스크
파일 lock 기반 배타 제어를 코드 변경 없이 두 호스트로 병행하면 자동수집 중복 실행이 발생할 수 있다.
