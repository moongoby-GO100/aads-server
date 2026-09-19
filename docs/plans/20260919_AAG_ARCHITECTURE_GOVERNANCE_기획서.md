# AAG 아키텍처 거버넌스 — 기획서 v1.1 Final

- 작성: 2026-09-19 KST
- 버전: 1.1 Final
- 상태: 최종 통합본(Phase 0 판정 반영)
- 적용 목표: `GO100 아키텍처 거버넌스 — AAG 그래프 가동 및 #310 파동 경로 구조 판정`
- 목표 ID: `40cfdfc5-06f9-4861-8dc9-27c8688cb3f7`
- 후속 문서: [설계서](../design/20260919_AAG_ARCHITECTURE_GOVERNANCE_설계서.md), [PRD](../prd/20260919_AAG_ARCHITECTURE_GOVERNANCE_PRD.md)
- 보완 계약: [v1.1 데이터·API 계약](../contracts/20260919_AAG_V1_1_DATA_API_CONTRACT.md)
- 감사 근거: [Phase 0 감사 보고서](../reports/20260919_AAG_V1_1_PHASE0_AUDIT.md)
- 추적표: [v1.0→v1.1 요구사항 추적표](../contracts/20260919_AAG_V1_1_TRACEABILITY.md)

## 1. 한 줄 기획

AAG(Architecture Awareness/Guard Graph)는 코드 변경 전에 영향 범위를 보여주고,
변경 뒤에는 구조 표류를 자동 검출하며, 발견된 결함을 목표·마일스톤·검증 근거로
이어 주는 아키텍처 거버넌스 체계다.

## 2. 해결하려는 문제

GO100은 라우터, 서비스, SQL 테이블, 전략 로직이 여러 디렉터리에 분산되어 있다.
사람이나 에이전트가 파일 하나만 보고 수정하면 다음 문제가 반복된다.

1. 같은 경로가 두 번 등록되어 뒤의 구현이 실제로는 호출되지 않는다.
2. 화면 호출과 백엔드 라우트가 어긋나도 인증 응답 때문에 HTTP 점검만으로는 놓친다.
3. SQL 문자열과 모델 선언이 표류해 런타임 전까지 계약 위반이 드러나지 않는다.
4. 생성된 리포트가 목표와 분리되어 다음 담당이 기획 의도와 완료 기준을 찾지 못한다.
5. 스캔 시점이 오래되거나 실패했는데도 결함 0건으로 오인할 수 있다.

AAG의 목적은 단순히 결함 목록을 만드는 것이 아니다. **착수 전 인지 → 변경 차단 →
정기 재검사 → 목표 근거 보존**까지 한 흐름으로 만드는 것이 핵심이다.

## 3. 대상 사용자

| 사용자 | 필요한 것 | AAG가 제공할 것 |
|---|---|---|
| CEO | 목표별 위험과 완료 근거를 한 화면에서 확인 | 목표에 기획·설계·PRD·최신 findings 연결 |
| ArchitectureOwner | 프로젝트 구조 부채를 우선순위별 관리 | 스냅샷, severity/rule/path 필터, baseline |
| 개발 담당/러너 | 수정 전에 영향 파일·라우트·테이블 파악 | `aag_brief(project, target)` |
| QA/리뷰어 | 변경이 구조 부채를 늘렸는지 판정 | baseline ratchet과 재스캔 결과 |
| 운영 담당 | 스캔·적재 지연과 실패 복구 | freshness, 실행 로그, 마지막 정상 스냅샷 |

## 4. 사용자 흐름

### 4.1 첫 진입

1. 목표 화면에서 AAG 기획서·설계서·PRD를 연다.
2. 현재 마일스톤과 최신 스냅샷 시각, P0/P1/P2 건수를 확인한다.
3. 작업 대상 파일 또는 지시서를 `aag_brief`에 넣어 관련 구조를 확인한다.
4. 발견된 P0/P1은 목표의 다음 마일스톤 또는 작업 지시로 전환한다.

설정·스케줄러·인증은 운영 보조 경로로 분리한다. 사용자의 첫 화면은 설정이 아니라
**현재 위험, 승인 필요 항목, 다음 행동**이어야 한다.

### 4.2 반복 사용

1. GO100 스캔은 매시간 생성된다.
2. 작업 착수 때 러너가 AAG 브리프를 자동 주입한다.
3. 구조 관련 커밋은 baseline보다 결함이 증가하면 차단된다.
4. 완료 보고에는 스냅샷 시각, 변경 전후 건수, 테스트·배포 근거를 남긴다.
5. 목표 화면에서 문서와 현재 findings를 다시 확인한다.

### 4.3 실패 복구

| 실패 | 사용자에게 보여줄 상태 | 복구 행동 |
|---|---|---|
| 스냅샷 지연 | 마지막 성공 시각과 `stale` 경고 | 수동 refresh 후 DB 적재 재시도 |
| 스캔 불가 | 결함 0건이 아니라 `scan_failed` | 대상 root/rules/권한 확인 |
| DB 적재 실패 | 로컬 그래프 사용 중임을 표시 | 적재 인증·API 상태 확인 후 재전송 |
| 오탐 | rule, 근거, 예외 만료일 표시 | 파서 보강 또는 좁은 예외 등록 |
| 프로젝트 경계 혼입 | 다른 프로젝트 파일을 별도 표시 | 스캔 root와 ownership 규칙 교정 |

## 5. 범위

### 포함

- 라우트, 라우터 마운트, 네임스페이스, SQL 테이블, 프런트 호출 구조 추출
- 구조 결함 규칙과 severity 분류
- GO100 매시간 스캔과 중앙 DB 최신 스냅샷
- 세션 도구 `aag_findings`, `aag_brief`
- 러너 착수 브리프와 커밋 게이트
- 목표 문서 및 마일스톤 근거 연결
- #310 관련 파일의 구조 영향 판정과, 정적 그래프 밖 순수 로직의 별도 호출 그래프 보완

### 제외

- AAG 정적 그래프만으로 전략 수익성이나 매매 의사결정을 판정하는 것
- 순수 Python 동적 호출을 완전하게 추론하는 것
- P2를 근거 없이 일괄 삭제하는 것
- 다른 프로젝트의 결함을 GO100 목표 실적으로 합산하는 것
- 스캔 실패를 결함 0건으로 간주하는 것

## 6. 성공 기준

1. 목표 화면에 기획서·설계서·PRD가 각각 연결되어 언제든 열 수 있다.
2. GO100 최신 스냅샷이 정상 운용 시 75분보다 오래되지 않는다.
3. `aag_findings(GO100)`와 `aag_brief(GO100, target)`가 같은 최신 그래프를 기준으로 답한다.
4. P0/P1은 근거 있는 수정 또는 예외 판정 없이 완료 처리되지 않는다.
5. 구조 관련 변경이 baseline 부채를 늘리면 커밋 또는 리뷰 단계에서 차단된다.
6. 스캔 실패, 적재 실패, stale 상태가 각각 구분되고 복구 행동이 표시된다.
7. #310 순수 로직처럼 그래프 밖 대상은 `code_explorer` 또는 호출 그래프로 보완했다는 사실을 판정 문서에 명시한다.

## 7. 마일스톤

| 순서 | 마일스톤 | 완료 기준 |
|---|---|---|
| AO1 | 중앙 스냅샷 경로 가동 | `aag_graph_snapshots`에 GO100 스냅샷 존재 |
| AO2 | GO100 로컬 그래프 자동화 | `aag_brief(project=GO100)` 정상 응답 |
| AO3 | 정기 스캔과 부채 상환 | 최신 스냅샷 기준 P0=0, P1=0 |
| AO4 | #310 구조 판정 | 근거·리스크·롤백을 포함한 판정과 DB 핸드오버 |
| AO5 | 운영 신뢰성 강화 | freshness 경보, 실패 재시도, 전 프로젝트 상태판 |

## 8. 기획 원칙

- 최신 그래프가 없으면 모른다고 답한다.
- baseline은 허용량이 아니라 줄여야 할 현재 부채다.
- 자동 생성 리포트와 사람이 승인한 설계 문서를 구분한다.
- 사용자 핵심 경로에는 내부 도구명보다 “영향 확인”, “구조 결함”, “다시 스캔”처럼
  이해 가능한 업무명을 쓴다.
- 목표 완료는 코드 변경만이 아니라 문서, 검증, 배포, 운영 스냅샷까지 확인한 뒤 판정한다.

## 9. v1.1 보완 방향

v1.1은 v1.0의 FR-001~FR-014, NFR-001~NFR-008, 사용자 시나리오,
AO1~AO5, 수용 테스트와 완료 정의를 승계한다. 이 절과 연결된 v1.1 계약이
명시적으로 바꾼 조항만 대체하며 충돌 시 v1.1을 우선한다.

1. 실행(run), 불변 graph(snapshot), 특정 시점 검증(observation)을 분리한다.
2. 같은 콘텐츠는 graph를 중복 저장하지 않고 observation만 추가해 freshness를 갱신한다.
3. latest는 `project/repository/ref/governance_scope`별로 관리하며 과거 commit이 덮지 못한다.
4. baseline은 총건수가 아닌 versioned stable finding key 집합으로 비교한다.
5. freshness, last run, debt, coverage를 독립 상태로 표시한다.
6. fallback은 참고 전용이며 authoritative evidence나 gate 성공으로 사용하지 않는다.
7. exception, severity 하향, rule 승격, 중앙 장애 override는 승인·만료·감사 계약을 따른다.
8. 전체 coverage와 위험 경로 coverage를 분리하고 미관측·미탐을 정상으로 표현하지 않는다.
9. v1/v2 API를 병행해 소비자 전환과 rollback 검증 후에만 v1 폐기를 승인한다.

## 10. Phase 0 판정과 실행 게이트

2026-09-19 15:50 KST 실측 결과는 **B — 기존 기준선 재현 불가**다. 현재 중앙
테이블은 `aag_graph_snapshots` 한 개뿐이고 GO100 10개 snapshot의 non-empty
`commit_sha`가 0건이다. run/observation, repository/ref, fingerprint, baseline,
publish 상태와 audit 이력이 없어 71건 기준선을 동일 입력으로 재현할 수 없다.

- AO1~AO3의 기존 `completed`는 과거 계약 evidence로 보존한다.
- v1.1 검증 상태는 별도 마일스톤으로 관리하며 소급 취소하지 않는다.
- 신규 기준선 후보는 v1.1 canonicalization·stable key로 재스캔한 뒤 승인한다.
- Phase 0 B 판정, 계약, 기준선 재수립안 승인 전에는 파괴적 migration, v1 제거,
  blocking gate를 실행하지 않는다.

## 11. 우선순위 로드맵

| 순서 | v1.1 마일스톤 | 완료 기준 |
|---:|---|---|
| V11-0 | Phase 0 감사·B 판정 승인 | 감사 보고서, 소비자 목록, 재수립·rollback안 승인 |
| V11-1 | run/snapshot/observation·no-change 계약 | additive migration, 동일 content 재사용, observation freshness 테스트 |
| V11-2 | ref별 latest·원자 publish | out-of-order/source-behind 차단, 실패 시 정상 pointer 유지 |
| V11-3 | stable key·baseline·fixture | key-set gate, baseline 불변성, 동일 입력 2회 결정성 |
| V11-4 | brief 안전성·coverage·미탐 | negative assertion 금지, 골든셋, 위험 coverage, miss review |
| V11-5 | API v2 이중 운영·소비자 전환 | v1/v2 병행, pinning/telemetry/rollback, 잔여 소비자 0 |
| V11-6 | RBAC·감사·override | project scope, 승인 분리, 만료·replay 방지, 사후 재검증 |
| V11-7 | UI·scheduler·복구·멀티프로젝트 | 4축 상태, 모바일/접근성, restore test, 두 번째 프로젝트 온보딩 |
