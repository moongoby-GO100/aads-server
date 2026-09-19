# PRD — AAG 아키텍처 거버넌스 v1.1 Final

- 버전: 1.1 Final
- 문서 상태: 최종 통합본
- 작성: 2026-09-19 KST
- 적용 목표 ID: `40cfdfc5-06f9-4861-8dc9-27c8688cb3f7`
- 기획: [AAG 기획서](../plans/20260919_AAG_ARCHITECTURE_GOVERNANCE_기획서.md)
- 기술 설계: [AAG 설계서](../design/20260919_AAG_ARCHITECTURE_GOVERNANCE_설계서.md)
- Phase 0: [감사 보고서](../reports/20260919_AAG_V1_1_PHASE0_AUDIT.md)
- 데이터/API 계약: [v1.1 계약](../contracts/20260919_AAG_V1_1_DATA_API_CONTRACT.md)
- 요구사항 추적: [v1.0→v1.1 추적표](../contracts/20260919_AAG_V1_1_TRACEABILITY.md)

v1.0의 FR-001~FR-014, NFR-001~NFR-008, 사용자 시나리오, 상태·규칙 정책,
AO1~AO5, 수용 테스트, 운영 지표 및 완료 정의를 승계한다. 아래 v1.1 조항이
명시적으로 수정한 부분만 대체하며 충돌 시 v1.1을 우선한다.

## 1. 제품 정의

AAG는 프로젝트의 현재 아키텍처를 코드에서 추출하고, 설계 표류를 규칙으로 검출하며,
모든 채팅 세션과 러너가 작업 전에 영향 범위를 조회하도록 하는 내부 제품이다. GO100을
프로젝트 확장 첫 운영 대상으로 삼고, AADS 중앙 DB와 목표관리 화면을 공통 제어면으로 쓴다.

## 2. 문제와 목표

### 문제

- 코드 구조가 문서보다 빨리 변한다.
- HTTP smoke만으로 죽은 라우트, 중복 라우트, 테이블 계약 표류를 모두 찾을 수 없다.
- 리포트는 생성되지만 목표·마일스톤과 분리되면 실행 우선순위가 사라진다.
- 정적 분석 범위 밖 순수 전략 로직을 AAG가 안다고 오인할 위험이 있다.

### 목표

1. GO100 구조 스냅샷을 매시간 중앙에 보존한다.
2. 세션과 러너가 동일 정본으로 findings와 착수 브리프를 조회한다.
3. P0/P1 구조 부채를 목표 마일스톤과 검증 근거로 관리한다.
4. 기획·설계·PRD를 목표에서 즉시 열 수 있게 한다.
5. 정적 그래프의 범위와 한계를 결과에 명시한다.

## 3. 사용자 시나리오

| ID | 사용자 | 시나리오 | 성공 결과 |
|---|---|---|---|
| US-01 | CEO | 목표 화면에서 AAG의 이유·구조·요구사항을 확인 | 기획·설계·PRD 3개 링크가 보임 |
| US-02 | 개발 담당 | 작업 파일을 수정하기 전에 영향 범위 조회 | 관련 라우트·테이블·결함을 받음 |
| US-03 | ArchitectureOwner | 프로젝트의 P0/P1을 우선 처리 | 최신 시각과 근거가 있는 목록 확보 |
| US-04 | QA | 변경이 새 구조 부채를 만들었는지 검증 | baseline 대비 증가 시 실패 |
| US-05 | 운영 담당 | 스캔이 늦거나 적재가 실패한 원인 복구 | 마지막 성공과 다음 행동 확인 |
| US-06 | 전략카드담당 | #310 순수 로직의 구조 영향을 판정 | AAG 범위와 별도 호출 그래프를 함께 받음 |

## 4. 기능 요구사항

| ID | 요구사항 | 우선순위 | 수용 기준 |
|---|---|---|---|
| FR-001 | 프로젝트별 rules로 소스 구조를 추출한다 | P0 | GO100 graph에 node/edge/stats가 생성됨 |
| FR-002 | 중복·미마운트·경로·SQL 계약 결함을 severity와 함께 생성한다 | P0 | finding에 rule/severity/key/location/detail 존재 |
| FR-003 | 최신 스냅샷을 중앙 DB에 멱등 저장한다 | P0 | 같은 project/generated_at 재전송 시 중복 없음 |
| FR-004 | `aag_findings`가 project/rule/severity/path로 필터한다 | P0 | 응답에 source/generated_at/stale 포함 |
| FR-005 | `aag_brief`가 파일 또는 지시서의 관련 구조를 반환한다 | P0 | 관련 노드·결함 또는 미포함 사유 반환 |
| FR-006 | GO100 스캔을 매시간 실행하고 중복 실행을 막는다 | P0 | cron 실행과 `flock` 근거, 최신본 75분 이내 |
| FR-007 | 구조 부채 증가를 baseline gate로 차단한다 | P0 | 증가 시 non-zero, scan 불가는 별도 exit |
| FR-008 | 러너 작업 시작 전에 AAG 브리프를 주입한다 | P1 | job log에 brief 성공/skip 사유 기록 |
| FR-009 | 목표에 기획서·설계서·PRD를 연결한다 | P0 | 목표 상세 응답에 세 경로가 반환됨 |
| FR-010 | 스캔 실패와 결함 0건을 구분한다 | P0 | 실패가 clean 결과로 저장되지 않음 |
| FR-011 | 정적 그래프 밖 순수 로직을 표시한다 | P1 | brief에 범위 밖 상태와 보완 도구 안내 |
| FR-012 | 목표/마일스톤 증거에 snapshot 시각과 commit을 기록한다 | P1 | 완료 evidence에서 재현 가능 |
| FR-013 | 프로젝트 소유 경계를 분리한다 | P1 | 외부 프로젝트 finding이 GO100 실적에 자동 합산되지 않음 |
| FR-014 | 사용자가 최신 상태·오류·재시도 행동을 같은 맥락에서 본다 | P1 | 목표/운영 화면에 시각·상태·행동 표시 |

## 5. 비기능 요구사항

| ID | 요구사항 | 기준 |
|---|---|---|
| NFR-001 | 신선도 | GO100 정상 시 최신 스냅샷 75분 이내 |
| NFR-002 | 결정성 | 같은 SHA와 rules 입력은 동일 stable finding key 생성 |
| NFR-003 | 안전성 | 스캔은 운영 서비스·매매 프로세스를 재시작하지 않음 |
| NFR-004 | 보존성 | 실패한 스캔이 마지막 정상 스냅샷을 덮어쓰지 않음 |
| NFR-005 | 성능 | 정기 스캔은 다음 주기 전에 종료, 세션 조회는 최신 snapshot만 읽음 |
| NFR-006 | 보안 | 적재 자격증명과 민감값을 graph/log/document에 저장하지 않음 |
| NFR-007 | 추적성 | finding에서 프로젝트·스냅샷·파일·rule을 역추적 가능 |
| NFR-008 | 접근성 | 내부 식별자보다 업무 라벨을 우선하고 오류에 복구 행동 제공 |

## 6. 화면·상호작용 요구사항

목표 상세의 첫 화면에는 다음을 우선 표시한다.

1. 최신 스냅샷 시각과 stale 여부
2. P0/P1/P2 요약
3. 현재 진행 마일스톤과 승인 필요 항목
4. 기획서·설계서·PRD 링크
5. 마지막 오류와 `다시 스캔`, `영향 확인` 행동

스케줄, host, raw JSON, rules 편집은 Admin/Settings 또는 상세 운영 화면에 둔다.
모바일에서는 요약 카드와 재시도 버튼을 한 손으로 누를 수 있는 크기로 제공하고,
세션 재진입 뒤에도 마지막 프로젝트와 필터를 복원한다.

## 7. 상태 모델

| 상태 | 의미 | 다음 행동 |
|---|---|---|
| fresh | 최신 스냅샷이 SLO 이내 | findings/brief 사용 |
| stale | 마지막 성공은 있으나 SLO 초과 | refresh 실행 및 로그 확인 |
| scan_failed | 스캐너가 대상/파싱/검증 오류로 실패 | root/rules/fixture 교정 |
| ingest_failed | 로컬 생성 성공, 중앙 적재 실패 | 인증/API/DB 복구 후 재전송 |
| no_snapshot | 정상 스냅샷 없음 | 최초 스캔 실행 |
| clean | 정상 스캔 결과 P0/P1=0 | P2 triage 또는 다음 마일스톤 |

## 8. 규칙 운영 정책

- P0: 실행 경로가 잘못되거나 보안·거래·핵심 기능에 직접 영향을 주는 구조 결함
- P1: 데이터/계약 표류 또는 가까운 시점에 장애로 이어질 가능성이 높은 결함
- P2: 죽은 코드, 백업, 소유 불명 등 정리가 필요하지만 즉시 실행을 막지 않는 결함
- 예외는 reason, owner, expiry, evidence를 가져야 한다.
- baseline은 현재 부채를 고정하는 ratchet이며 새 부채 허용치가 아니다.
- severity 하향은 코드 근거와 리뷰 없이 할 수 없다.

## 9. 마일스톤과 출시 기준

| 마일스톤 | 상태 기준 | 출시/완료 게이트 |
|---|---|---|
| AO1 중앙 스냅샷 | DB와 API 가동 | GO100 snapshot 1건 이상 |
| AO2 GO100 자동화 | graph 생성과 brief 응답 | target 3종 샘플 매칭 |
| AO3 정기 스캔 | hourly + P0/P1 상환 | 최신 DB 기준 P0=0/P1=0 |
| AO4 #310 구조 판정 | 정적/호출 그래프 결합 | 근거·리스크·롤백·handover |
| AO5 운영 신뢰성 | 상태판·경보·재시도 | 실패 주입 복구와 freshness 경보 |

## 10. 수용 테스트

1. 목표 문서 API에서 plan 1건, PRD 1건, 기술 설계서 reference 1건이 반환된다.
2. 세 문서 경로가 실제 파일이며 상호 링크가 깨지지 않는다.
3. GO100 최신 snapshot의 `generated_at`과 findings 합계가 DB와 리포트에서 일치한다.
4. `aag_findings`의 severity/rule/path 필터가 예상 결과만 반환한다.
5. `aag_brief`가 라우터 파일, SQL 사용 파일, 그래프 밖 순수 로직을 구분한다.
6. scanner가 대상 0개일 때 exit 2이며 새 clean snapshot을 쓰지 않는다.
7. baseline보다 P0/P1이 1건 증가한 fixture는 게이트가 실패한다.
8. DB 적재 실패 중에도 마지막 정상 snapshot과 로컬 fallback 출처가 표시된다.
9. hourly 작업이 겹칠 때 한 프로세스만 실행된다.
10. AO4 판정이 목표 evidence와 DB handover 양쪽에서 조회된다.

## 11. 운영 지표

- snapshot age 분포와 SLO 초과 횟수
- scan 성공/실패/소요시간
- 프로젝트별 P0/P1/P2 추이
- finding 생성부터 해소까지의 시간
- brief 자동 주입 성공률과 skip 사유
- false-positive 예외 수와 만료 초과 수
- 목표 문서 누락 수

수치는 실제 DB·로그 측정값만 보고하며 추정 목표치는 별도 승인 전 확정값으로 쓰지 않는다.

## 12. 현재 기준선 (2026-09-19 15:00 KST 실측)

| 항목 | 값 | 근거 |
|---|---:|---|
| GO100 최신 생성 시각 | 2026-09-19 14:25:59 KST | `aag_graph_snapshots` |
| graph node / edge | 983 / 2,188 | `aag_graph_snapshots` |
| findings | 71 | `aag_graph_snapshots` |
| P0 / P1 | 0 / 0 | 최신 findings 집계 |
| P2 | ORPHAN_ROUTER 67, STALE_BACKUP 4 | 최신 findings 집계 |
| AO1 / AO2 / AO3 | completed | `milestones` |
| AO4 | pending | `milestones` |

## 13. 완료 정의

AAG 목표는 다음을 모두 충족해야 완료다.

- 기획·설계·PRD가 목표에 연결되고 실제 파일로 열림
- 정기 스캔과 중앙 적재가 freshness SLO를 충족
- 세션·러너·게이트가 같은 snapshot 계약을 사용
- P0/P1이 근거와 검증을 거쳐 0건
- AO4 #310 구조 판정과 DB handover 완료
- 실패 복구 시험과 운영 상태 확인 완료
- 관련 변경이 커밋·푸시되고, 배포가 필요한 경우 프로젝트 배포 계약과 모니터링을 통과

## 14. v1.1 추가 기능 요구사항

| ID | 요구사항 | 우선순위 |
|---|---|---:|
| FR-015 | 코드 작업 세션과 runner가 착수 전 authoritative AAG brief를 요청하고 결과 또는 승인된 skip을 기록한다 | P1 |
| FR-016 | 정적 graph와 보완 analyzer를 동일 commit 기준으로 연결한다 | P1 |
| FR-017 | snapshot별 스캔·미관측 범위와 위험 가중 coverage를 기록한다 | P1 |
| FR-018 | 구조 장애와 영향 오판 rollback에 미탐 사후 검토를 연결한다 | P1 |
| FR-019 | 신규 rule을 warn-only 관측 후 승인받아 enforce로 승격한다 | P1 |
| FR-020 | scanner/parser/rule 변경 시 골든 fixture와 결정성 회귀 검증을 수행한다 | P0 |
| FR-021 | baseline, exception, severity, rule lifecycle, override를 승인·감사한다 | P0 |
| FR-022 | 중앙 장애 override를 만료·승인·재검증 가능한 별도 경로로 관리한다 | P0 |
| FR-023 | v1/v2 API를 병행하고 소비자 전환·rollback 검증 후 v1을 폐기한다 | P0 |
| FR-024 | 문서 링크·유형·상호 참조·content hash 무결성을 CI에서 검사한다 | P1 |
| FR-025 | finding 최초·최근 관측, 해결, 재발과 snapshot별 occurrence를 관리한다 | P1 |
| FR-026 | 재스캔 권한, 중복, queue, 진행 상태와 결과를 관리한다 | P1 |
| FR-027 | 목표와 프로젝트의 명시적 연결에 따라 지표를 격리 집계한다 | P1 |
| FR-028 | backup, restore, latest pointer 재구축과 artifact 재처리를 지원한다 | P1 |
| FR-029 | 문서 표류를 검출하거나 승인된 제품 범위 밖으로 명시한다 | P1 |
| FR-030 | 두 번째 프로젝트를 같은 절차로 온보딩할 수 있다 | P1 |

## 15. v1.1 추가 비기능 요구사항

| ID | 요구사항 | 기준 |
|---|---|---|
| NFR-009 | 원자성 | partial snapshot을 ready/latest로 노출하지 않음 |
| NFR-010 | 호환성 | 전환 기간 v1 소비자가 계속 동작하고 사용량이 계측됨 |
| NFR-011 | 복구성 | 승인된 RPO/RTO와 실제 restore test를 충족 |
| NFR-012 | 무결성 | artifact·audit 변조를 차단하거나 탐지 |
| NFR-013 | 격리성 | 권한 없이 프로젝트 간 조회·적재·집계가 혼합되지 않음 |
| NFR-014 | 관측성 | 데이터 부재, evaluator 실패, 알림 실패를 정상과 구분 |

## 16. v1.1 완료 상태 계약

아래 상태는 각각 독립적으로 보고한다. 문서만 수정한 상태를 제품 완료로 표현하지 않는다.

- 문서 계약 완료
- DB migration 완료
- API v2 적용
- 소비자 전환 완료
- 기준선 재수립 완료
- coverage·미탐 거버넌스 적용
- 보안·감사·override 적용
- 복구 시험 완료
- v1 폐기 완료

Phase 0는 B(재현 불가)로 판정됐다. 따라서 기존 71건은
`legacy_unreproducible`로 보존하고 v1.1 기준선을 새로 수립해야 한다.
