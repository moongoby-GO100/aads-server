# ACCT-CAFE24-HELD-VERIFY-20261003-R23 — 결과 보고

- 실행: runner-ef3ebe1d, 2026-10-03 12:05~12:20 KST. 변경 파일은 이 보고서 1건뿐이다.
- 판정: **운영 라우팅·origin·방화벽·인증서·300초 안정성은 검증됨. 자료 무누락·로그인 후 화면은 검증되지 않았고, 전체 이전은 완료가 아니다.**
- 이 세션이 하지 않은 것: 코드 수정, commit/push, 배포, nginx/apache reload, apply-origin/apply-edge, 방화벽 변경, 진아서버(5.104.85.244) 접속, 운영 DB 쓰기(cafe24는 SELECT만). 아래 §9 에 예외 1건(AADS DB handover)을 따로 적었다.
- 시각은 모두 KST. 세션 로컬 시계는 CEST(KST−7h)여서 모니터 로그의 시각과 7시간 차이가 난다.

## 0. 사전 조회 (읽기 전용)

| 항목 | 실측 |
|---|---|
| 이 worktree HEAD / `origin/main` | 둘 다 `9ecf4232f059…` (`git ls-remote` 일치, status clean) |
| 활성 job (DB `pipeline_jobs`) | AADS running 1 = 이 job, AADS queued 1 = `runner-c856e5ff`(클로브 UI, 대상 파일 `app/static/apps/obys/*`), ACCT queued 1 = `runner-52a46b5e`(`ACCT-DOC-STORAGE-INVENTORY`, 어제 18:46 등록), GO100 awaiting_approval 1 |
| 동일 파일 소유권 | `chat_workspace_change_ledger` 에 `ACCT_CAFE24_HELD`·`CAFE24-HELD` 로 걸리는 행 0. 클로브 job 과 파일 겹침 없음 |
| `deploy_runs` queued/running | 0 (최근 `5566` GO100 success, `5562` AADS api failed 10:13 — 이 작업과 무관) |
| 중복 | 같은 TASK_ID 의 다른 job 없음. 중복 아님으로 진행 |

`error_book.py match` 는 `psql` 이 이 세션에 없어 실행하지 못했다. 오류사전 조회는 **미수행**이다. canonical `run_preflight` 도 호출하지 않았다(asyncpg 없음). 착수 시 PROCEED 는 지시서가 전달한 값이며 내가 재현한 것이 아니다.

## 1. STEP 0 분류

- 유지: cafe24 앱·DB·Apache vhost·방화벽·edge `fb.conf`·`deploy_acct_fb_cutover.sh`·`cutover_fb_cafe24.sh`·R10/R20 증거 전부 (읽기만)
- 수정: 없음
- 신규: 이 보고서
- 삭제: 없음. cafe24 `/tmp` 에 임시 파일 3개를 만들었다가 같은 명령에서 지웠다(내가 만든 scratch).

## 2. 운영 경로 인증 — 검증됨

공개 `fb.newtalk.kr` = Cloudflare → contabo116 edge nginx(`/etc/nginx/conf.d/fb.conf`) → `https://114.207.244.86`(cafe24 Apache `00-zz-fb.newtalk.kr.conf`) → `172.28.50.2:8111`.

| 검증 | 결과 |
|---|---|
| 고유 nonce 상관 | `X-Probe`/쿼리 `probe=r23-1790996800-14109` 요청이 cafe24 `fb.newtalk.kr-access.log` 에 `5.104.86.116`(edge) / `GET /health/live` / 200 으로 기록됨 (12:06:40). 공개 응답이 cafe24 origin 에서 나온다는 직접 증거 |
| edge 설정 | health·`/api/v1/`·`/static/`·obys index 모두 `https://114.207.244.86`, `proxy_ssl_verify on`, depth 3. 진아 IP 참조 0 |
| origin 컨테이너 | `acct-app-candidate-r8`, image `acct-candidate:b05d0a8b`, image id `sha256:71e5e811c220…`, 라벨 revision `b05d0a8b` = 저장소 커밋 `b05d0a8b`(R6 로그인 503 수정). restarts 0, OOM false |
| upstream 소유 | `172.28.50.2` 는 `acct-pg` 컨테이너 IP 다. app 컨테이너(r8, r5)는 `network_mode: container:<acct-pg>` 로 같은 netns 를 쓴다. 호스트에는 8111 리스너가 없다. app 명령은 `uvicorn app.obys_standalone:create_app … --port 8111 --workers 1` |
| 공개 응답 | `/health/live` 200 `{"status":"ok","service":"yeoljeong-finance"}`, `/` 302 → obys index, `/static/apps/obys/index.html` 200, `/api/v1/auth/me` 401, `/api/v1/workspaces` 401, `/api/v1/auth/login` GET 405 (인증 없이 열리는 업무 API 없음) |
| 방화벽 | `ufw inactive`. `INPUT` 은 `CF-WEB` 로 80/443 을 보내고, `CF-WEB` 첫 규칙이 `-s 5.104.86.116/32 --dport 443 ACCEPT`(comment `fb-edge-contabo116`), 마지막이 `-j DROP`. 이 edge 규칙은 정확한 /32 + 443 한정이다. 다만 `CF-WEB` 에는 Cloudflare 대역과 `68.183.183.11/32` 가 포트 무관 ACCEPT 로 이미 있다(기존 설정, 이번 전환 이전부터) |
| origin 인증서 | 체인 3장 전송, `Verification: OK`, 만료 2026-12-25. edge 는 이 체인으로 depth 3 검증 |
| 진아 참조 | r8 컨테이너 env·로그(500줄)·compose·runtime 디렉터리·Apache vhost·edge `fb.conf` 에서 `5.104.85.244` 0건. 호스트→진아 established 연결 0. DB URL 3개는 모두 `127.0.0.1:5432`(acct/obys/obys_auth) |

## 3. 300초 이상 모니터링 — 통과

12:10:18~12:15:50 KST(332초), 5초 간격, 읽기 전용 GET.

- fb 공개 `/health/live`·obys index 는 200, `/api/v1/auth/me` 는 401 이 기대값. 120 요청, **기대 밖 응답 0**.
- cafe24 origin Apache 가 같은 구간에 기록한 상태: 200 ×84, 401 ×40, 5xx **0**. fb error log 0줄.
- r8 앱 로그 최근 30분: 57줄, ERROR/Traceback 0, HTTP 5xx 0. `pg_isready` accepting.
- 공유 사이트(15초마다 4곳): `aads.newtalk.kr/api/v1/ops/health-check` 200, `pick`·`v2` 307(따라가면 200), `newtalk.kr` 403. 구간 내 상태 변화 0. `newtalk.kr` 403 은 curl UA 에 대한 Cloudflare 응답이며(`server: cloudflare`) 브라우저 UA 로는 200 이다. **시작 전 기준선을 따로 받지 않았으므로 "회귀 없음"이 아니라 "구간 내 변화 없음"으로 적는다.**
- 이 구간은 R20 의 11:24 감시와 별개의 새 측정이다.

## 4. 자료 무누락 대사 — **검증되지 않음 (BLOCK 유지)**

방법: cafe24 `acct-pg` 에서 SELECT 만 수행(`obys`, `acct`, `obys_auth`). 구 서버는 조회하지 않았다. 숫자는 정확한 `count(*)` 결과다.

### 4-1. 기존 R10 수치와의 대조
- `obys` 43개 테이블 합계 31,263행. R10 의 31,261 에서 +2 는 `yeoljeong_businesses` 4→5, `yeoljeong_business_tenant_mapping` 4→5(단하루 등록, 08:54)로 설명된다. 그 외 모든 테이블 count 가 R10 `recon.out` 과 같다.
- 사업장별 매출행 합: 967+928+829+1089 = 3,813 = `yeoljeong_delivery_sales` 전체. 고아 행 없음.
- FK 18개 중 **고아 행이 있는 제약 0**. PK 없는 테이블 0. 시퀀스는 `ddl_audit_log_id_seq`(3)뿐이고 정상.
- 단, FK 4개가 `NOT VALID` 다: `yeoljeong_contracts.fk_yf_contracts_business_tenant`, `yeoljeong_onboarding_documents.fk_yf_documents_business_tenant`, `yeoljeong_employee_join_requests.fk_yf_join_business_tenant`, `yeoljeong_payroll_statements.fk_yf_payroll_business_tenant`. 기존 행이 이 제약을 만족하는지는 검증된 적이 없다는 뜻이다. 고아 조회는 위 4개 포함 전 제약에 대해 0 이었다.
- `obys_auth`: tenant 1(`tenant-32` 열정국밥 운영관리), 사용자 3(owner 2·member 1), 5개 사업장이 모두 이 tenant 하나에 매핑. R10 이 남긴 `internal` slug 는 보이지 않는다(해소로 본다. 단 로그인으로 확인하지 못했다).

### 4-2. 사업장별 적재 (사업자번호는 마스킹)

| 사업장 | 매출 | 정산 | 리뷰 | 광고 | 지점 | 사업자등록증 |
|---|---|---|---|---|---|---|
| 열정국밥 중화점 | 967 | 959 | 1,298 | 695 | 1 | 1 |
| 열정국밥 성신여대점 | 928 | 759 | 1,253 | 606 | 1 | 1 |
| 언니냉면 | 829 | 826 | 1,291 | 588 | 2 | 1 |
| 열정국밥_미아점 | 1,089 | 1,007 | 1,233 | 775 | 1 | 1 |
| 단하루 | 0 | 0 | 0 | 0 | 0 | 0 |

라일론·귀속불명 회사는 `yeoljeong_businesses`·`acct.company`·`acct.tenant` 어디에도 없다. 단하루는 사업장만 등록됐고 자료는 아직 없다.

### 4-3. 첨부
- DB 첨부 참조 18건(사업자등록증 4 + 입사서류 14, 삭제 표시 1건 포함)과 `/srv/acct/attachments` 파일 18개가 일대일이다. 파일시스템에만 있는 고아 파일 0.
- 사업자등록증 4건은 DB `sha256`·`byte_size` 가 실파일과 **전부 일치**. 입사서류 14건은 DB 에 해시가 없어 **크기만** 일치(14/14). 내용은 열어보지 않았다(개인정보).

### 4-4. 기존 4대 문제가 해결됐는가

| 기존 문제 | 현재 SELECT 결과 | 판정 |
|---|---|---|
| source 315 gap | `acct.source_file` 0, `source_ingest` 0, `atom_*` 0, `raw_document` 0, `canonical_*` 0. R10 도 "315 재현 불가"였고 지금도 근거 행이 없다 | **미해결 / 미검증** |
| 원장 0 | `yeoljeong_journal_vouchers`·`journal_lines`·`accounting_entries`·`bank_transactions`·`card_transactions`·`uploaded_ledger_rows`·`payroll_statements`·`tax_reports` 전부 0, `acct.journal_entry`·`journal_line` 0 | **미해결** |
| freshness UNKNOWN | 기준 manifest 가 없다. 측정값만 적는다: 매출 `created_at` 최대 2026-09-12 02:12 UTC, `payload.occurred_on` 최대 2026-09-11, 수집상태 `updated_at` 최대 2026-09-23 02:06 UTC. 오늘(10-03)과 21일·10일 차이. 매출 3,813행 중 3,097행은 `occurred_on` 이 빈 문자열이다 | **UNKNOWN 유지** (기준 시점이 없어 "낡았다/최신이다"를 판정하지 않음) |
| 서명 PDF 미확보 | `yeoljeong_contracts` 13건 = draft 10 + requested 3. `signed_at`/`signed_pdf_path` 가 있는 행 0. 입사서류 중 PDF 는 `resident_register` 1건뿐 | **미확보** (서명 완료본이 DB·파일 어디에도 없음) |

### 4-5. 비라일론 파일(R10 manifest)
`r10_manifest.tsv` 1,598행 = UNATTRIBUTED 922 + LYLON_BY_CONTENT 676 (LYLON_PATH 837 은 경로만으로 제외돼 manifest 에 없음). `staged=Y` 0, `company_map` 비어있음 0건 채움, `selected/` 파일 0. **회사가 귀속된 비라일론 파일이 0건이라 적재할 것이 없었다는 R10 결론이 그대로다.** 새 근거는 없고 재분류도 하지 않았다.

### 4-6. 허용 회사별 범위와 누락 항목, 담당 액션

| 구분 | 상태 | 담당 / 필요한 입력 |
|---|---|---|
| 배달 매출·정산·리뷰·광고·수집상태 4개 사업장 | 구 서버 대비 R10 에서 md5/PK 일치, 현재 count 불변 | 무변경. 신규 수집분(09-12 이후)은 cafe24 에 없음 → 수집 재개 필요 |
| 원장·전표·카드·은행·세금계산서·급여 | 0행 | CEO: 위하고 원장 재내보내기 승인, 카드·은행·세금계산서 기관·동의 범위 확정 |
| 단하루 | 사업장만 존재 | 단하루 자료 제공 |
| UNATTRIBUTED 922파일 | 회사 귀속 없음, 미적재 | 담당자의 회사 매핑 또는 기준 manifest |
| 서명 계약서 PDF | 없음 | 원본 보유처 확인 |
| 최신성 기준 | 없음 | 기준 시점·목록 제공 |

이 표의 어느 항목도 완료로 처리하지 않았다.

## 5. 화면 검증 — 일부만 수행

- **수행 (비로그인)**: 서버에서 Playwright(chromium headless)로 `https://fb.newtalk.kr/static/apps/obys/index.html` 을 desktop 1366×900, mobile 390×844(터치) 로 렌더. 둘 다 HTTP 200, 제목 `오비서`, networkidle, 요청 12건 중 4xx/5xx·requestfailed 0, console error 0, 가로 넘침 없음. 로그인 폼(이메일·비밀번호·로그인·직원 회원가입·사업자 회원가입)이 표시됨. 스크린샷은 직접 확인했다. 증거는 이 서버 `/tmp/r23_evidence/login_desktop.png`(sha256 `67f2641f…`, 79,742B)와 `login_mobile.png`(sha256 `65feb0db…`, 53,678B)이며 저장소에는 넣지 않았다.
- **미수행 (로그인 후)**: 회사선택, 원장, 매장비서, 첨부, 세션복구 desktop/mobile. 이유:
  1. Vault 에 `https://fb.newtalk.kr` origin 의 `agent_vault_credentials` 가 1건 있으나 `owner=CEO`, `policy=ask`(사용 시마다 CEO 승인), 출처 Google 비밀번호 관리자 CSV, 사용 이력 없음. 건별 승인 근거가 이 세션에 없다.
  2. `e2e_credentials` 의 fb 항목은 라벨이 `라일론 운영 E2E` 라 이 작업에서 제외 대상이다.
  3. 복호화 키가 이 세션 환경에 없고, `run_e2e_verify` 는 `task_logs` 에 기록하는 운영 쓰기 경로다.
  
  자격증명을 우회해 쓰지 않았다. 따라서 **"⚠️ 브라우저 E2E(로그인 후) 미실행, API 검증으로 대체"** 이며 인증 후 화면 검증은 미완료다. 대체로 HTTP→API→container 순으로 확인한 결과는 §2·§3 이다(인증 API 401, 컨테이너 정상). 이것은 로그인·회사별 데이터 표시를 검증한 것이 아니다.

## 6. 발견된 위험 (코드 수정 없이 보고)

| # | 등급 | 내용 | 최소 조치 요청 |
|---|---|---|---|
| 1 | **P1** | cafe24 의 edge 허용 규칙(`CF-WEB` 첫 규칙)이 **영속화되지 않았다.** `/etc/iptables/rules.v4`(4/30 파일)에 `fb-edge-contabo116` 이 없고 `netfilter-persistent` 도 없다. iptables 재적재·재부팅 시 edge IP 가 `CF-WEB` 끝의 DROP 에 걸려 fb 가 502 가 된다 | ops: 현재 규칙을 재적재 후에도 유지되는 방식으로 반영(방식 확정 필요). 이 세션은 변경하지 않음 |
| 2 | P2 | contabo116 edge 의 `newtalk.kr` 인증서가 **2026-07-29 에 만료**됐다(127.0.0.1:443 SNI fb.newtalk.kr 에서 `notAfter=Jul 29 2026`). 공개 경로는 Cloudflare 의 인증서(만료 2026-12-16)를 보고 있어 지금은 영향이 없고 CF→edge 구간이 Full(비strict)인 것에 의존한다 | edge 인증서 갱신. CF 를 strict 로 바꾸기 전에 필수 |
| 3 | P2 | `fb.conf` 의 `/unni-naengmyeon/` 와 `/_next/` 는 아직 `aads_dashboard`(이 서버)로 간다. 서버 68 의 `yeoljeong-finance` 컨테이너(127.0.0.1:8110)도 가동 중이고 `aads.conf` 에 `/api/yeoljeong/finance/ → aads_api` 가 남아 있다. obys 앱 경로(index/API/static/health)는 cafe24 로 갔지만 **fb 사이트 전체가 cafe24 독립은 아니다.** 이 세션은 중단·삭제하지 않음 | 후속: 어디까지가 독립 범위인지 CEO 확인 후 정리 |
| 4 | P2 | `server_registry` 의 `cafe24_114` 설명은 "SF/NTV2/NAS 실행 환경, 포트 7916, workdir /data/shortflow"(2026-09-07 이후 미갱신)이고 ACCT/fb 운영을 설명하지 않는다. `contabo116` 도 "AADS Backend+Dashboard+PostgreSQL" 뿐이다. 레지스트리를 정본으로 쓰면 토폴로지가 틀린다 | 레지스트리 갱신 요청 |
| 5 | P3 | cafe24 에 후보 컨테이너 `acct-app-candidate-r5` 가 계속 실행 중이다(같은 netns, 19시간). `jinah-pg`(127.0.0.1:18433)도 떠 있으나 앱 env 는 참조하지 않는다. 둘 다 건드리지 않음 | 정리 여부 CEO 확인 |
| 6 | P3 | `/health/ready` 는 공개에서 302(edge catch-all)로 응답해 origin 의 readiness 에 닿지 않는다. 의도된 설계일 수 있다 | 필요 시 edge 에 경로 추가 검토 |

## 7. 완료·미완료 분리

| 구분 | 상태 |
|---|---|
| 코드 검수 | 해당 없음 (코드 변경 없음) |
| push / 큐 등록 / 실배포 | 이 세션은 하지 않음 |
| 운영 라우팅·origin·방화벽·인증서·300초 | **검증 완료** (§2, §3) |
| 공개 로그인 화면 desktop/mobile | **검증 완료** (§5) |
| 로그인 후 화면(회사선택·원장·매장비서·첨부·세션복구) | **미완료** |
| 자료 무누락 | **미검증** — 원장 0, source 0, 서명 PDF 0, freshness 기준 없음 |
| 전체 이전 | **미완료** (§4-4, §6-3) |

검증 성공과 전체 이전 성공은 같지 않다. 마일스톤 증거를 신고하지 않았다(완료된 것이 없다).

## 8. 비용 / 서약

- 이 세션이 호출한 외부 유료 LLM API: 0건. 이 세션 자신의 모델 사용 비용은 **미측정**.
- 시크릿·원문 금융자료·개인정보 출력 0. Vault 에서 암호문·평문을 읽지 않았고 메타데이터만 조회했다. 사업자번호는 앞 3자리만 적었다.
- 진아서버 접속 0. 라일론·귀속불명 자료는 사용하지 않았다.

## 9. 기록 / 후속

- DB handover `acct-cafe24-held-verification-20261003-r23`: 이 보고서 작성 뒤 `project_handover_entries` 에 기록(아래 "기록 결과" 참조).
- goal `df479771-…`(status `blocked`, progress 0.14)의 `link_task`/binding, cancelled predecessor 이력: 공식 서비스(마일스톤 reconcile 포함)를 거쳐야 해서 raw SQL 로 쓰지 않았다. **미수행** — 오케스트레이터가 공식 경로로 `runner-ef3ebe1d` 를 연결해야 한다.
- 후속 검증이 필요한 일: (a) CEO 의 로그인 자격 사용 승인(건별) 또는 비라일론 테스트 계정 → 로그인 후 desktop/mobile 증거, (b) §6-1 방화벽 영속화 뒤 재확인, (c) §4-6 의 CEO 입력 항목.

### 기록 결과
- `project_handover_entries` (project_key `AADS`, entry_key `acct-cafe24-held-verification-20261003-r23`, revision 1, id `2a1e5d3f-077f-44c2-95e2-ffce42cf32a1`)와 `project_handover_events`(created) 1행을 한 트랜잭션으로 기록했다.
- **예외 고지**: 공식 `handover_store.upsert_handover_entry` 는 asyncpg 가 필요한데 이 세션에 없어서 같은 INSERT 문을 psycopg2 로 직접 실행했다. 이 세션이 한 운영 DB 쓰기는 이 2행뿐이다.
- 저장소 `HANDOVER.md` 는 수정하지 않았다(지정 파일 1개 제한).
