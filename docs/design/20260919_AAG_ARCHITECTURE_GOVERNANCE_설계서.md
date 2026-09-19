# AAG 아키텍처 거버넌스 — 기술 설계서 v1.1 Final

- 작성: 2026-09-19 KST
- 버전: 1.1 Final
- 상태: 최종 통합본(구현 전 계약 승인 대기)
- 적용 목표 ID: `40cfdfc5-06f9-4861-8dc9-27c8688cb3f7`
- 배경: [기획서](../plans/20260919_AAG_ARCHITECTURE_GOVERNANCE_기획서.md)
- 요구사항: [PRD](../prd/20260919_AAG_ARCHITECTURE_GOVERNANCE_PRD.md)
- 상세 계약: [v1.1 데이터·API 계약](../contracts/20260919_AAG_V1_1_DATA_API_CONTRACT.md)
- Phase 0: [감사 보고서](../reports/20260919_AAG_V1_1_PHASE0_AUDIT.md)

## 1. 설계 목표

프로젝트별 코드를 동일한 그래프 계약으로 추출하되 스캔 규칙과 소유 경계는 프로젝트별로
분리한다. 생성된 그래프는 로컬 산출물과 중앙 DB 스냅샷으로 보존하고, 채팅 세션·러너·
커밋 게이트·목표 화면이 같은 정본을 읽게 한다.

## 2. 논리 아키텍처

```text
GO100 source + rules_go100.yml
             │
             ▼
      scan_aads.py ────────► graph.json / findings.md / arch.mmd
             │                              │
             │ push                         │ local fallback
             ▼                              ▼
     aag_graph_snapshots  ◄──────── app/services/aag_tools.py
             │                              │
             ├────────► aag_findings ───────┤
             ├────────► aag_brief ──────────┤
             └────────► /api/v1/aag/*       │
                                            ▼
                            chat session / pipeline runner / goal evidence
```

정적 그래프 밖 순수 로직은 별도 호출 그래프가 보완한다.

```text
pure Python target ──► code_explorer/call graph ──► AO4 판정 문서
```

## 3. 컴포넌트 계약

| 컴포넌트 | 정본 경로 | 책임 | 실패 시 동작 |
|---|---|---|---|
| 스캐너 | `tools/aag/scan_aads.py` | AST/SQL/프런트 호출 추출, findings 생성 | exit 2로 스캔 불가 명시 |
| GO100 규칙 | `tools/aag/rules_go100.yml` | root, 라우터, SQL, 출력 경계 | AADS 규칙으로 자동 대체하지 않음 |
| GO100 갱신 | `scripts/aag_go100_refresh.sh` | 원격 스캔, 산출물 회수, DB 적재 | 잠금·로그를 남기고 이전 스냅샷 유지 |
| 스냅샷 API | `app/api/aag.py` | 프로젝트별 스냅샷 적재·조회 | 적재 실패 시 로컬 fallback 허용 |
| 세션 도구 | `app/services/aag_tools.py` | findings 필터와 착수 브리프 | source와 stale_minutes 표시 |
| 브리프 렌더러 | `tools/aag/brief.py` | target과 연결된 노드·결함 요약 | 관련 노드 없음으로 명시 |
| 커밋 게이트 | `scripts/hooks/pre-commit` | baseline보다 부채 증가 차단 | scan_failed와 clean을 구분 |
| 러너 주입 | `scripts/pipeline-runner.sh` | 작업 전에 AAG 브리프 제공 | timeout은 로그에 skip 사유 기록 |
| 목표 문서 | `goal_documents` | 기획·설계·PRD·근거 연결 | 없는 문서 종류를 목표 화면에 표시 |

## 4. 그래프 모델

### 4.1 노드

- ASGI entrypoint
- router module
- mounted route (`METHOD + normalized path`)
- namespace owner
- service/module file
- referenced SQL table
- frontend API call

### 4.2 엣지

- `includes_router`
- `mounts_route`
- `calls_endpoint`
- `reads_table` / `writes_table`
- `owns_namespace`
- `imports` 또는 정적으로 확인 가능한 모듈 관계

### 4.3 finding

```json
{
  "severity": "P0|P1|P2",
  "rule": "ROUTE_SHADOWED",
  "key": "stable-deduplication-key",
  "module": "relative/path.py",
  "detail": "사람이 이해할 수 있는 원인과 영향"
}
```

finding에는 최소 `severity`, `rule`, 안정적인 `key`, 위치, 설명이 있어야 한다.
프로젝트마다 같은 규칙 이름은 같은 의미를 가져야 한다.

## 5. 저장 설계

### 5.1 로컬 산출물

| 파일 | 용도 |
|---|---|
| `reports/aag/go100-graph.json` | 세션 도구 로컬 fallback과 자동 분석 |
| `reports/aag/go100-findings.md` | 사람이 읽는 최신 결함 보고 |
| `reports/aag/go100-arch.mmd` | 구조 시각화 원본 |

자동 생성 파일은 현재 상태이며 기획·설계·PRD 정본을 대체하지 않는다.

### 5.2 중앙 DB

`aag_graph_snapshots`는 `(project, generated_at)`을 유일키로 하며, `stats`,
`findings`, `unresolved`, node/edge/finding 수, host, commit SHA를 보존한다.
조회는 프로젝트별 `generated_at DESC LIMIT 1`을 사용한다.

`goal_documents`는 문서 사본을 저장하지 않고 경로를 연결한다. 현재 kind 계약은
`plan | prd | report | reference`이므로 독립 설계서는 `reference`로 연결하되 제목과
note에 “기술 설계서”를 명시한다. kind 확장은 별도 호환성 변경으로 다룬다.

## 6. 데이터 흐름

1. 스케줄러가 프로젝트별 refresh를 시작한다.
2. refresh는 중복 실행 잠금을 획득한다.
3. 스캐너가 지정 root와 rules로 그래프를 생성한다.
4. 자기검증과 산출물 유효성 검사를 통과한 경우에만 최신 파일을 교체한다.
5. snapshot pusher가 중앙 DB에 멱등 upsert한다.
6. 세션 도구는 DB 최신 스냅샷을 우선 사용하고, DB가 없을 때만 로컬 그래프를 쓴다.
7. 응답에는 `source`, `generated_at`, `stale_minutes`를 포함한다.
8. 목표·마일스톤 완료 증거에는 사용한 snapshot 시각과 commit SHA를 기록한다.

## 7. 스케줄과 freshness

- GO100: 매시간 25분 실행, 중복 실행은 `flock`으로 차단한다.
- AADS: 구조 스캔과 행위 스캔은 서로 다른 타이머로 운용한다.
- GO100 정상 freshness SLO: 75분 이내.
- 75분 초과: stale 경고, 120분 초과 또는 연속 2회 실패: 운영 알림 대상.
- 새 스캔 실패 시 이전 정상 스냅샷을 삭제하거나 결함 0건으로 덮어쓰지 않는다.

## 8. 도구 계약

### `aag_findings`

- 입력: `project`, 선택 `rule`, `severity`, `path_prefix`, `limit`
- 출력: snapshot 시각, stale, source, 필터된 findings
- 빈 결과: “결함 없음”, “스냅샷 없음”, “필터 결과 없음”을 구분한다.

### `aag_brief`

- 입력: `project`, `target`(파일 경로 또는 지시서 본문)
- 출력: 관련 노드, 경로, 테이블, findings, 그래프 범위 밖 여부
- 순수 로직이 그래프에 없으면 관련 없음이 아니라 “정적 그래프 미포함”으로 판정한다.

## 9. 프로젝트 경계

GO100 스캔은 `/root/kis-autotrade-v4`를 root로 사용하되, GO100 소유 라우트와 공유/KIS
의존성을 구분한다. 다른 프로젝트 소유 파일에서 발견된 결함은 GO100 목표의 P0/P1 실적으로
자동 합산하지 않고 `external_dependency`로 분리한다. 교정 작업은 해당 프로젝트의 승인·배포
계약을 따른다.

## 10. 보안과 운영 안전

- snapshot 적재에는 서비스 인증을 사용하고 토큰을 그래프·로그·문서에 쓰지 않는다.
- 조회 도구는 읽기 전용이며 프로젝트/테넌트 범위를 벗어나지 않는다.
- 스캔은 운영 프로세스를 재시작하거나 매매 코드를 실행하지 않는다.
- 자동 생성 파일의 절대 경로를 외부 사용자에게 노출하는 UI는 업무명 링크로 변환한다.
- 배포가 필요한 변경은 프로젝트별 배포 계약과 롤백 절차를 따른다.

## 11. 장애 복구 설계

| 상태 | 탐지 | 자동 조치 | 사람 조치 |
|---|---|---|---|
| scheduler 누락 | 다음 예정 시각 초과 | 수동 refresh 1회 | cron/systemd 정본 복구 |
| scanner exit 2 | exit code + 대상 0개 | 최신 정상본 유지 | root/rules 교정 |
| ingest 실패 | API/DB 오류 | 제한 재시도 | 인증·DB 상태 점검 |
| stale snapshot | `stale_minutes` | 로컬 fallback | 원격 스캔 상태 점검 |
| false positive | 반복 예외와 코드 근거 | 없음 | 파서 수정 또는 만료 예외 |
| rule regression | selftest 실패 | 배포/커밋 차단 | fixture와 규칙 수정 |

## 12. 검증 설계

1. `tools/aag/selftest_aads.py`와 프로젝트별 fixture로 규칙을 검증한다.
2. 샘플 프로젝트에서 중복 라우트, 미마운트 라우터, SQL 참조를 의도적으로 만들어 탐지한다.
3. DB 최신 스냅샷과 로컬 graph의 `generated_at`, finding count를 대조한다.
4. `aag_findings` 필터와 `aag_brief` 타깃 매칭 단위 테스트를 실행한다.
5. 목표 상세 API가 기획서·설계서·PRD 3개 경로를 반환하는지 확인한다.
6. 스캔 실패 시 이전 정상본이 유지되고 stale/실패 상태가 노출되는지 확인한다.

## 13. 롤백

- 문서 연결 오류: `goal_documents`의 해당 goal/path 연결만 제거한다. 원본 문서는 보존한다.
- 규칙 회귀: 이전 rules/baseline 커밋으로 되돌리고 새 스냅샷 적재를 중지한다.
- 적재 장애: DB 쓰기를 멈추고 검증된 로컬 그래프를 읽기 전용 fallback으로 사용한다.
- 스케줄 장애: hourly 항목을 비활성화하고 마지막 정상 스냅샷과 수동 실행 절차를 유지한다.

## 14. v1.1 논리 모델

```text
scan_run (모든 시도)
  ├─ input_fingerprint
  ├─ result: succeeded | no_change_success | scan_failed | ingest_failed |
  │          skipped_locked | cancelled | out_of_order | source_behind
  └─ snapshot_observation ──► graph_snapshot (불변 content)
                                  ├─ graph_nodes / graph_edges
                                  └─ finding_occurrences ──► finding_definition

latest_pointer(project, repository, target_ref, governance_scope)
baseline ──► baseline_findings(stable_finding_key, stable_key_version)
```

`generated_at`은 fingerprint에 포함하지 않는다. snapshot 최초 publish 시각은 불변이며,
동일 content 재검증은 observation과 latest authoritative verification 시각만 추가한다.

## 15. Canonicalization과 stable key

- UTF-8, LF, repository-relative POSIX path, JSON object key 정렬을 사용한다.
- node/edge/finding 배열은 stable identity 기준으로 정렬한다.
- timestamp, run ID, host 임시값, 절대 경로를 content fingerprint에서 제외한다.
- hash는 SHA-256, canonicalization/stable key schema는 명시적 version을 갖는다.
- stable finding key는 project, rule, semantic target, route/table/symbol,
  normalized path, contract signature로 만든다. line number는 보조 evidence다.
- key version 변경은 과거 값을 덮지 않고 mapping 또는 신규 baseline 승인을 요구한다.

## 16. Publish와 latest pointer

한 DB transaction에서 candidate metadata → nodes/edges/occurrences → 집계·hash 검증 →
`ready` → observation → latest pointer 순으로 publish한다. 실패·partial·집계 불일치는
ready가 될 수 없고 기존 usable pointer를 유지한다. latest 갱신 전
`resolved_commit_sha == expected_target_ref_head_sha`와 ancestry를 검사한다.

## 17. 상태와 API 전환

상태는 `freshness`, `last_run_result`, `debt_level`, `coverage` 네 축으로 제공한다.
v1은 기존 project/generated_at 계약을 유지하고 v2는 run/snapshot/observation,
repository/ref, authoritative, coverage, pagination pinning을 additive하게 제공한다.
v1 telemetry와 rollback 검증 후 승인된 시점에만 v1을 제거한다.

## 18. 보안·감사·override

- Scanner credential은 project-scoped이며 다른 프로젝트 적재·조회가 불가하다.
- baseline 제안자/승인자, severity·exception·rule 승격 승인자를 분리한다.
- audit event는 actor/action/target/before/after/reason/correlation/time/source를 보존한다.
- local fallback은 항상 `authoritative=false`이며 gate·목표 완료 evidence로 금지한다.
- 중앙 장애 override는 request ID, commit, 승인자, 만료, 이전 정상본, 독립 검증,
  rollback, replay 방지와 복구 후 재검증 job을 요구한다.

## 19. 구현 제한

Phase 0 B 판정, 신규 계약, baseline 재수립안, API 병행·rollback, 권한·감사,
최소 수용 테스트 계획이 승인되기 전에는 파괴적 migration, v1 제거, blocking gate를
활성화하지 않는다. 최초 구현은 additive migration과 advisory 모드로 한정한다.
