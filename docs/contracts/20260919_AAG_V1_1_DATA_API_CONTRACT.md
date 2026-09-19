# AAG v1.1 데이터·API 계약

## 1. 정본과 식별자

정본은 중앙 DB에서 검증 후 `ready`로 publish된 snapshot과 authoritative observation이다.
로컬 결과는 `source=local_fallback`, `authoritative=false`이며 복구·참고 외 용도로
승격하지 않는다.

필수 evidence: snapshot_id, observation_id/run_id, project_id, repository_id,
target_ref, commit_sha, detection/severity/gate policy digest, stable_key_version,
scan_scope_digest, generated_at, verified_at/published_at.

## 2. v2 최소 데이터 모델

- `aag_scan_runs`: 모든 시도와 input fingerprint, 단계별 상태·오류
- `aag_graph_snapshots_v2`: 불변 content와 publish 상태
- `aag_snapshot_observations`: run-snapshot-ref 검증 이력과 authoritative 여부
- `aag_graph_nodes`, `aag_graph_edges`
- `aag_finding_definitions`, `aag_finding_occurrences`
- `aag_latest_pointers`: project/repository/ref/scope별 latest 상태
- `aag_baselines`, `aag_baseline_findings`
- `aag_rules`, `aag_gate_policy_versions`, `aag_finding_exceptions`
- `aag_audit_events`
- 기능 활성 시 override, critical path, miss review, supplemental analysis 테이블

## 3. fingerprint

`input_fingerprint`는 project/repository/ref/resolved SHA, detection ruleset,
scanner build/version, parser versions, scan scope, normalization version, submodule과
필요 시 working-tree digest의 SHA-256이다. `content_fingerprint`는 canonical
nodes/edges/findings의 SHA-256이며 실행 시각과 host 임시값을 제외한다.

## 4. API v2

- findings 필터: project, repository, target_ref, rule, severity, path,
  snapshot_id, stable_finding_key, status, owner
- brief 입력: project/repository/ref/commit, file_paths/instruction,
  requested_snapshot_id, max_results
- 공통: deterministic sort, pagination+snapshot pinning, max size, timeout/retry,
  request ID, truncation, validation error와 rate limit
- 응답: schema_version, snapshot/observation/commit/ref, source, authoritative,
  generated/verified, freshness, coverage, limitation, pagination

`not_detected`, outside_scope, partial, stale, fallback, mismatch, source_behind,
authoritative=false, truncated는 안전 신호가 아니며 추가 확인 행동을 반환한다.

## 5. 상태·gate

상태는 freshness(fresh/stale/unavailable), last run, debt, coverage 네 축이다.
gate는 stable key 집합 차이로 신규 P0/P1, 승격, exception 만료, key/rules/scope
불일치, coverage 축소, partial, source behind, 비정본을 차단한다. 초기 정책은
advisory이며 승인된 관측 근거 없이 required/blocking으로 승격하지 않는다.

## 6. scanner·scheduler

exit code는 0 성공, 2 대상 없음, 3 설정/rules, 4 parse/graph, 5 gate,
6 ingest, 7 lock/duplicate skip, 8 internal error다. scanner/ingest/gate 상태를
분리한다. lock key는 project/repository/ref이고 owner/acquired/heartbeat/expires/
released/reason을 기록한다. 운영·매매 프로세스는 제어하거나 재시작하지 않는다.

## 7. 보안·override

artifact는 hash, producer, scanner build, project scope, created time, replay window,
무결성 evidence를 갖는다. root/symlink/path traversal을 차단하고 secret을 redaction한다.
중앙 장애 override는 별도 승인 객체이며 fallback을 authoritative로 바꾸지 않는다.

## 8. 보존·복구

run, observation, snapshot, graph body, finding identity/occurrence, audit, evidence를
분리 보존한다. goal/baseline/incident/override/handover evidence는 pin한다. restore
시험은 baseline과 latest pointer, stable key, evidence 일치를 검증한다.

## 9. V11-1 additive rollout

- migration: `migrations/20260919_aag_v1_1_foundation.sql`; legacy
  `aag_graph_snapshots`는 수정하지 않는다.
- ingest: `app/services/aag_ingest_v2.py`; 동일 content는 immutable snapshot을
  재사용하고 run/observation만 추가해 freshness를 갱신한다.
- source fence: `resolved_commit_sha`와 `expected_target_ref_head_sha`가 다르면
  observation은 `commit_mismatch`, `authoritative=false`이며 정상 freshness로 승격하지 않는다.
- retry: 동일 `run_id`와 같은 input은 기존 결과를 반환하고, 다른 input으로 재사용하면
  conflict로 거부한다.
- API: `/api/v1/aag/v2/snapshots`; `AAG_V2_ENABLED=1`에서만 열리며 기본값은 off다.
- rollback: flag를 off로 되돌리면 v2 ingest를 즉시 중지한다. 실제 DB migration 적용과
  v2 활성화는 배포·운영 승인 뒤 진행하며 v1 API와 legacy row는 그대로 유지한다.

## 10. V11-2 authoritative latest publish

- DB handover 정본 migration 순서는 `20260919_aag_v1_1_foundation.sql` 다음
  `20260919_aag_v1_1_latest_pointers.sql`이다. 두 파일은 additive·반복 적용 가능하며
  legacy `aag_graph_snapshots`를 변경하지 않는다.
- 이미 초기 foundation draft가 적용된 운영 호스트에서는 release asset diff가
  `latest_pointers.sql`만 재실행할 수 있으므로, 해당 migration이 누락된
  `expected_target_ref_head_sha`와 `verification_status`를 자체 backfill한다.
- `aag_ref_heads`와 `aag_latest_pointers`의 격리 키는
  `(project, repository_id, target_ref, governance_scope)`이다.
- ingest는 격리 키의 PostgreSQL advisory transaction lock을 획득한 뒤 같은 transaction에서
  candidate insert, ready 전환, observation insert, authoritative pointer 갱신 순으로 수행한다.
  어느 단계든 실패하면 publish 전체가 rollback되고 별도 실패 run만 기록한다.
- 같은 content는 기존 ready snapshot과 `first_published_at`을 보존하고 새 observation의
  `verified_at` 및 authoritative pointer의 freshness만 갱신한다.
- `resolved_commit_sha != expected_target_ref_head_sha`는 `source_behind`, 현재 pointer보다
  `generated_at`이 오래된 실행은 `out_of_order`다. 두 observation은 보존하지만 pointer와
  ref-head freshness를 갱신하지 않는다.
- `GET /api/v1/aag/v2/latest`는 project/repository_id/target_ref/governance_scope를 정확히
  지정하며 snapshot, observation, run, ref, commit, source, authoritative,
  generated_at, verified_at, freshness, limitation을 반환한다.
- v2 write/read 모두 `AAG_V2_ENABLED` 기본 off fence를 공유한다. tenant/RBAC 변경은 없다.
- rollback은 flag off로 신규 접근을 차단하는 방식이며, additive table은 감사·재처리 근거로
  보존한다. pointer 복구는 authoritative observation과 ref/order evidence로 재구축한다.

## 11. V11-3 stable-key baseline and rule lifecycle

- 모든 v1.1 finding은 `aag-stable-key-v1:<sha256>` 키를 갖는다. 키 입력은 project,
  rule, semantic target, normalized repository-relative path, route/table/symbol contract이며
  line number와 설명 문구는 evidence로만 남아 키를 바꾸지 않는다.
- 동일 입력은 collection 순서·실행 시각과 무관하게 같은 stable key set과 content
  fingerprint를 생성한다. 수신자가 canonical identity와 다른 키를 보내면 ingest를 거부한다.
- 신규 baseline gate는 count 증가가 아니라 stable key set 차이를 평가한다. `enforced`
  rule의 예외 없는 신규 key만 차단하고, 신규/`warn_only` rule은 관측 경고로 남긴다.
- approved baseline의 identity와 finding membership은 DB trigger로 불변이다. 교체 시 기존
  baseline은 `superseded`로 보존하고 독립 승인된 신규 baseline을 활성화한다.
- 신규 rule은 `warn_only`로 시작한다. 최소 2회 관측, 통과한 golden fixture digest,
  제안자와 다른 승인자가 모두 확인해야 `enforced`로 승격된다.
- rollback은 신규 rule을 enforce하지 않고 warn-only로 유지하거나 직전 approved baseline을
  계속 사용한다. additive table과 audit evidence는 삭제하지 않는다.
