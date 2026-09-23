# 오비서 이전 M1 PRD — 독립 실행 기반·설정 분리

| 항목 | 내용 |
|---|---|
| 문서 ID | OBYS-MIGRATION-JINAH-M1 |
| 상위 PRD | `docs/prd/20260923_OBYS_FULL_MIGRATION_JINAH_PRD.md` (정본 v1.2.0, goal_documents id=214) |
| 목표/마일스톤 | goal `df479771-f250-4a11-90a3-220432da2bfa` / M1 `859dbeb0-40b1-46f9-b72e-99ab9fff44b7` |
| 담당 역할 | Developer (owner_session_id 미배정) |
| 상태 | 초안 — M0 실사 기반 작성 |
| 작성 근거 | M0 기준선(커밋 `24e151b7`, `docs/reports/20260923_OBYS_M0_MIGRATION_BASELINE.md`, 미병합 브랜치 `obys-m0-baseline-20260923`) |

## 1. 목적과 범위
전용 앱 실행점(`app/yeoljeong_main.py`)이 실제로 AADS 없이 독립 기동·응답하도록 라우터·설정·DSN을 분리한다. M2(인증 이전)와 M4(전체 차단 검증) 완료 인증의 전제 조건이며, 이 단계 자체는 AADS 인증 완전 차단을 증명하지 않는다.

## 2. M0에서 물려받은 차단 요인
| # | 차단 요인 | 근거 |
|---|---|---|
| 1 | `YEOLJEONG_FINANCE_DATABASE_URL`이 빈 값이라 compose 기본값에 의해 `DATABASE_URL`=AADS DB로 주입됨. `OBYS_DATABASE_URL`만 별도 DSN을 가리켜 "역할별 명시 DSN" 미충족 | M0 2.5절 |
| 2 | 소스 SHA 미확정 — 운영 프로세스가 bind mount된 dirty 워킹트리를 실행 중이라 배포 이미지와 실행 코드 대응이 불명 | M0 3절 |
| 3 | API 공개 등록 116개 중 72개(`yeoljeong-finance`)만 이전 대상, 4개 prefix(`yeoljeong-dashboard` 등)는 aads-server 전용이라 제외 — 라우터 분리 시 누락 검증 필요 | M0 2.2절 |

## 3. 완료 기준 (DB 정본, milestones.completion_criteria)
전용 앱 라우터 METHOD+PATH 기준선과 격리 health 대조, 인증/업무/파일 설정 계약 분리 및 누락 fail-closed 테스트 통과. 인증 데이터·토큰 이전과 AADS 인증 의존 제거의 완료 인증은 M2, 전체 AADS 차단 검증은 M4에서 수행. 구현 기록만으로 완료 금지.

## 4. 작업 항목
1. `OBYS_DATABASE_URL`/`YEOLJEONG_FINANCE_DATABASE_URL` 이원화 해소 — 역할별 DSN 명시 주입으로 통일
2. 소스 SHA 고정 — dirty 워킹트리 실행 제거, 이미지 빌드 SHA와 실행 코드 일치 증명
3. `app/yeoljeong_main.py` 전용 라우터 METHOD+PATH 목록을 116개(72+13+11+10+10) 기준과 대조해 기준선 문서화
4. 설정 누락 시 fail-closed(부팅 실패) 테스트 추가

## 5. 완료 판정 방법
- 격리 환경에서 `yeoljeong_main:app` 단독 기동 → health 200
- 설정 키 강제 누락 시 기동 실패(예외) 확인
- 라우터 목록 diff 0건

## 6. 리스크
실 운영 컨테이너의 dirty 워킹트리 실행 상태를 먼저 해소하지 않으면 "독립 실행"의 기준 코드가 무엇인지 증명할 수 없다.

## 7. 기존 관련 작업 (거버넌스 주의 — 신규 발견)
브랜치 `obys-jinah-m1-20260923`(커밋 `4313e0c1`, **main 미병합**)에 `app/core/obys_runtime.py`, `app/obys_standalone.py`, systemd 서비스 정의(`deploy/obys/`), 관련 테스트가 이미 작성돼 있다. `owner_session_id`도 비어 있어 누가 이 작업을 이어받을지 확정되지 않았다. 이 문서는 그 구현을 대체하지 않고 존재만 기록하며, 병합·검수 여부는 CEO/CTO 결정 사항이다.
