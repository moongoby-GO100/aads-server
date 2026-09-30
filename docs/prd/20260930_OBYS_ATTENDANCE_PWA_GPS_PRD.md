# PRD — 오비서 직원 모바일 출퇴근 (설치형 웹앱 + GPS 매장 반경 판정)

| 항목 | 값 |
|---|---|
| 문서 ID | FOOD-LAYOUT-ATTENDANCE-PWA-GPS-20260930 |
| 대상 서비스 | 오비서(obys_standalone, 열정국밥 운영관리) — 운영: 진아서버 jinah244, 코드: aads-server |
| 작성 | 2026-09-30 15:55 KST, CTO(AI) |
| 요청 | CEO 2026-09-30 "웹앱을 직원 핸드폰에 설치하고 GPS로 출퇴근 관리" → "기획 설계 PRD 상세 작성" |
| 상태 | 초안(Draft) r3 — §13 D1 확정(반경 50m), D2·D3 확정 후 v1.0 |
| 개정 | r2 2026-09-30 16:05 KST — API 경로·설치 시작주소·서비스워커 범위를 실제 라우팅(`/api/v1/workspaces`, `/static/apps/obys/`)에 맞춤, 법정 보존 항목 추가 |
| 개정 | r3 2026-09-30 16:01 KST — CEO 결정 D1 "매장 반경 50m 안" 반영: 기본·상한 50m, 정확도 여유 제거, 정확도 판정 기준 50m |
| 선행 작업 | 직원 테넌트 멤버십 R2(직원 계정 403 해소), 근태 서버 저장 `runner-9186c664`(push, 운영 미배포) |
| 연관 작업 | 계약정보→고용정보·급여 기준값 반영(AADS-OBYS-CONTRACT-TO-EMPLOYMENT-PAYROLL-20260930) |

---

## 1. 요약

직원이 자기 휴대폰에 오비서를 "홈 화면에 추가"로 설치하고, 매장에서 **출근/퇴근 버튼 한 번**으로 근태를 남긴다.
버튼을 누르는 순간에만 위치를 1회 측정해 **매장 반경 안이면 자동 확정**, 반경 밖·측정 실패·조작 의심이면 **"확인필요"로 사장님 화면에 올린다.**
확정된 근무시간은 계약서 시급과 곱해 급여명세서 기준값이 된다. 위치를 계속 추적하지 않는다.

## 2. 배경 — 현재 상태 (2026-09-30 실측)

| 항목 | 현재 | 근거 |
|---|---|---|
| 근태 입력 | 관리자가 화면 입력 또는 CSV 붙여넣기 | `app/static/apps/obys/index.html` 3193·4922행 [코드 확인] |
| 서버 저장 | 근태 CRUD API 신설, push 완료·운영 미배포 | `app/api/obys_workspaces.py` 739~941행, `runner-9186c664` |
| 근태 필드 | 근무일·지점·시작·종료·휴게·근무분·시급·상태·메모 | `ATTENDANCE_COLUMNS` |
| 상태값 | `pending`/`approved`/`rejected` | `ATTENDANCE_STATUSES` |
| 제약 | `employee_email NOT NULL`, `UNIQUE(employee_email, work_date, start_at)` (사업자 구분 없음) | 같은 파일 주석(운영 스키마 조회 2026-09-30) |
| 근무분 계산 | 서버 계산, 자정 넘김 허용, 24시간 초과 거부. **종료 시각 필수** | `attendance_worked_minutes` |
| 직원 본인 화면 | "본인 근태 보기"만 있음. 출퇴근 버튼 없음 | `index.html` 7270행 |
| 위치·설치형 웹앱 | 없음 (`latitude`/`geolocation`/manifest·서비스워커 0건) | `git grep` origin/main |
| 지점 테이블 | `yeoljeong_branches`에 주소만, 좌표·반경 컬럼 없음 | `migrations/113_yeoljeong_finance_settings.sql` |
| 지점 주소 | 중화점·미아점 주소 있음, **성신여대점 주소 공란** | `CANONICAL_BRANCHES` |
| 직원 계정 | 테넌트 멤버십이 없어 직원 로그인 시 403 | 2026-09-30 14:20 KST E2E |

**핵심 제약:** 기존 근태 행은 시작·종료를 한 번에 받는 구조다. "출근만 찍고 아직 퇴근 전" 상태를 담을 수 없으므로
원시 타각(punch)을 별도 테이블에 쌓고, 퇴근 시점에 근태 행을 만든다(§7).

## 3. 목표 / 비목표

### 목표
| # | 목표 | 측정 |
|---|---|---|
| G1 | 직원이 설치형 웹앱으로 출퇴근을 스스로 기록 | 전 직원 중 모바일 타각 비율 ≥ 90%(도입 4주 후) |
| G2 | 매장 반경 판정으로 관리자 확인 부담 감소 | 자동 확정 비율 ≥ 80%, 확인필요 ≤ 20% |
| G3 | 근태 → 급여 기준값 자동 연결 | 월마감 시 근태 수기 입력 0건 |
| G4 | 개인정보 최소수집·동의·감사 이력 확보 | 동의 없는 위치 저장 0건, 모든 수정에 감사로그 |

### 비목표 (이번 범위 아님)
- 근무 중 실시간 위치 추적, 이동 경로 기록
- 네이티브 앱(앱스토어·플레이스토어) 출시
- 얼굴 인식·생체 인증
- 근무표(스케줄) 작성 기능 — 단, 지각·조퇴 판정을 위한 **계약서 근무시간 참조**는 2단계에 포함
- 푸시 알림 기반 출근 독려 — 3단계 선택 기능

## 4. 사용자

| 사용자 | 하는 일 | 핵심 요구 |
|---|---|---|
| 직원(정규·아르바이트) | 출근·퇴근·휴게 기록, 본인 근태 확인, 누락 정정 요청 | 3초 안에 출근 완료, 한 손 조작, 실패 시 이유와 다음 행동 |
| 점장/매니저(admin) | 확인필요 건 승인·반려, 정정 요청 처리 | 오늘 누가 출근했는지 한눈에, 예외만 보기 |
| 사장(owner) | 지점 좌표·반경 설정, 월마감, 급여 확정 | 설정은 한 번, 월말엔 합계만 확인 |

## 5. 사용자 흐름

### 5.1 첫 실행 (직원)
1. 가입 승인 또는 계약서 서명 링크로 오비서 접속 (로그인)
2. 설치 안내 카드 표시
   - Android Chrome: `beforeinstallprompt`로 [설치] 버튼
   - iPhone Safari: "공유 → 홈 화면에 추가" 그림 안내 (iOS는 자동 설치 프롬프트 없음)
3. **위치정보 수집 동의 화면** — 수집 항목·목적·시점·보존기간·거부 시 불이익 없음(수기 정정 경로 안내) 명시 → [동의] / [나중에]
4. 브라우저 위치 권한 요청 (동의 후에만 요청)
5. 소속 지점 확인 → 첫 화면 = 출근 버튼

### 5.2 매일 사용 (직원)
1. 홈 화면 아이콘 → 로그인 유지 상태로 **출근 화면 바로 진입**
2. [출근] 탭 → 위치 1회 측정(최대 10초) → 결과 표시
   - ✅ "중화점 09:02 출근 완료" (반경 안)
   - ⚠️ "매장에서 230m 떨어져 있어 사장님 확인이 필요합니다" (반경 밖, 기록은 남음)
3. 근무 중 화면: 출근 시각, 경과 시간, [휴게 시작]/[퇴근]
4. [퇴근] 탭 → 동일 판정 → 근무시간·예상 금액(계약 시급 기준) 표시

### 5.3 실패 복구
| 상황 | 화면 동작 | 결과 |
|---|---|---|
| 위치 권한 거부 | "위치 없이 기록" + 설정 방법 안내 | 타각 저장, `geo_verdict=no_permission` → 확인필요 |
| 측정 시간 초과/오차 과대(정확도 > 50m, 즉 반경보다 오차가 큼) | 1회 재시도 버튼 → 그래도 실패면 "위치 없이 기록" | `geo_verdict=low_accuracy` → 확인필요 |
| 오프라인 | 기기에 임시 저장, 연결되면 자동 전송. 화면에 "전송 대기 1건" | 서버 수신 시각과 기기 시각 모두 저장, 차이 > 10분이면 확인필요 |
| 퇴근 미기록 | 다음 날 첫 진입 시 "어제 퇴근이 없습니다 → 퇴근 시각 입력" | 정정 요청 → 관리자 승인 |
| 중복 탭 | 60초 내 같은 종류 타각은 무시하고 기존 결과 표시 | 중복 행 없음 |
| 세션 만료 | 로그인 화면 후 **원래 누르려던 동작으로 복귀** | 타각 유실 없음 |
| 계정 권한 없음(403) | "사장님께 직원 승인 요청" 버튼 | 가입요청 화면으로 연결 |

### 5.4 관리자
1. 대시보드 "오늘 근태" 카드: 출근 n / 미출근 n / 확인필요 n
2. 확인필요 목록: 직원·시각·거리·정확도·사유 → [승인]/[반려]/[시각 수정]
3. 정정 요청 목록: 요청 시각·사유 → 승인 시 근태 행 반영, 감사로그
4. 설정(Admin/Settings 메뉴): 지점 좌표(주소 검색 또는 "지금 위치로 설정"), 허용 반경, 위치 판정 사용 여부

## 6. 기능 요구사항

| ID | 요구사항 | 우선 | 단계 |
|---|---|---|---|
| FR-01 | Web App Manifest(`name`, `short_name`, `start_url=/static/apps/obys/index.html?view=clock`, `scope=/static/apps/obys/`, `display=standalone`, 아이콘 192/512) | P0 | 1 |
| FR-02 | 서비스워커: 정적 자산 캐시 + 오프라인 타각 큐(IndexedDB) + 재연결 시 전송. **API 응답은 캐시하지 않음** | P0 | 1 |
| FR-03 | 설치 안내 카드(Android 프롬프트 / iOS 그림 안내), 설치 후 숨김 | P1 | 1 |
| FR-04 | 위치정보 수집 동의 화면 + 동의 이력 저장 + 동의 철회 | P0 | 1 |
| FR-05 | 출근/퇴근 타각 API, 버튼 누른 순간 1회 측정(`enableHighAccuracy`, timeout 10초, maximumAge 0) | P0 | 1 |
| FR-06 | 서버 측 반경 판정(하버사인 거리) — 클라이언트 판정 결과는 신뢰하지 않음 | P0 | 1 |
| FR-07 | 퇴근 타각 시 근태 행(`yeoljeong_attendance_records`) 생성, 근무분은 기존 `attendance_worked_minutes` 재사용 | P0 | 1 |
| FR-08 | 지점 좌표·반경 설정 화면(owner/admin) | P0 | 1 |
| FR-09 | 관리자 확인필요 목록·승인·반려 | P0 | 1 |
| FR-10 | 휴게 시작/종료 타각 → `break_minutes` 자동 합산 | P1 | 2 |
| FR-11 | 누락·정정 요청(직원) → 승인(관리자) | P1 | 2 |
| FR-12 | 계약서 근무시간 대비 지각·조퇴·연장 표시(판정만, 급여 자동 가감 없음) | P1 | 2 |
| FR-13 | 근태 확정분 × 계약 시급 → 급여명세서 기준값(수정 가능) | P1 | 2 |
| FR-14 | 조작 의심 신호 기록(§8.3) | P1 | 2 |
| FR-15 | 매장 QR 보조 인증(매장 기기에 30초마다 바뀌는 QR) — 부정 사례 발생 시 활성화 | P2 | 3 |
| FR-16 | 출근 예정 시각 알림(웹 푸시, iOS 16.4+ 설치형만 지원) | P2 | 3 |

## 7. 데이터 모델 (마이그레이션 초안)

모든 변경은 **추가형(nullable/신규 테이블)**이다. 기존 행·기존 API 응답은 바뀌지 않는다.

```sql
-- 1) 지점 위치
ALTER TABLE yeoljeong_branches
  ADD COLUMN IF NOT EXISTS latitude          DOUBLE PRECISION,
  ADD COLUMN IF NOT EXISTS longitude         DOUBLE PRECISION,
  ADD COLUMN IF NOT EXISTS geofence_radius_m INTEGER NOT NULL DEFAULT 50 CHECK (geofence_radius_m BETWEEN 30 AND 50),
  ADD COLUMN IF NOT EXISTS geo_check_enabled BOOLEAN NOT NULL DEFAULT FALSE;

-- 2) 원시 타각 (출근·퇴근·휴게)
CREATE TABLE IF NOT EXISTS yeoljeong_attendance_punches (
  id              TEXT PRIMARY KEY,
  business_id     TEXT NOT NULL,
  branch_id       TEXT NOT NULL,
  employee_email  TEXT NOT NULL,
  employee_name   TEXT NOT NULL,
  punch_type      TEXT NOT NULL CHECK (punch_type IN ('in','out','break_start','break_end')),
  client_ts       TIMESTAMPTZ NOT NULL,          -- 기기 시각
  server_ts       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  latitude        DOUBLE PRECISION,              -- 동의·측정 성공 시에만
  longitude       DOUBLE PRECISION,
  accuracy_m      REAL,
  distance_m      REAL,
  geo_verdict     TEXT NOT NULL CHECK (geo_verdict IN
                   ('inside','outside','low_accuracy','no_permission','no_consent','branch_unset','disabled')),
  suspicion_flags JSONB NOT NULL DEFAULT '[]',
  idempotency_key TEXT NOT NULL,                 -- 오프라인 재전송 중복 방지
  attendance_record_id TEXT,                     -- 퇴근 시 생성된 근태 행
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (business_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_punch_emp_day
  ON yeoljeong_attendance_punches (business_id, employee_email, server_ts DESC);

-- 3) 위치 수집 동의
CREATE TABLE IF NOT EXISTS yeoljeong_location_consents (
  id              TEXT PRIMARY KEY,
  business_id     TEXT NOT NULL,
  employee_email  TEXT NOT NULL,
  consent_version TEXT NOT NULL,
  agreed_at       TIMESTAMPTZ NOT NULL,
  withdrawn_at    TIMESTAMPTZ,
  user_agent      TEXT NOT NULL DEFAULT '',
  UNIQUE (business_id, employee_email, consent_version)
);

-- 4) 근태 행에 출처 표시
ALTER TABLE yeoljeong_attendance_records
  ADD COLUMN IF NOT EXISTS source     TEXT NOT NULL DEFAULT 'manual',  -- manual|csv|mobile
  ADD COLUMN IF NOT EXISTS punch_in_id  TEXT,
  ADD COLUMN IF NOT EXISTS punch_out_id TEXT;
```

- 롤백: 신규 테이블 DROP은 **금지 규칙상 수행하지 않고** 기능 플래그(`geo_check_enabled=false`, 화면 숨김)로 비활성화한다. 추가 컬럼은 nullable/기본값이라 구버전 코드와 호환된다.
- `UNIQUE(employee_email, work_date, start_at)` 충돌: 같은 직원·같은 날·같은 분에 두 번 출근하는 경우만 해당. 모바일 타각은 60초 중복 차단(FR 5.3)으로 예방하고, 충돌 시 409 + "이미 기록된 출근" 안내.

## 8. 판정 로직

### 8.1 반경 판정 (서버)
```
if not consent:                 verdict = no_consent      → pending
elif branch.lat is null:        verdict = branch_unset    → pending
elif not branch.geo_check:      verdict = disabled        → approved (위치 미사용 매장)
elif coords is null:            verdict = no_permission   → pending
elif accuracy_m > radius:       verdict = low_accuracy    → pending   # 오차가 반경(50m)보다 크면 안/밖 판정 불가
else:
    d = haversine(coords, branch)
    verdict = inside if d <= radius else outside              # radius 기본·상한 50m, 여유 없음
```
- CEO 결정(2026-09-30): **매장 반경 50m 안**만 자동 확정한다. 정확도 여유를 더하지 않는다 — 여유를 더하면 실제 허용 거리가 최대 100m로 늘어 결정과 어긋난다.
- 대가: 실내에서 GPS 오차가 50m를 넘으면 `low_accuracy`로 "확인필요"가 된다. 기록은 남고 관리자가 승인하면 된다. 도입 첫 주 확인필요 비율을 측정해(§14 운영) 20%를 넘으면 매장 QR(FR-15) 앞당김을 CEO에게 올린다. 반경을 넓히는 것은 이 문서 범위에서 하지 않는다.
- 근태 행 상태: 출근·퇴근 **둘 다** `inside`(또는 `disabled`)이고 의심 신호가 없으면 `approved`, 아니면 `pending`.

### 8.2 시각
- 근무 시각은 **서버 수신 시각** 기준. 오프라인 전송분만 기기 시각을 쓰되 `|server_ts - client_ts| > 10분`이면 `pending`.
- 시간대는 Asia/Seoul 고정. 자정 넘김은 기존 `attendance_worked_minutes` 규칙 그대로.

### 8.3 조작 의심 신호 (차단하지 않고 기록 → 관리자 판단)
| 신호 | 기준 |
|---|---|
| 정확도가 비정상적으로 정밀 | `accuracy_m < 3` 가 반복 |
| 좌표 완전 동일 반복 | 서로 다른 날 소수점 6자리까지 동일 |
| 순간이동 | 직전 타각 대비 이동속도 > 200km/h |
| 기기 시각 불일치 | §8.2 기준 초과 |
| 다른 직원과 같은 기기 | 같은 기기 식별값(로컬 저장 UUID)으로 두 계정 타각 |

웹 브라우저는 위치 조작 앱을 확실히 탐지할 수 없다. 이 방식의 한계로 명시하고, 부정 사례가 확인되면 FR-15(매장 QR)를 켠다.

## 9. API

| 메서드 | 경로 | 권한 | 설명 |
|---|---|---|---|
| GET | `/api/v1/workspaces/{business_id}/attendance/clock/state` | 직원 본인 | 오늘 상태(미출근/근무중/휴게중/퇴근), 소속 지점, 동의 여부, 반경 |
| POST | `/api/v1/workspaces/{business_id}/attendance/clock/punch` | 직원 본인 | body: `punch_type, client_ts, lat?, lng?, accuracy_m?, idempotency_key, device_id` → 판정 결과 반환 |
| POST | `/api/v1/workspaces/{business_id}/attendance/consent` | 직원 본인 | 동의/철회 |
| GET | `/api/v1/workspaces/{business_id}/attendance/review` | owner/admin | 확인필요 목록 |
| POST | `/api/v1/workspaces/{business_id}/attendance/review/{record_id}` | owner/admin | `approve / reject / adjust(start_at,end_at)` + 사유, 감사로그 |
| POST | `/api/v1/workspaces/{business_id}/attendance/corrections` | 직원 본인 | 정정 요청(2단계) |
| PUT | `/api/v1/workspaces/{business_id}/branches/{branch_id}/geo` | owner/admin | 좌표·반경·사용 여부 |

- 경로는 기존 라우터(`APIRouter(prefix="/workspaces")`, 화면은 `fetch(`/api/v1/workspaces${path}`)`) 규칙을 따른다. 기존 근태 CRUD(`/{business_id}/attendance/records`, 841~944행) 옆에 두고, 제네릭 `/{route}/records` 보다 먼저 등록한다.
- 직원은 **본인 타각만** 쓸 수 있다. `employee_email`은 요청 본문이 아니라 로그인 세션에서 가져온다.
- 관리자 대리 타각 금지(계약서 대리 서명 차단과 같은 원칙). 관리자는 review/adjust만 가능.
- 모든 쓰기는 `yeoljeong_audit_logs`에 `attendance.punch / attendance.review / attendance.geo_config / attendance.consent` 로 남긴다.

## 10. 화면

| 화면 | 대상 | 핵심 요소 |
|---|---|---|
| 출퇴근(기본 첫 화면, 직원) | 직원 | 큰 버튼 1개(상태에 따라 출근/퇴근), 현재 지점, 오늘 기록, 전송 대기 건수 |
| 동의 | 직원 | 수집 항목·목적·시점·보존기간·철회 방법, [동의]/[나중에] |
| 설치 안내 | 직원 | OS별 1장 그림, 닫기 |
| 내 근태 | 직원 | 이번 달 근무일·시간·예상 금액, 확인필요 사유, [정정 요청] |
| 오늘 근태 카드 | 관리자 | 출근/미출근/확인필요 수 → 목록 이동 |
| 확인필요 | 관리자 | 거리·정확도·사유 표시, 일괄 승인 |
| 지점 위치 설정 | owner | 주소→좌표, "지금 위치로 설정", 반경 슬라이더(30~50m, 기본 50m — CEO 결정 상한, 더 넓힐 수 없음), 지도 미리보기(선택) |

모바일 기준: 버튼 높이 ≥ 56px, 한 손 엄지 영역, 글자 줄바꿈, 앱 재실행 후 근무중 상태 복원.
화면 라벨은 업무명(출근·퇴근·확인필요)만 쓰고 `geo_verdict` 같은 내부값은 노출하지 않는다.

## 11. 개인정보·법무

| 항목 | 정책 |
|---|---|
| 수집 항목 | 타각 시점의 위도·경도·정확도, 기기 식별 UUID(앱이 생성), 브라우저 정보 |
| 수집 시점 | 출근·퇴근·휴게 버튼을 누른 순간 1회. 백그라운드 수집 없음 |
| 목적 | 근무 장소 확인을 통한 근태 기록의 정확성 확보 |
| 보존 | 좌표 원값은 **90일 후 파기**(거리·판정 결과만 근태와 함께 보존) — §13 결정 필요 |
| 동의 | 별도 동의 화면, 버전 관리, 철회 가능. 거부해도 불이익 없이 "위치 없이 기록 → 관리자 확인" 경로 제공 |
| 열람 권한 | 좌표 원값은 owner만, admin은 거리·판정만 |
| 법적 검토 | 개인정보보호법상 동의·고지 요건, 위치정보법상 사업자 신고 필요 여부 **[미검증 — 노무사/법무 확인 필요]**. 확인 전에는 1단계를 운영 배포하지 않는다 |

### 11.1 법정 보존과 좌표 파기의 관계

| 자료 | 보존 | 근거 |
|---|---|---|
| 근태 행(근무일·시작·종료·휴게·근무분·상태·승인자) | 3년 이상 | 근로기준법 제42조(계약 서류 3년 보존) — 출퇴근 기록이 시행령상 대상 서류에 포함되는지 **[미검증 — 노무사 확인]** |
| 타각 판정 결과(`geo_verdict`, `distance_m`, 의심 신호) | 근태 행과 같이 | 이의제기 시 근거 |
| 좌표 원값(위도·경도·정확도) | 90일(D2) 후 NULL 처리 | 최소수집 원칙. 파기 배치는 행을 지우지 않고 좌표 컬럼만 비운다 |

파기 배치는 매일 1회, 처리 건수를 감사로그 `attendance.geo_purge` 로 남긴다. DELETE 는 쓰지 않는다.

## 12. 단계별 범위

| 단계 | 범위 | 선행 | 완료기준 |
|---|---|---|---|
| 0 | 직원 멤버십 R2 운영 반영, 근태 서버 저장 운영 배포 | — | 직원 계정 `clock/state` 200, 기존 근태 CRUD 운영 동작 |
| 1 | FR-01~09: 설치형 웹앱, 동의, 출퇴근, 반경 판정, 지점 설정, 확인필요 | 0, §13 결정 | 단위 테스트 PASS, 3개 지점 좌표 등록, 실기기(Android 1·iPhone 1) 출퇴근 캡처 |
| 2 | FR-10~14: 휴게, 정정 요청, 지각·조퇴 표시, 급여 기준값, 의심 신호 | 1, 계약정보 반영 작업 | 월마감 화면에서 근태 합계 = 급여 기준값 |
| 3 | FR-15~16: 매장 QR, 출근 알림 | 2, 부정 사례/요청 발생 | 선택 |

## 13. 미결 결정 (CEO)

| # | 결정할 것 | 권장 | 이유 |
|---|---|---|---|
| D1 | 허용 반경 | ✅ **확정: 50m 안** (CEO 2026-09-30 16:01 KST). 정확도 여유 없음, 서버 상한 50m | 초안 권장 100m 대신 CEO 결정 반영 |
| D2 | 좌표 원값 보존기간 | 90일 | 월마감·이의제기 기간을 넘기고 최소 보관 |
| D3 | 성신여대점 주소·좌표 | 사장님이 매장에서 "지금 위치로 설정" 1회 | 현재 주소 공란 [코드 확인] |

## 14. 테스트·수용 기준

| 구분 | 항목 |
|---|---|
| 단위 | 하버사인 거리(경계 49/50/51m), 정확도 50m 경계(`low_accuracy`), 반경 설정 51m 이상 거부(400), verdict 7종 → 상태 매핑, 60초 중복 차단, idempotency 재전송, 자정 넘김, 동의 없음 시 좌표 미저장 |
| 권한 | 직원이 타인 이메일로 타각 불가, 관리자 대리 타각 403, admin에게 좌표 원값 미노출 |
| 회귀 | 기존 수기·CSV 근태 CRUD, 기존 `test_obys_attendance_api.py` 전부 PASS |
| E2E | 실기기 2종에서 설치→동의→출근→퇴근→관리자 확인필요 처리까지 캡처. 브라우저 E2E 불가 시 API 폴백(clock/state·punch 200) 명시 |
| 운영 | 도입 첫 주 자동 확정 비율, 확인필요 사유 분포, 좌표 파기 배치 실행 확인 |

## 15. 리스크

| 리스크 | 영향 | 대응 |
|---|---|---|
| 위치 조작 앱 | 부정 출근 | 의심 신호 기록 + QR 보조(3단계) |
| 실내 GPS 오차(반경 50m라 영향 큼) | 확인필요 과다 | 첫 주 비율 측정, 20% 초과 시 매장 QR(FR-15) 앞당김 제안. 반경 확대는 CEO 결정 사항 |
| iOS 설치 난이도 | 설치율 저조 | 그림 안내, 설치 없이 브라우저에서도 동작 |
| 직원 계정 403 미해결 | 기능 자체 사용 불가 | 0단계를 하드 선행으로 둠 |
| 법적 요건 미확인 | 과태료·분쟁 | §11 확인 전 운영 배포 금지 |
| 기존 UNIQUE 제약 | 같은 분 재출근 충돌 | 60초 차단 + 409 안내 |

## 16. 구현 메모 (러너 지시용)

- 대상 파일: `app/api/obys_workspaces.py`(라우트), `app/static/apps/obys/index.html`(화면), 신규 `app/static/apps/obys/manifest.webmanifest`, `app/static/apps/obys/sw.js`, `migrations/2026xxxx_obys_attendance_mobile.sql`, 테스트 `tests/unit/test_obys_attendance_clock.py`.
- 기존 `attendance_worked_minutes`·`_attendance_audit` 재사용. 근무분을 클라이언트에서 계산하지 않는다.
- 서비스워커 파일은 `app/static/apps/obys/sw.js`, scope는 `/static/apps/obys/` 로 한정해 AADS 다른 화면에 영향이 없게 한다. 공개 주소 `https://fb.newtalk.kr/` 는 `/static/apps/obys/index.html?v=…` 로 302 이동하므로(2026-09-30 실측) 설치 시작 주소도 이 경로를 쓴다. `?v=` 캐시 무효화 규칙과 서비스워커 캐시 버전을 같은 값으로 묶는다.
- push까지만. 운영(진아서버) 반영은 별도 승인.
