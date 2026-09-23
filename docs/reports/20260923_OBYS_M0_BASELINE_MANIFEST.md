# 오비서 전체 진아서버 이전 M0 기준선·범위 manifest

| 항목 | 값 |
|---|---|
| 목표 | `df479771-f250-4a11-90a3-220432da2bfa` |
| 마일스톤 | `a60a4939-4599-4afd-ae6e-1fdc65098f52` — M0 이전 범위·기준선 확정 |
| 조사 시각 | 2026-09-23 18:33~18:44 KST |
| 판정 | **부분 확정 / M0 완료 보류** |
| 보류 사유 | 전체 RTO의 앱 기동·E2E 구간 미측정, 역할별 담당 세션·개별 PRD 확정 기록 없음, 목표 `paused`와 PRD `active` 상태 불일치 |
| 상위 PRD | `docs/prd/20260923_OBYS_FULL_MIGRATION_JINAH_PRD.md`, DB `goal_documents.id=214`, v1.1.0 `active` |

## 1. 소스와 대상 기준선

| 구분 | 실측값 | 출처 |
|---|---|---|
| AADS 조사 작업트리 HEAD | `6266c9089aadf3c7be53e6e70471fe62e3f84825`; `origin/main`보다 5 ahead/51 behind | `git status`, `git log` |
| AADS 운영 오비서 이미지 | revision `0720ed89caf2`, image digest `sha256:1969a61a242a5b6da57176709d385b95aef16a124f44bb98d9347ce8ed83a3c0` | `docker inspect aads-server-green` |
| 진아서버 회계비서 소스 | `6931f82bb0c8d3c1e8056d177f019fe7367af82c`, 브랜치 `관문개편`, origin보다 2커밋 앞, AAG 보고서 2파일 dirty | `ssh jinah244 git -C /srv/biseo/회계비서/회계비서 ...` |
| 공개 health | `https://fb.newtalk.kr/health/live` HTTP 200, 0.759315초 | `curl`, Cloudflare 경유 |
| 오비서 전용 API | 153 METHOD+PATH, local health HTTP 200/0.008576초 | 운영 `yeoljeong-finance` 컨테이너 route introspection |
| 소스 호스트 용량 | RAM 25,199,218,688B, 가용 15,320,735,744B; 루트 가용 4,422,942,720B, 사용률 98% | `free -b`, `df -B1` |
| 대상 호스트 용량 | RAM 25,199,181,824B, 가용 20,760,141,824B; 루트 가용 225,146,679,296B, 사용률 28% | `ssh jinah244 free -b`, `df -B1` |
| 대상 런타임 | PostgreSQL 16.15 실행; Docker 명령 없음; 80/443 listener 없음; Cloudflare tunnel·biseo 서비스 실행 | `docker --version`, `systemctl`, `ss -ltnp` |

조사 작업트리 SHA와 운영 이미지 SHA가 다르므로 구현 기준 SHA는 M1 착수 전에 하나로 고정해야 한다. 현 dirty 작업과 untracked 상위 PRD를 운영 이미지에 포함된 것으로 간주하지 않는다.

## 2. 포함 manifest

| 영역 | 포함 대상 | 실측 기준선 | 식별·검증 방법 |
|---|---|---|---|
| 인증 사용자 | 아래 2개 tenant의 활성 membership으로 연결된 사용자 | tenant 2, 활성 membership 7, distinct user 6, consent 3, invite 0, usage override 0 | ID 보존 dump + 사용자별 로그인/권한 대조 |
| 조직 | `15055cac-71b0-45ec-b714-7093dde189ff`(열정국밥, slug `tenant-32`), `d1695f15-6b68-4929-bc8d-646827363ff9`(라일론, slug `tenant-42`) | 양쪽 모두 active; 활성 member 각각 2/5 | `tenants`, `tenant_memberships` 필터와 FK 대조 |
| 사업자 | 열정국밥 4개 + 라일론 1개 | `biz-eonni-naengmyeon`, `biz-junghwa`, `biz-mia`, `biz-sungshin`, `biz-lylon-e2e` | `obys.yeoljeong_businesses` ID·tenant_id 보존 |
| 인증 스키마 | 사용자·조직·membership·invite·consent 및 실제 참조 함수/정책 | 전체 AADS 기준 users 108, tenants 108, memberships 148이나 위 target subset만 데이터 이전 | 스키마/FK/함수/RLS manifest 후 subset dump |
| 업무 DB | PostgreSQL `obys` public 40 tables | DB 40,557,927B; sales 3,813, settlements 3,551, reviews 5,075, ads 2,664, collection status 4,979, quarantine 11,029 | table count, PK/FK/sequence, tenant별 count·amount, dump checksum |
| 정적 화면 | `app/static/apps/obys` | 5,908,287B, 28 files, manifest hash `03453a14f2679788cfefb7795cde46181574122d7e64a034528e1846545ddae1` | 파일별 SHA-256 후 route smoke |
| 업무 파일·상태 | `app/data/yeoljeong_finance` 전체 | 65,898,759B, 952 files, manifest hash `13d681a253f16c9b117248f3aa4b3e2389e18cc358dd92a5ebba3735c8f2ecf5` | 파일별 SHA-256·DB 메타 교차검증 |
| 브라우저 상태 참조 | `browser-bridge-state`의 승인된 오비서 상태만 | 전체 디렉터리 1,907,913B; 포함 파일 선별 미확정 | 계정/사이트별 소유·만료·재인증 검수 |
| 키 참조 | Vault 암호화 키와 인증/JWT 발급·검증 키의 **참조·버전·소유권** | 운영 컨테이너 `VAULT_ENCRYPTION_KEY` env 미설정, `/app/app/.vault.key` read-only mount의 소스 파일 44B | 원문 복사/문서화 금지; target secret store에 주입 후 암복호 round-trip |
| API | 현재 오비서 앱의 153 METHOD+PATH | auth, finance, ACCT read, inventory, workspace 포함 | OpenAPI snapshot + 인증/권한별 contract test |
| 워커 | 배달·은행 수집, PC/Browser Bridge 의존, lock/checkpoint/상태 | compose 정의는 있으나 `yeoljeong-finance-worker` 컨테이너 미실행 | 단일 owner/epoch + 멱등 재실행 + 마지막 체크포인트 |
| 기존 ACCT 연동 | 진아서버 `acct` DB를 같은 호스트에서 read-only로 사용 | DB 1,768,496,151B; 39 tables, RLS 21/39·FORCE RLS 21/39; 5 tenant/company mappings | app role/RLS 문맥·동일 필터 count/amount 검증 |

## 3. 제외 manifest

| 제외 대상 | 이유 | 경계 검증 |
|---|---|---|
| AADS 전체 DB 13,534,453,095B | 오비서와 무관한 채팅·러너·목표·다른 tenant를 진아서버로 복제하지 않음 | export allowlist와 target table 목록 비교 |
| AADS 채팅·Pipeline Runner·프롬프트/모델 운영 데이터 | 오비서 핵심 업무의 독립 런타임이 아님 | AADS 목적지 차단 상태에서 오비서 핵심 기능 성공 |
| 대상 `/srv/biseo` 기존 파일 6,350,719,849B·71,944 files의 재복사 | 이미 진아서버 정본이며 오비서 payload가 아님 | 기존 파일은 읽기 연결·해시/권한 검수만 수행 |
| 대상 `acct` DB 재이전 | 이미 진아서버에 존재 | DB 자체 이동 대신 로컬 접속/RLS/성능 검증 |
| 무관 tenant 106개 및 그 사용자 데이터 | 최소권한·개인정보 범위 제한 | target tenant ID allowlist 외 row 0 |
| 구 JWT·세션·쿠키의 영구 호환 | 독립 issuer/key 경계를 약화 | 전환 후 구 토큰 거부·재로그인 검증 |
| 구 서버 폐기 | M6 안정화와 별도 CEO 승인 전 유지 | read-only/rollback 보존 상태 확인 |

## 4. 복사·복원·RTO 예산

| 항목 | 실측 | 예산/판정 |
|---|---|---|
| 네트워크 복사 | 64MiB random payload 1.60초, 40.0MiB/s; source/target SHA-256 일치 | 확정 기준선. 운영 payload copy budget 5분(해시 검증 포함) 제안 |
| 현재 이전 payload | OBYS DB 40,557,927B + 업무 파일 65,898,759B + 정적 화면 5,908,287B + Browser state 1,907,913B + key reference 44B = 114,272,930B; 인증 subset dump 제외 | 용량 확정. 실제 전체 복사는 미실행 |
| OBYS dump | custom dump 3,024,368B, 2.33초; 진아서버 전송 0.60초; SHA-256 `7820a5809aa7aebf8523c190e8d10c24e09ea301e87be0e996f80da62d274d5a` | dump/copy 기준선 확정 |
| dump 검사 | target `pg_restore --list` 0.05초 | archive 가독성 확인 |
| PostgreSQL 실제 복원 | 진아서버 PostgreSQL 16 격리 임시 cluster에 1.27초; 복원 DB 41,794,583B·40 tables; sales 3,813, settlements 3,551, reviews 5,075, ads 2,664, collection status 4,979, quarantine 11,029로 원본과 일치 | DB restore 기준선 확정. 임시 cluster·dump·port 55432 정리 확인 |
| FK 상태 | 원본과 복원본 모두 `NOT VALID` FK 4개 | restore 신규 결함은 아니나 M1에서 제약별 검증 필요 |
| 쓰기 동결 | 미실행 | **미확정**. 인증+업무+파일+worker 공통 장벽 리허설 필요 |
| RTO | 데이터 구간 실측은 copy 1.60초/dump 2.33초/restore 1.27초. target runtime이 없어 앱 기동·route/E2E는 미측정 | **30분 engineering ceiling 제안**: copy 5분 + restore 5분 + 앱 기동 5분 + smoke/cutover 10분 + rollback reserve 5분. 담당 승인 전 미확정 |
| RPO | 합격 기준은 승인된 쓰기 유실 0건 | 목표값이며 현재 실적 아님 |

네트워크 실측만으로 계산한 114,272,930B의 순수 전송시간은 약 2.7초이다. 실제 DB restore도 1.27초였지만 파일 열거·쓰기 동결·인증 subset 복원·서비스 기동·화면 검증은 포함하지 않는다. 따라서 합산 수치를 RTO 실적으로 사용하지 않고, 위 30분을 검증 전 engineering ceiling으로만 둔다.

## 5. 현재 결함과 차단 조건

| 우선순위 | 결함 | 근거 | M1 이전 차단 조건 |
|---|---|---|---|
| P0 | source 루트 디스크 98%, 가용 4,422,942,720B | `df -B1` | 백업/이미지 빌드 여유 확보 전 source 작업 금지 |
| P0 | `YEOLJEONG_FINANCE_DATABASE_URL` 미설정, `DATABASE_URL`은 AADS, `OBYS_DATABASE_URL`은 `obys` | 운영 컨테이너 env 계약(값 비노출) | 모든 업무 경로를 명시적 target DSN으로 고정하고 fallback 제거 |
| P0 | 자동수집 worker 미실행 | compose에는 정의, `docker ps -a`에는 컨테이너 없음 | 현재 수집 공백 원인 확인 및 target 단일 실행권 설계 |
| P0 | target에 Docker/Nginx/80·443 listener 없음 | `docker --version`, `ss` | blue/green 계약을 만족할 target runtime·proxy 설치안 확정 |
| P0 | end-to-end RTO 미측정 | DB restore 1.27초는 측정했으나 target runtime 부재 | 앱 기동·route/E2E·cutover/rollback 포함 통합 시간 측정 |
| P0 | goal은 `paused`, PRD는 `active` | `goals`, `goal_documents` DB 조회 | 승인 주체가 목표 상태와 PRD 상태를 일치시킴 |
| P0 | M0~M6 `owner_session_id` 전부 NULL | `milestones` DB 조회 | 담당 세션 확정 및 인수 응답 기록 |
| P1 | 목표 조직명 중복·테스트 tenant 혼재 | 열정국밥 운영관리 28 rows(현재 active 8), 라일론테스트상사 7 rows(현재 active 2) | canonical tenant 2개 allowlist를 승인하고 나머지 제외 증거 남김 |
| P1 | 조사 HEAD·origin·운영 SHA가 서로 다름 | Git/runtime image 실측 | 단일 release SHA와 깨끗한 worktree 확정 |
| P1 | PostgreSQL 15→16 버전 차이 | source 15.17, target 16.15 | extension/collation/RLS/function 호환 restore 검증 |

## 6. 역할과 개별 PRD 확정 상태

| 단계 | DB owner role | owner session | 개별 PRD | 상태 |
|---|---|---|---|---|
| M0 | CTO | NULL | 상위 PRD v1.1의 M0 절만 존재 | 미확정 |
| M1~M4 | Developer | 전부 NULL | milestone별 문서 연결 필드/문서 없음 | 미확정 |
| M5~M6 | CTO | 전부 NULL | milestone별 문서 연결 필드/문서 없음 | 미확정 |

현재 `goal_documents`에는 목표 단위 PRD 1건만 있고 현재 스키마에는 `milestone_id`가 없다. milestone별 PRD 연결 작업 `runner-1b2b0474`가 queued 상태이므로 우회 DB 쓰기를 하지 않는다. 목표 소유 CTO 세션 `acc75e55-0917-4a01-9a01-000000000002`에 PRD 승인과 역할 배정을 질의했으며 relay `462df734-7d1e-4f0f-ab83-60f89977c4c2` 응답 대기 중이다.

## 7. M0 완료 판정

| 완료 기준 | 현재 상태 | 완료에 필요한 증거 |
|---|---|---|
| 포함/제외 manifest | 충족 | 본 문서 2·3절 |
| 소스 SHA | 충족하되 release SHA 선택 미확정 | 조사/운영/target SHA 기록 + M1 release SHA 결정 |
| 용량·복사 기준선 | 충족 | DB/files bytes, 64MiB copy, dump/copy checksum |
| 실제 DB 복원 | 충족 | 진아서버 PG16 격리 restore 1.27초, 핵심 row count 일치 |
| 전체 RTO 예산 | 부분 충족 | 30분 engineering ceiling 담당 승인 + 앱 기동/API smoke/E2E 실측 |
| 역할별 담당 | 미충족 | owner_session_id 또는 공식 인수 기록 |
| 개별 PRD 확정 기록 | 미충족 | milestone별 PRD status/승인 기록 |

따라서 이 문서는 M0의 **실측 기준선 산출물**이지만, 마일스톤 완료 신고 근거로는 아직 부족하다. 실제 복원·RTO, 역할 인수, 개별 PRD가 확정될 때 M0 완료를 신고한다.
