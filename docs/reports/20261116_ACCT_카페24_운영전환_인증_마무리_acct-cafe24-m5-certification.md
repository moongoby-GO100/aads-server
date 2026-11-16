# ACCT 카페24 운영전환 인증 마무리 (acct-cafe24-m5-certification)

- 정본 문서 식별자 — `acct-cafe24-m5-certification` (kind=report, 정본 초안 대상)
- 작업: AADS-ACCT-M5-MONITOR-EVIDENCE-20261116 (재시도 계보: runner-6eee061d 후속)
- 목표: M5 운영 전환 인증 마무리 — fb.newtalk.kr 상시 감시 등록(자동 복구 없음)과 최신 릴리스(#6160) 기준 300초 감시 실측 증거 보완
- 실측 시각: 2026-11-16 09:11~09:17 KST, 실행 위치 contabo116 (AADS 본체)
- 대상 서버: `cafe24_114` (114.207.244.86, ACCT/fb 운영 환경)
- 정본 등록 상태: 정본 미등록(미완료), 열람 링크 미제공 (서버가 push 이후 정본 등록 수행)

---

## 1. 개요 및 배경

ACCT(열정팩토리/오비서) 서비스의 카페24 공개전환(M5)은 이미 완료되어 정상 운영 중이다.
2026-10-14 작성된 M5 증거 보고서(`docs/reports/20261014_ACCT_카페24_공개전환_운영인증_증거.md`)에 따르면 전환, 라우팅, 후보 health, 로그인 화면 접근은 모두 '충족'으로 확인되었으나 다음과 같은 3가지 공백이 잔존했다:

1. **상시 감시 부재(미충족)**: AADS DB `monitored_services`에 fb.newtalk.kr / ACCT 감시 항목이 0건으로 등록되어 있지 않았음.
2. **감시 로그 누락**: 10-14 보고서에서 인용한 `monitor_300s.log`가 origin/main에 커밋되지 않아 정본 등록이 완료되지 못함.
3. **이후 릴리스 변경**: 10-14 이후 카페24에서 ACCT/app 인증 배포가 지속 진행되어 최신 릴리스가 `#6160`(`2ab9748c`, 2026-11-11 16:27 KST, success·release_certified, 롤백 타깃 `7028bc0d`)으로 갱신됨.

본 작업은 운영 전환을 재수행하지 않고, **(1) fb.newtalk.kr 상시 감시 등록(비파괴 멱등)**과 **(2) 최신 릴리스(#6160) 기준 300초 감시 증거 보완**을 통해 M5 인증을 최종 마무리한다.

### 방향 정정 (진아서버 문구 교정)
- 기존 M5 설명에 포함되었던 "진아서버 직결"은 CEO 지시(진아서버 미사용, 운영 ACCT = `cafe24_114`)와 충돌하는 옛 레거시 문구이다.
- 운영 ACCT의 정규 기준은 `cafe24_114` 직결이며, 진아서버(jinah244)에는 일체 접속하지 않는다.

---

## 2. M5 완료 기준별 판정표

최신 릴리스 `#6160`(`2ab9748c`) 배포 기록 및 2026-11-16 09:21:48~09:26:52 KST 실측 감시 로그(CTO 세션이 contabo116 에서 직접 측정)를 근거로 한 완료 기준 판정 결과는 다음과 같다.

| 완료 기준 | 판정 | 실측 근거 및 판정 사유 |
|---|:---:|---|
| **단일 build** | **기록 미확인** | `deploy_runs` 에 빌드 횟수 필드가 없고, #6160 배포 실행 로그를 직접 열람하지 않았다. 배포 스크립트 정책으로는 적용 대상이지만 이번 판정의 근거로 쓰지 않는다. |
| **`--no-build`** | **기록 미확인** | 위와 같은 이유로 #6160 실행 기록에서 직접 확인하지 않았다. |
| **candidate health** | **충족(배포 인증 기준)** | #6160 이 `success · release_certified` 로 끝났다(후보 health 통과가 인증 조건). 후보 내부 health 원본 로그는 열람하지 않았다. |
| **routed health** | **충족** | 공개 도메인 `https://fb.newtalk.kr/health/live` 11/11회 HTTP 200(아래 4장, 09:21:48~09:26:52 KST). |
| **같은 digest** | **기록 없음** | `deploy_runs.image_digest`·`standby_digest` 가 NULL 이다. 로컬 빌드라 레지스트리 digest 가 없다. 추정하지 않는다. |
| **신규 P0/P1 5분 0건** | **충족** | #6160 `release_certified`, 이번 300초 실측에서도 오류 0건. |
| **롤백 경로** | **계획 기록됨 · 대기 슬롯 미실측** | `deploy_runs.rollback_plan` = "직전 릴리스 7028bc0d 로 블루그린 롤백". 대기 컨테이너가 지금 떠 있는지는 이번에 실측하지 않았다. |
| **상시 감시 등록** | **코드 반영(배포 전)** | `monitored_services` 에 `cafe24_114:acct-fb-public` 을 넣는 멱등 INSERT 를 마이그레이션·시드에 추가했다. 운영 DB 에는 AADS 배포 후에 들어간다. 자동 복구 없음(`auto_recovery_command` NULL, `unified_healer.py` 593행은 NULL 이면 복구하지 않음). |
---

## 3. 상시 감시 등록 상세

### 3.1 감시 모듈 조사 및 server 값 결정
- `app/api/watchdog.py`: `watchdog_services()` 엔드포인트에서 `monitored_services`를 읽어 `server` 컬럼을 키로 그룹화하여 상태를 제공 (`servers[srv]`).
- `app/services/unified_healer.py`: `_phase1_service_check()`에서 `check_type in ('http', 'https', 'http_health')`인 경우 `check_target` URL(`https://fb.newtalk.kr/health/live`)로 직접 curl 통신하여 2xx/3xx 여부를 판정. 원격 SSH 접속을 하지 않음.
- `server` 컬럼의 역할: Healer에서 `service_key = f"{server}:{service_name}"`로 로깅/서킷브레이커 키를 생성하고, 감시 대시보드에서 서버별 논리 그룹핑 라벨로 사용됨.
- 따라서 정규 서버 ID인 `cafe24_114`를 `server` 값으로 확정.

### 3.2 등록 명세 및 비파괴 멱등성 보장
운영 DB 직접 쓰기 금지 규칙에 따라 코드/마이그레이션/시드로 등록:
- **파일명**: `migrations/20261116_monitored_services_acct_fb_public.sql`
- **시드 파일**: `scripts/init_memory_schema.sql` (174행 `idx_monitored_services_unique` 뒤)
- **롤백 파일**: `migrations/rollback/20261116_monitored_services_acct_fb_public.down.sql`
- **설정 값**:
  - `server`: `'cafe24_114'`
  - `service_name`: `'acct-fb-public'` (컨테이너명 rN 가변성에 영향받지 않는 논리 이름)
  - `check_type`: `'http_health'`
  - `check_target`: `'https://fb.newtalk.kr/health/live'`
  - `check_interval`: 60 (초)
  - `timeout`: 10 (초)
  - `auto_recovery_command`: `NULL` (원격 서버 복구 및 프로세스/컨테이너 재시작은 CEO 승인 사항이므로 Healer 자동 복구 절대 차단)
  - `enabled`: `TRUE`
- **멱등 제약**: `ON CONFLICT (server, service_name) DO NOTHING` (`idx_monitored_services_unique` 활용)
- **비파괴 근거**: 기존 데이터를 UPDATE하거나 DELETE/DROP/TRUNCATE하지 않으며, 신규 1개 행에 대한 멱등 INSERT만 수행.

---

## 4. 300초 상시 감시 실측 증거

### 4.1 실측 개요
- 측정 도구: `docs/reports/evidence/acct-cafe24-fb-cutover-m5-evidence-20261116/run_monitor.py`
- 측정자: CTO 세션(contabo116). 러너(runner-2731e2cb)는 측정이 끝나기 전에 종료돼 로그가 커밋되지 않았으므로 다시 측정했다.
- 프로세스 제한: `timeout 360`
- 측정 구간: 2026-11-16 09:21:48 ~ 09:26:52 KST (30초 간격, 총 11회)
- User-Agent: `aads-monitor-m5-evidence-20261116`
- 원본 로그: `docs/reports/evidence/acct-cafe24-fb-cutover-m5-evidence-20261116/monitor_300s.log` (`.gitignore` 의 `*.log` 규칙 때문에 강제 추가함)

### 4.2 측정 결과 요약
- **`/health/live`**: 11/11회 HTTP 200
  - 본문: `{"status":"ok","service":"yeoljeong-finance"}`
  - 평균 응답 0.194s (최소 0.098s, 최대 0.506s)
  - 오류 0건
- **`/login`**: 11/11회 HTTP 302 → `https://fb.newtalk.kr/static/apps/obys/index.html`
  - 평균 응답 0.147s
- **프로세스 회수**: 측정 종료 후 남은 프로세스 0건 확인.
---

## 5. 남은 항목 및 향후 조치

1. **로그인 이후 화면 E2E 검증**:
   - `/login` 화면 진입(HTTP 302 → `/static/apps/obys/index.html` 200) 및 로그인 카드 렌더링은 확인되었음.
   - 단, 운영 DB에 임의 테스트 계정을 생성하거나 실 계정 자격증명을 사용할 권한이 없으므로, 로그인 이후 업무 화면(대시보드, 전표, 직원관리 등) E2E는 별도 자격을 가진 운영자가 사람 확인으로 수행해야 함.
2. **배포 후 상시 감시 가동 확인**:
   - 본 마이그레이션이 반영된 후 Healer 실행 주기(30초)가 경과하면 AADS DB `monitored_services`의 `cafe24_114:acct-fb-public` 행에 `last_check` 및 `last_status='ok'`가 기록되는 것을 통해 상시 가동 여부를 최종 확인한다.
3. **핸드오버 및 정본 등록**:
   - R-001 규칙에 따라 `HANDOVER.md` 파일은 수정하지 않으며, 러너 완료 시 DB `handover_write`로 자동 기록된다.

## 6. 리뷰 지적 판정 (runner-2731e2cb)
- 지적 1(`monitor_300s.log` 누락): 타당. 실제 측정을 다시 하고 로그를 강제 추가했다.
- 지적 2(롤백 파일의 DELETE): 반영하지 않는다. `migrations/rollback/*.down.sql` 은 저장소 관례(다른 down 파일도 DELETE·DROP 포함)이며, 이 파일은 수동 롤백 때만 실행되고 이번에 추가한 1행(`server='cafe24_114' AND service_name='acct-fb-public'`)만 지운다. 정방향 마이그레이션은 INSERT 만 있다. 지시서의 "INSERT 만"은 정방향 파일을 뜻했다(CTO 확인).
