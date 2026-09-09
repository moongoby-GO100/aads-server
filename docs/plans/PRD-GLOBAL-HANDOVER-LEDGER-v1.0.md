# PRD: Global Handover Ledger v1.0

- 문서 상태: 구현 기준선
- 작성일: 2026-09-09 KST
- 정본 시스템: AADS 중앙 PostgreSQL
- 첫 적용 프로젝트: GO100
- 적용 대상: AADS, GO100, KIS, SF, NTV2 및 신규 프로젝트

## 1. 배경과 문제

프로젝트별 `HANDOVER.md`는 사람과 Git 감사에는 유용하지만 여러 세션과 러너가 동시에 덧붙이면서 정본이 여러 경로로 갈라지고, 파일이 무제한으로 커지며, 현재 상태와 과거 이력이 섞였다. GO100에서는 루트와 `docs/`의 핸드오버가 병존하고 작성 도구도 서로 다른 경로를 사용해 문서현황 누락과 충돌 위험이 확인됐다.

## 2. 제품 목표

1. AADS 중앙 DB를 테넌트별·프로젝트별 핸드오버 정본으로 사용한다.
2. 현재 상태는 즉시 검색하고 모든 변경 리비전은 불변 이벤트로 보존한다.
3. 기존 Markdown은 폐기하지 않고 가져오기·내보내기 호환 형식으로 유지한다.
4. 모든 채팅 세션이 동일한 기록·검색 도구를 사용하게 한다.
5. GO100을 시범 적용하되 KIS 주문·계좌·매매 원장은 전혀 변경하지 않는다.

## 3. 사용자 흐름

### 첫 적용

관리자는 기존 `HANDOVER.md`를 `/api/v1/handovers/import`로 가져온다. 제목과 원본 경로에서 만든 안정 키로 재실행해도 중복 생성되지 않는다.

### 반복 사용

에이전트는 작업 종료 전 `handover_write`로 상태·결정·작업·위험·검증을 기록한다. 사용자는 `handover_search` 또는 REST 조회로 프로젝트·상태·유형·키워드를 검색한다.

### 충돌 복구

클라이언트는 읽은 `revision`을 `expected_revision`으로 보내며, 다른 세션이 먼저 갱신했으면 HTTP 409 또는 `revision_conflict`를 받고 최신 항목을 다시 읽은 뒤 병합한다.

### 기존 문서 사용

`/api/v1/handovers/export?project_key=GO100` 또는 `handover_export`는 현재 DB 상태를 Markdown으로 생성한다. 생성 문서는 읽기용이며 DB가 정본임을 머리말에 표시한다.

## 4. 데이터 모델

| 테이블 | 역할 | 핵심 키 | 보존 정책 |
|---|---|---|---|
| `project_handover_entries` | 현재 상태 | `tenant_id + project_key + entry_key` | 상태 전환, 리비전 증가 |
| `project_handover_events` | 변경 감사 원장 | `entry_id + revision` | UPDATE/DELETE 금지 |

항목 유형은 `status`, `decision`, `task`, `risk`, `verification`, `note`, 상태는 `active`, `resolved`, `superseded`, `archived`로 제한한다. 본문은 1,000,000자, metadata는 직렬화 기준 100,000바이트로 제한한다.

## 5. 검색 설계

- 본문·요약·제목: PostgreSQL `tsvector`와 GIN을 사용한다.
- 제목·원본 경로 오탈자: `pg_trgm` GIN을 보조로 사용한다.
- `pg_trgm`은 본문 전체의 주 검색기가 아니다. 본문까지 trigram 인덱싱해 저장공간과 쓰기비용이 커지는 구성을 피한다.
- 모든 쿼리는 `tenant_id`를 첫 조건으로 사용해 테넌트 간 노출을 막는다.

## 6. API와 채팅 도구

| 기능 | REST | 채팅 도구 |
|---|---|---|
| 기록/멱등 갱신 | `POST /api/v1/handovers` | `handover_write` |
| 검색 | `GET /api/v1/handovers` | `handover_search` |
| 단건/이력 | `GET /api/v1/handovers/{id}`, `/events` | 검색 결과의 ID 사용 |
| Markdown 가져오기 | `POST /api/v1/handovers/import` | REST/운영 스크립트 |
| Markdown 내보내기 | `GET /api/v1/handovers/export` | `handover_export` |

조회는 tenant viewer 이상, 쓰기·가져오기는 tenant member 이상 권한이 필요하다. 삭제 API는 제공하지 않는다.

## 7. 글로벌 적용 규칙

- 프로젝트 키는 중앙 프로젝트 별칭으로 정규화하되 신규 프로젝트도 안전한 영문·숫자·점·밑줄·하이픈 키로 등록할 수 있다.
- 모든 세션에서 `handover_write`, `handover_search`를 eager core tool로 제공한다.
- 프로젝트별 DB에는 새 원장을 만들지 않는다. 중앙 AADS만 정본을 보유한다.
- 원격 프로젝트 장애가 중앙 기록을 훼손하지 않으며, 중앙 장애 시 기존 Markdown을 읽기 전용 폴백으로 사용한다.

## 8. GO100 시범 적용 범위

GO100에는 중앙 원장 사용 시작을 나타내는 구조화 항목을 기록하고 검색·수정·이벤트·Markdown 내보내기를 검증한다. 기존 대형 핸드오버 파일은 삭제하거나 덮어쓰지 않는다. 전체 과거 이력 백필은 별도 dry-run에서 섹션 수와 중복률을 확인한 뒤 수행한다. KIS와 공유하는 코드·DB 및 주문 경로에는 변경이 없다.

## 9. 수용 기준

1. 마이그레이션을 두 번 실행해도 성공하고 테이블 수·인덱스가 중복되지 않는다.
2. 같은 `entry_key`와 같은 내용의 재기록은 `changed=false`, 리비전·이벤트 수가 증가하지 않는다.
3. 변경된 내용은 리비전이 1 증가하고 이벤트 스냅샷이 추가된다.
4. 다른 tenant ID로 같은 entry ID를 조회할 수 없다.
5. `pg_trgm` 유사 제목 검색과 FTS 본문 검색이 모두 동작한다.
6. GO100 시범 항목이 검색되고 Markdown으로 내보내진다.
7. API blue/green 배포 후 candidate·active·standby가 같은 image digest이며 외부 health와 5분 P0/P1 모니터링이 통과한다.

## 10. 롤백

애플리케이션은 직전 이미지로 라우팅을 되돌린다. 마이그레이션은 additive이며 기존 테이블과 파일을 수정하지 않으므로 DB 테이블은 비활성 상태로 보존한다. 감사 원장 보호 때문에 자동 DROP/TRUNCATE 롤백은 금지한다.

## 11. 후속 단계

- P1: 문서현황 화면에 DB 정본 필터와 리비전 타임라인 추가
- P1: 프로젝트별 Markdown 정기 발행과 Git PR 자동화
- P2: 과거 대형 파일 dry-run/중복 보고 후 점진 백필
- P2: 보존 기간과 아카이브 정책을 tenant 설정으로 제공
