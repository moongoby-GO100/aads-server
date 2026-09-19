# AAG v1.0→v1.1 요구사항 추적표

상태 값: `implemented-v1`, `partial`, `planned`, `approval-gated`.
코드·테스트·evidence가 없는 항목은 완료로 간주하지 않는다.

| v1.0/v1.1 요구사항 | 처리 | 기술·DB/API 계약 | 구현 파일/영역 | 테스트·evidence | 상태 |
|---|---|---|---|---|---|
| FR-001~002 | 유지·보강 | project rules, finding contract | `tools/aag/scan_aads.py`, rules YAML | AT-003~007, 62~65 | implemented-v1 |
| FR-003 | 대체 | run/input/content/observation/latest pointer | foundation+latest migration, `aag_ingest_v2.py` | AT-011~017, 77~81 | implemented-v11-2 |
| FR-004 | 수정 | v2 filter/pagination/pinning | AAG v2 API·tool service | AT-098~102 | planned |
| FR-005 | 수정 | authoritative brief+limitation | brief v2 API·renderer | AT-034~038, 100~102 | planned |
| FR-006 | 보강 | hourly+DB lock/heartbeat | refresh scheduler·scan_runs | AT-009, 93~97 | partial |
| FR-007 | 대체 | stable key-set baseline gate | baseline/gate service | AT-025~033 | planned |
| FR-008 | 보강 | snapshot/commit pinned injection | pipeline runner wiring | AT-035, 47, 93 | partial |
| FR-009 | 유지·보강 | typed document identity/hash | goals/document API+CI | AT-001~002, 118~120 | partial |
| FR-010 | 보강 | atomic publish, source-behind/out-of-order 상태 | scanner/ingest/publish | AT-006, 15~24 | implemented-v11-2 (coverage planned) |
| FR-011 | 보강 | coverage/limitation/analyzer | brief v2+supplemental analysis | AT-005, 34~38, 48 | planned |
| FR-012 | 보강 | immutable milestone evidence | goal evidence integration | AT-047~050 | planned |
| FR-013 | 보강 | project RBAC+metric scope | auth/aggregate services | AT-039~040, 111~113 | planned |
| FR-014 | 보강 | four-axis UI+recovery action | goal/ops UI | AT-018~024, 88~92, 117 | planned |
| FR-015~019 | 추가 | brief policy, analyzer, coverage, miss, lifecycle | runner/AAG governance services | AT-034~038, 47~65 | planned |
| FR-020~023 | 추가 | fixture, approval/audit, override, API dual-run | CI/migrations/v2 routes | AT-059~086 | approval-gated |
| FR-024~030 | 추가 | doc CI, history, rescan, aggregate, restore, drift, onboarding | CI/UI/ops services | AT-085~125 | planned |
| NFR-001~008 | 유지·보강 | freshness, determinism, safety, security, traceability, accessibility | scanner/API/UI | AT-006, 18~24, 39~46, 87~97 | partial |
| NFR-009~014 | 추가 | atomicity, compatibility, recovery, integrity, ref/scope isolation, observability | v2 data/API/ops | AT-011~024, 77~81; ops observability planned | partial |
| US-01~06 | 유지·보강 | first use, repeated use, recovery | goal UI/tools/runner/AO4 | AT-001~010, 34~38, 47~50, 88~92 | partial |
| AO1~AO3 | 유지+상태 분리 | legacy evidence / v1.1 verification | milestones+evidence | Phase 0 audit | implemented-v1 / v1.1-unverified |
| AO4 | 분리 | AO4-A #310, AO4-B coverage | supplemental analysis+handover | AT-047~058 | planned |
| AO5 | 보강 | alert/retry/override/restore | ops metrics/UI | AT-018~24, 66~71, 85~86, 121~122 | planned |

## 수용 테스트 연결

| 테스트 ID | 계약 영역 | 구현 milestone |
|---|---|---|
| AT-001~010 | 문서·기본 기능·scheduler | V11-0/V11-1 |
| AT-011~024 | 멱등성·원자성·상태·장애 | V11-1/V11-2 |
| AT-025~033 | gate·baseline | V11-3 |
| AT-034~038 | brief 골든셋 | V11-4 |
| AT-039~046 | 권한·보안 | V11-6 |
| AT-047~058 | AO4·coverage·미탐 | AO4-A/V11-4 |
| AT-059~065 | rule lifecycle·fixture | V11-3 |
| AT-066~071 | override | V11-6 |
| AT-072~086 | migration·API·freshness·ref·복구 | V11-1/V11-2/V11-5/V11-7 |
| AT-087~097 | 승계·UI·접근성·scheduler | V11-7 |
| AT-098~110 | findings/brief/exception/history | V11-3/V11-4/V11-6 |
| AT-111~125 | 집계·재스캔·문서·관측·민감정보 | V11-5/V11-6/V11-7 |

### V11-2 구현 evidence

- AT-011~017: run idempotency, content reuse, advisory DB fence, 단일 transaction publish,
  failure rollback 후 실패 run 기록.
- AT-018~024: commit mismatch/source-behind, generated order fence, pointer 보존,
  project/repository/ref/scope 격리, no-change freshness, latest read contract.
- DB 정본: `migrations/20260919_aag_v1_1_foundation.sql` →
  `migrations/20260919_aag_v1_1_latest_pointers.sql`. 운영 적용 여부와 별개로 이 순서가
  handover 기준이며 migration은 `BEGIN`/`ROLLBACK` 검증 대상이다.

## 운영 evidence 최소 키

모든 v1.1 milestone evidence는 snapshot_id, observation_id/run_id, project_id,
repository_id, target_ref, commit_sha, detection/severity/gate digest,
stable_key_version, scan_scope_digest, generated_at, verified_at/published_at를 포함한다.
