# 오비서 진아서버 이전 — M0 이전 범위·기준선 확정

| 항목 | 내용 |
|---|---|
| 문서 ID | OBYS-MIGRATION-JINAH-M0-BASELINE |
| 상위 PRD | `docs/prd/20260923_OBYS_FULL_MIGRATION_JINAH_PRD.md` v1.2 |
| 목표 / 마일스톤 | goal `df479771-f250-4a11-90a3-220432da2bfa` / M0 `a60a4939-4599-4afd-ae6e-1fdc65098f52` |
| 측정 시각 | 2026-09-23 18:33~18:47 KST |
| 측정 대상 | 출발 contabo116(5.104.86.116) · 도착 jinah244(5.104.85.244) |
| 문서 기준 커밋 | `3e1789f5511d595c84a24372f0fc06218b7f8c59` (origin/main) |
| 성격 | 실사·리허설 기록. 운영 이전은 미실행이며 이 문서로 이전 완료를 선언하지 않는다. |

PRD 16~17절이 M0의 진아서버 자원·API manifest·인증 스키마 의존·PG15→16 복원까지 기록했고,
17.5절이 **파일 저장소 해시·용량**과 **전환 시간·복원 예산**을 미실행으로 남겼다.
이 문서는 그 두 항목을 실측으로 닫고, 남은 M0 완료 기준(소스 SHA·담당·개별 PRD)의
확정/미확정을 구분한다.

---

## 1. M0 완료 기준 대비 판정

| 완료 기준 | 상태 | 근거 |
|---|---|---|
| 포함/제외 manifest | 확정 | 2절 |
| 소스 SHA | **미확정** | 3절 — 운영 프로세스가 bind mount 된 dirty 워킹트리를 실행 중 |
| 용량·복사/복원 예산 | 확정 | 4절 — 데이터 동기화 구간 12.06초 실측 |
| RTO 예산 | **부분 확정** | 4.3절 — 데이터 구간만 실측. 앱 기동·health·라우팅 전환 미측정 |
| 역할별 담당 | **부분 확정** | 6절 — role_key 는 있으나 7건 모두 owner_session_id NULL, 목표는 paused |
| 개별 PRD 확정 기록 | **미확정** | 6절 — M1~M6 개별 PRD 미작성, 정본 v1.1 vs 커밋본 v1.2 불일치 |

---

## 2. 포함/제외 manifest

### 2.1 실행 단위 (포함)

| 구성 | 실측 |
|---|---|
| API 프로세스 | `yeoljeong-finance` 컨테이너, `uvicorn app.yeoljeong_main:app --port 8080` → 호스트 `127.0.0.1:8110` |
| 공개 경로 | `fb.newtalk.kr` → 8110 |
| 컨테이너 상태 | Up 40분 (healthy), 이미지 `sha256:e829f404…` |
| 수집 워커 | `docker-compose.prod.yml:372` 에 `yeoljeong-finance-worker` 정의 — **컨테이너 미존재** (`docker ps -a` 0건) |

### 2.2 API (PRD 16.3 실측 승계)

`openapi.json` 기준 **116개**. prefix 별: `yeoljeong-finance` 72 · `auth` 13 ·
`acct-purchase` 11 · `workspaces` 10 · `yeoljeong-inventory` 10.

미등록(=이전 기본 범위 **제외**): `yeoljeong-dashboard`, `yeoljeong-accounting`,
`yeoljeong-ops`, `acct-sales`. 이 4개는 aads-server 가 서빙하는 레거시 화면 전용이다.

### 2.3 DB (포함)

| DB | 크기 | 객체 | 판정 |
|---|---:|---|---|
| `obys` (업무) | 39 MB / 40,557,927 B | public 테이블 40개 | 전량 이전 |
| `aads` (인증) | 13 GB | 인증 7테이블만 | **선별 추출** — 전량 복원 금지 |

`obys` 상위 테이블 행수: `yeoljeong_delivery_quality_quarantine` 11,029 ·
`yeoljeong_delivery_reviews` 5,075 · `yeoljeong_delivery_collection_status` 4,979 ·
`yeoljeong_delivery_sales` 3,813 · `yeoljeong_delivery_settlements` 3,551 ·
`yeoljeong_delivery_ads` 2,664 · `ddl_audit_log` 86.

인증 7테이블 (2026-09-23 18:41 KST):

| 테이블 | 행수 | PRD 17.1 (같은 날 오전) | 증감 |
|---|---:|---:|---:|
| `saas_users` | 108 | 106 | +2 |
| `tenants` | 108 | 106 | +2 |
| `tenant_memberships` | 148 | 144 | +4 |
| `saas_user_consents` | 48 | 42 | +6 |
| `tenant_plan_limits` | 4 | 4 | 0 |
| `tenant_invites` | 0 | 0 | 0 |
| `tenant_usage_overrides` | 0 | 0 | 0 |

**공유 인증 DB 는 반일 만에 14행이 늘었다.** 전환 시 공통 쓰기 장벽 없이
덤프만 뜨면 그 사이 가입·동의·멤버십이 유실된다(PRD 9절 5단계의 실측 근거).

### 2.4 파일 (포함)

| 대상 | 실측 |
|---|---|
| `app/data/yeoljeong_finance` | **952개 파일 / 65,898,759 B (62.8 MiB)** |
| `browser-bridge-state` | 7개 파일 / 1,907,913 B |
| 해시 manifest | SHA-256 952행, manifest 자체 digest `c28c59d7cb6e97d40f814f1001f162af39b903602299f04351017113f295b23d` |

manifest 는 `find … -print0 | sort -z | xargs -0 sha256sum` 로 생성했다(경로 정렬 고정).

이 디렉터리에는 DB 와 **이중 보관**되는 JSON 이 있다 —
`delivery_sales.json`(3.2MB), `delivery_collection_status.json`(7.2MB),
`delivery_settlements.json`(2.6MB), `delivery_ads.json`(1.8MB), `bank_accounts.json`.
같은 사실이 `yeoljeong_delivery_*` 테이블에도 있으므로 **이전 후 어느 쪽이 정본인지
M3 에서 확정해야 한다.** 한쪽만 옮기면 화면과 수집 결과가 갈린다.

`.bak_*` 접미 백업본(예: `delivery_collection_status.json.bak_aads_20260825_…` 1.8MB)도
952개에 포함돼 있다. 이전 대상에서 제외할지는 M3 결정 사항이며, 제외하면 manifest digest 가 바뀐다.

### 2.5 비밀·키 참조 (이름만 기록, 값 미기재)

`JWT_SECRET_KEY` · `SECRET_KEY` · `VAULT_ENCRYPTION_KEY` · `OBYS_DATABASE_URL` ·
`OBYS_ACCT_COMPANY_MAP` · `OBYS_JOURNAL_WRITE_FROZEN` · `YEOLJEONG_FINANCE_DATABASE_URL` ·
`YEOLJEONG_FINANCE_DATA_DIR` · `YEOLJEONG_BAEMIN_BROWSER_WORK_KEY` ·
`YEOLJEONG_BANK_AUTO_COLLECT_LOCK_PATH` · `YEOLJEONG_DELIVERY_PC_AGENT_ID` ·
파일 `app/.vault.key` (44 B, mode 0600).

운영 컨테이너 실측 결과 **`YEOLJEONG_FINANCE_DATABASE_URL` 이 빈 값**이다.
따라서 compose 기본값에 의해 `DATABASE_URL` = AADS DB 로 주입되고,
`OBYS_DATABASE_URL` 만 `…@aads-postgres:5432/obys` 를 가리킨다.
PRD 8절이 요구한 "역할별 명시 DSN" 은 아직 충족되지 않았다.

### 2.6 워커·자동수집 (포함, 단 현재 구조가 PRD 전제와 다름)

- compose 의 `yeoljeong-finance-worker` 는 **실행되고 있지 않다.**
- API 컨테이너 안에 프로세스는 uvicorn 하나뿐인데,
  `.bank_auto_collect.lock` 이 18:15 KST 에 갱신됐고 내용은 PID `8131` 이다.
  → **수집이 API 프로세스 내부에서 돌고 파일 lock 으로만 배타 제어된다.**
- 두 호스트 병행 구간에서 파일 lock 은 상호배제하지 못한다(PRD 6절 경고가 실제 구조와 일치).
  COLLECT-01 의 단일 실행권은 코드 변경 없이는 충족 불가다.
- 기존 결함(이전과 무관, baseline 으로 분리): 신한은행 수집이
  `PC_AGENT_LOGIN_REQUIRED` / `LOGIN_SUCCESS_NOT_OBSERVED` 로 실패 중
  (`browser_collection_stage_logs.jsonl`, 최근 17:26 KST).

### 2.7 대상 조직·사업자 닫힌 집합 (포함)

| 조직 ID | 이름 | 멤버십 |
|---|---|---:|
| `15055cac-71b0-45ec-b714-7093dde189ff` | 열정국밥 운영관리 | 2 |
| `d1695f15-6b68-4929-bc8d-646827363ff9` | 라일론테스트상사 | 5 |

사업자 5개 / 지점 5건:

| business_id | 사업자 | 소속 조직 |
|---|---|---|
| `biz-junghwa` | 열정국밥 중화점 | 15055cac… |
| `biz-sungshin` | 열정국밥 성신여대점 | 15055cac… |
| `biz-mia` | 열정국밥_미아점 | 15055cac… |
| `biz-eonni-naengmyeon` | 언니냉면 | 15055cac… |
| `biz-lylon-e2e` | 주식회사 라일론 | d1695f15… |

고유 사용자 **6명**. 이 6명은 **다른 AADS 조직에 멤버십 6건**을 추가로 갖고 있다.

→ PRD 6절의 "공유 회원의 다른 AADS 조직 멤버십은 제외" 조항이 실제로 발동된다.
**PRD 17.2 리허설이 복원한 인증 7테이블 전량(108/108/148/48/4)을 운영 인증 DB 로 쓰면
타 조직 6건이 함께 넘어간다.** M2 는 조직 2개를 출발점으로 한 추출기를 써야 한다.

### 2.8 제외

AADS 채팅·Runner 플랫폼·다른 프로젝트, `aads` DB 의 인증 7테이블 외 전부,
인증 테이블로 들어오는 인바운드 FK 87건(전부 AADS 내부 테이블, PRD 17.1),
레거시 화면 전용 4개 prefix, 대상 6명의 타 조직 멤버십 6건,
`/root/.ssh`·Docker 소켓·AADS 전체 `.env`(PRD 8절).

---

## 3. 소스 SHA — 미확정

| 후보 | 값 |
|---|---|
| origin/main | `3e1789f5511d595c84a24372f0fc06218b7f8c59` |
| 운영 컨테이너 이미지 | `sha256:e829f4042481e553a7dbf01f4ac659326adaa2a2b91e039557bf81f03c78db6e` |
| 실제 실행 코드 | **호스트 워킹트리 `/root/aads/aads-server/app` (bind mount rw)** |

compose 가 `app/` 을 rw 로 bind mount 하므로 컨테이너 이미지 digest 는 실행 코드를
증명하지 못한다. 그 워킹트리는 origin/main 대비 **ahead 5 / behind 52**, dirty 18개 이상이며
그중 오비서가 쓰는 파일이 4개다 — `app/yeoljeong_main.py`, `app/api/obys_workspaces.py`,
`app/services/obys_upload_service.py`, `app/api/acct_source_ledger.py`.

**실증:** 컨테이너 안에서 읽은 `obys_upload_service.UPLOAD_ROOT` 는 고정 문자열
`app/data/yeoljeong_finance/uploads/ledgers` 인데, origin/main 의 같은 줄은
`os.getenv("OBYS_UPLOAD_ROOT", …)` 다. 즉 **운영 프로세스는 origin/main 코드를 실행하고 있지 않다.**

→ 이전 기준선 SHA 는 지금 고정할 수 없다. M1 의 첫 작업은 bind mount 제거와
릴리스 SHA 고정이며(PRD 8절), 그 전까지 "이 SHA 를 옮겼다" 고 보고할 수 없다.

---

## 4. 용량·복사/복원·RTO 예산

### 4.1 복사/복원 실측 (contabo116 → jinah244, 2026-09-23 18:43~18:46 KST)

| # | 단계 | 실측 | 산출물 |
|---|---|---:|---|
| 1 | `obys` `pg_dump -Fc` | 6.50 s | 3,024,368 B |
| 2 | 인증 7테이블 `pg_dump -Fc --no-owner --no-acl` | 1.70 s | 55,923 B |
| 3 | 덤프 2개 scp → 244 | 0.62 s | — |
| 4 | `obys` `pg_restore` (PG15→16) | 1.20 s | 40테이블 |
| 5 | 인증 `pg_restore` | 0.17 s | 5테이블 |
| 6 | 파일 SHA-256 manifest 952개 | 0.34 s | manifest |
| 7 | `rsync -a --delete` 952파일 65.9 MB | 1.53 s | — |
| **합계** | **데이터 동기화 구간** | **12.06 s** | — |

### 4.2 복원 정합성 검증

| 검증 | 결과 |
|---|---|
| `obys` 테이블 수 | 원본 40 = 대상 40 |
| `obys` 행수 (상위 8테이블) | 11,029 / 5,075 / 4,979 / 3,813 / 3,551 / 2,664 / 86 / 58 — 원본과 전부 동일 |
| 인증 행수 | `saas_users` 108 · `tenants` 108 · `memberships` 148 · `consents` 48 · `plan_limits` 4 — 원본과 동일 |
| 파일 해시 대조 | **952 / 952 성공, 실패 0** |
| PG 15 → 16 | 이 범위에서 호환 (PRD 17.2 재확인) |

검증 후 양쪽 서버의 덤프·복사본·리허설 DB(`obys_m0_rto`, `obys_auth_m0_rto`)를 삭제했다
(PRD 17.4 동일 처리). 덤프에는 비밀번호 해시가, 파일에는 계좌 정보가 들어 있다.

### 4.3 예산안

| 항목 | 기준값 | 목표 예산 | 측정 명령 | 상태 |
|---|---|---|---|---|
| 데이터 동기화 | 12.06 s 실측 | 5분 | 4.1절 재실행 | 확정 |
| 앱 기동 + readiness 200 | — | 2분 | `systemctl start obys-api@candidate` → `/health/ready` | **미측정** |
| 라우팅 전환 + routed health | — | 1분 | nginx reload → 공개 health | **미측정** |
| 쓰기 동결 총시간 (RTO) | — | **10분 이내 제안** | 위 3항 합산 | **부분 확정** |
| 백업 복원 시간 | 1.37 s(덤프+복원 실측 합) | 5분 | 4.1절 #1·#4 | 확정 |
| RPO | — | 승인된 쓰기 유실 0 | 전환 원장 대조 | 목표값(미실측) |

**용량은 제약이 아니다.** 진아서버 210GB 여유 대비 이전 대상은 DB 39MB + 파일 66MB ≒ 105MB 다.
전환 시간을 지배하는 것은 데이터 복사가 아니라 **쓰기 동결·검증·라우팅 절차**다.

### 4.4 진아서버 현황 재실측 (2026-09-23 18:36 KST)

| 항목 | 값 | 판정 |
|---|---|---|
| CPU | 8 vCPU | 여유 |
| 메모리 | 24,031 MB 중 가용 19,942 MB | 여유 |
| 디스크 | 290 GB 중 210 GB 여유 (28% 사용) | 여유 |
| PostgreSQL | 16.15 (Ubuntu 24.04) | 사용 가능 |
| Docker / Nginx | **둘 다 미설치** | M1 작업량의 본체 |
| 기존 DB | `acct` 1,687 MB 외 리허설 DB 7종 | `acct` 보존 필수 |

PRD 16.1 수치와 일치한다(디스크 사용 80G/28% 동일). 자원 제약 없음을 재확인했다.

---

## 5. 실측으로 드러난 차단 요인

| # | 차단 요인 | 근거 | 영향 | 귀속 |
|---|---|---|---|---|
| 1 | 운영 코드가 dirty bind mount | 3절 UPLOAD_ROOT 대조 | 기준선 SHA 확정 불가, 이전본과 운영본 불일치 | M1 |
| 2 | 인증 전량 복원 시 타 조직 유출 | 대상 6명이 타 조직 멤버십 6건 보유 | AUTH-02·G1 위반 | M2 |
| 3 | 공유 인증 DB 실시간 변동 | 반일 +14행 | 쓰기 장벽 없으면 가입·동의 유실 | M5 |
| 4 | 수집이 API 프로세스 내부 + 파일 lock | 2.6절 | 두 호스트 병행 시 중복 수집 | M3 |
| 5 | 업무 파일과 DB 이중 보관 | 2.4절 | 정본 미확정 시 화면/수집 결과 분기 | M3 |
| 6 | `YEOLJEONG_FINANCE_DATABASE_URL` 빈 값 | 2.5절 | 인증 DB 로 폴백, 역할별 DSN 미충족 | M1 |
| 7 | 신한은행 수집 실패(기존) | 2.6절 | 이전 성패와 무관한 baseline 결함 | 별도 추적 |

---

## 6. 역할별 담당과 개별 PRD

`milestones` 테이블 직접 조회 (2026-09-23 18:52 KST):

| 단계 | `owner_role_key` | `owner_session_id` | `dispatched_session_id` |
|---|---|---|---|
| M0 이전 범위·기준선 확정 | CTO | **NULL** | `8ad08cc2…` (본 세션) |
| M1 독립 실행 기반·설정 분리 | Developer | **NULL** | NULL |
| M2 인증·조직·권한 이전 검증 | Developer | **NULL** | NULL |
| M3 업무 DB·파일·자동수집 이전 검증 | Developer | **NULL** | NULL |
| M4 전체 메뉴·독립성·복구 통합 검수 | Developer | **NULL** | NULL |
| M5 승인된 운영 전환·배포 인증 | CTO | **NULL** | NULL |
| M6 안정화·복원·운영 인계 | CTO | **NULL** | NULL |

**역할 키는 배정돼 있으나 실제 담당 세션은 7건 전부 비어 있다.** M0 만 본 세션으로
dispatch 됐고 나머지는 dispatch 조차 되지 않았다. 목표 `df479771…` 의 상태는 **`paused`** 다.

PRD 12절의 Backend/Ops/QA/Data 배정은 문서 자신이 "제안" 이라고 명시하며 DB 에 반영되지 않았다.

**개별 PRD: 미작성.** M1~M6 각각의 PRD 는 존재하지 않고 상위 PRD 의 절로만 기술돼 있다.
추가로 **정본 버전이 어긋나 있다** — `goal_documents` 의 active/is_latest 는 v1.1.0 인데
커밋 `4313e0c1` 에 들어간 문서는 v1.2 다. 어느 쪽이 정본인지 확정되지 않았다.

→ 목표 재활성화 전에 ① 정본 버전 확정 ② M1~M6 개별 PRD 작성 ③ 담당 세션 배정이
선행돼야 한다. 이 셋은 M0 의 측정 대상이 아니라 **CEO/주도 CTO 의 결정 사항**이다.

## 7. 미확정 항목과 닫는 방법

| 미확정 | 닫는 방법 | 귀속 |
|---|---|---|
| 소스 SHA | bind mount 제거 후 릴리스 SHA 고정 | M1 |
| 앱 기동·라우팅 전환 시간 | 진아서버 후보 슬롯 기동~routed health 계측 | M1 |
| 파일 정본(JSON vs DB) | 두 저장소 동일 기간 건수·금액 대조 | M3 |
| `.bak_*` 파일 이전 여부 | 포함/제외 결정 후 manifest digest 재생성 | M3 |
| 인증 함수·뷰·트리거 확정 목록 | `pg_dump -t` 가 함수를 빼는 문제(PRD 17.3) 포함 재점검 | M2 |
| 개별 단계 PRD | M1~M6 PRD 작성 | M1 착수 전 |
| 비용($) | 신규 서버·백업 저장소 필요 여부 확정 후 산정 | 미측정 |

---

## 8. 재현 명령

```bash
# 용량·파일
du -sb /root/aads/aads-server/app/data/yeoljeong_finance
find /root/aads/aads-server/app/data/yeoljeong_finance -type f -print0 \
  | sort -z | xargs -0 sha256sum > /tmp/obys_files.sha256

# DB 크기·행수
docker exec aads-postgres psql -U aads -d postgres -tAc \
  "SELECT datname, pg_size_pretty(pg_database_size(datname)) FROM pg_database WHERE datname IN ('aads','obys')"

# 대상 조직 닫힌 집합
docker exec aads-postgres psql -U aads -d aads -tAc \
  "SELECT count(*) FROM tenant_memberships WHERE user_id IN (
     SELECT DISTINCT user_id FROM tenant_memberships WHERE tenant_id IN
     ('15055cac-71b0-45ec-b714-7093dde189ff','d1695f15-6b68-4929-bc8d-646827363ff9'))
   AND tenant_id NOT IN
     ('15055cac-71b0-45ec-b714-7093dde189ff','d1695f15-6b68-4929-bc8d-646827363ff9')"

# 실행 코드가 릴리스인지 확인 (3절)
docker exec yeoljeong-finance python3 -c \
  "import app.services.obys_upload_service as m; print(m.UPLOAD_ROOT)"
git show origin/main:app/services/obys_upload_service.py | sed -n 24p
```

## 교훈

이전 대상의 **크기**는 제약이 아니었다(DB 39MB + 파일 66MB, 동기화 12초).
실제 제약은 셋이다 — 운영 코드가 릴리스가 아니라 dirty 워킹트리라는 것,
공유 인증 DB 가 이전 작업 중에도 계속 변한다는 것, 수집이 분리된 워커가 아니라
API 프로세스 안에서 파일 lock 으로 돈다는 것. 모두 "용량 산정" 으로는 보이지 않고
실행 중인 프로세스를 직접 들여다봐야 나왔다.

특히 compose 에 `yeoljeong-finance-worker` 가 정의돼 있다는 사실만 읽고
"워커가 분리돼 있다" 고 적었다면 M3 의 단일 실행권 설계가 통째로 틀렸을 것이다.
정의 파일이 아니라 `docker ps -a` 와 lock 파일의 PID 를 봐야 했다.
