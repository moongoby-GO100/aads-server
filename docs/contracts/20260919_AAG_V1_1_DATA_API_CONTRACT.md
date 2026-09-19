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
