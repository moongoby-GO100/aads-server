# AAG v1.1 Phase 0 기존 데이터 감사

- 기준일: 2026-09-19 KST
- 목표 ID: `40cfdfc5-06f9-4861-8dc9-27c8688cb3f7`
- 우선 프로젝트: GO100
- 판정: **B — 기준선 재수립 필요(신 계약 기준 재현 불가)**

## 1. 실측 결과

| 항목 | 실측 | 판정 |
|---|---:|---|
| AAG 테이블 | `aag_graph_snapshots` 1개 | run/observation/baseline/audit 부재 |
| 전체 snapshot | 11건(AADS 1, GO100 10) | 이력은 있으나 실행 이력과 분리 안 됨 |
| GO100 non-empty commit SHA | 0/10 | 기존 결과 입력 재현 불가 |
| GO100 finding/content 집합 | 5/5종 | no-change를 별도 observation으로 기록하지 않음 |
| 최신 GO100 graph | node 983, edge 2,188, finding 71 | P0 0, P1 0, P2 71 |
| 최신 GO100 generated_at | 2026-09-19 15:26:53 KST | 15:50 KST 기준 freshness 24분 |
| scheduler | `25 * * * *`, `flock` | 매시간 실행·중복 방지 가동 |
| 기존 마일스톤 | AO1~AO3 completed, AO4 pending | 완료 evidence를 소급 취소하지 않음 |

근거: 중앙 DB `information_schema`, `aag_graph_snapshots`, `milestones` 조회와
contabo116 crontab/`aag-go100.log` 실측(2026-09-19 15:49~15:50 KST).

## 2. 컬럼·채움률

현재 컬럼은 project, host, generated_at, commit_sha, stats, findings, unresolved,
node/edge/finding count, created_at뿐이다. `commit_sha`는 GO100 전 행에서 NULL 또는
빈 문자열이다. repository, target_ref, ruleset/scanner/parser version, scan scope,
input/content fingerprint, publish/verification 시각, authoritative, stable key version,
baseline membership, latest pointer는 표현할 수 없다.

## 3. 재현 가능성 판정

GO100 10건 모두 핵심 입력인 resolved commit이 없어 snapshot별 동일 입력 재현이
불가능하다. 최신 4건의 finding hash는 같지만 이는 동일 소스·rules·scope였다는
증거가 아니다. 따라서 71건 기준선은 `legacy_unreproducible`로 보존하고 v1.1
canonicalization과 stable key를 적용한 신규 authoritative observation에서 기준선을
재수립한다. 기존 AO1~AO3 완료는 “v1.0 계약 완료 / v1.1 미검증”으로 병기한다.

## 4. 소비자와 호환 위험

| 소비자 | 현재 계약 | 전환 위험 |
|---|---|---|
| `/api/v1/aag/findings` | 최신 project snapshot, rule/severity/path | v2 필드 추가는 additive 가능 |
| `/api/v1/aag/projects` | 프로젝트별 generated_at/count/commit | 상태 4축을 별도 v2로 제공 필요 |
| `aag_findings` 세션 도구 | DB 최신 또는 local fallback | fallback을 정본처럼 읽을 위험 |
| `aag_brief` 세션/runner | 로컬 graph renderer | 중앙 snapshot·commit pinning 없음 |
| hourly push script | `(project, generated_at)` upsert | commit/ref/run/observation 미기록 |

v1을 유지한 채 v2를 추가하고, 세션 → runner → CI → 목표 화면 순으로 전환한다.
v1 사용량 telemetry와 rollback 검증 전에는 v1을 제거하지 않는다.

## 5. Migration·rollback 위험

- additive 신규 테이블로 시작하고 기존 `aag_graph_snapshots` row를 수정하지 않는다.
- 기존 row는 legacy import 대상으로만 참조하며 commit이 없어 authoritative 승격 금지다.
- latest pointer는 v2 ready snapshot에만 연결한다.
- rollback은 v2 route/consumer flag를 끄고 v1 읽기를 유지하는 방식이다.
- 예상 데이터 삭제: 0건. 기존 snapshot·AO evidence·문서 링크를 보존한다.

## 6. 승인 필요 항목

1. Phase 0 B 판정 및 신규 기준선 재수립
2. additive run/snapshot/observation 계약과 v1/v2 병행
3. 문서 표류 옵션 A(기계 판독 선언 + warn-only rule, 권장)
4. RBAC·audit·override 핵심 계약
5. 125개 수용 테스트를 단계별 필수 subset으로 집행하는 계획

승인 전 금지: 파괴적 migration, v1 제거, blocking gate, legacy 완료 이력 수정.
