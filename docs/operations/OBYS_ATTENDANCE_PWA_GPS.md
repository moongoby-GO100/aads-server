# 오비서 PWA 출퇴근(GPS 반경) 운영 절차

TASK: AADS-OBYS-PWA-GPS-ATTENDANCE-20260930 · 작성 2026-09-30 KST
관련: `docs/prd/20260930_OBYS_ATTENDANCE_PWA_GPS_PRD.md`(r3), `app/api/obys_workspaces.py`

## 0. 선행 조건

- **직원 테넌트 멤버십(runner-0f81541a, R2)** 이 운영에 반영되어야 한다. 직원 계정이 고용주
  테넌트의 활성 멤버십(owner/admin/member)을 가져야 출퇴근 API가 열린다. 없으면 403 이 정상이다.
  이 기능은 테넌트 게이트를 우회하지 않는다.
- 아래 §1 마이그레이션이 오비서 업무 DB 에 적용되어야 한다. 적용 전에는 출퇴근 API 가 500
  (없는 컬럼)을 낸다. 기존 근태 목록·수기 입력 API 는 새 컬럼을 읽지 않으므로 영향이 없다.

## 1. 마이그레이션 (수동, 오비서 업무 DB 전용)

AADS DB 에 돌리지 않는다. `scripts/migrations_auto_apply_baseline.txt` 에 HOLD 로 올라가 있다.

```bash
psql "$OBYS_DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/20260930_obys_attendance_pwa_gps.sql
```

- 추가만 한다: `yeoljeong_branches` 3열, `yeoljeong_attendance_records` 13열, 모두 NULL 허용·기본값 없음.
- `source` 에 CHECK 가 있으면 기존 조건에 `OR source = 'pwa'` 만 더한다. 원래 조건은 새 제약
  `yeoljeong_attendance_records_source_pwa_check` 의 COMMENT 에 남는다(NOTICE 로도 출력).
- 기존 근태 행의 status 를 재판정하지 않는다.

롤백: `migrations/rollback/20260930_obys_attendance_pwa_gps.down.sql` — `source='pwa'` 행이 있으면 멈춘다(§8).

## 2. 반경 판정 규칙

반경은 CEO 결정 D1(2026-09-30) "매장 반경 50m 안" 을 따른다. 코드 상수:
`GEOFENCE_DEFAULT_RADIUS_M=50`, `GEOFENCE_MIN_RADIUS_M=30`, `GEOFENCE_MAX_RADIUS_M=50`.
지점별 `geofence_radius_m` 이 있으면 그 값(상한 50m), 없으면 기본 50m. 정확도 여유를 더하지 않는다.

| 입력 조건 (위에서부터 먼저 걸리는 것) | geofence_result | status |
|---|---|---|
| 위치 미동의(서버 동의 없음 또는 요청 `location_consent=false`) — 좌표 저장 안 함 | `unknown` | `pending` |
| 지점 좌표 미등록(또는 지점 없음) — 사유 "지점 좌표 미등록" | `unknown` | `pending` |
| 좌표 없음(권한 거부·측정 실패) | `unknown` | `pending` |
| `accuracy_m` 없음 | `unknown` | `pending` |
| `accuracy_m` > 반경 (오차가 반경보다 큼 — 안으로 판정하지 않음) | `unknown` | `pending` |
| 거리(하버사인, 올림) ≤ 반경 | `inside` | `approved` |
| 거리 > 반경 — memo 에 거리(m) 기록 | `outside` | `pending` |

퇴근 시 행 전체 판정: 출근·퇴근 **둘 다** `inside` 면 `inside`/`approved`, 한쪽이라도 `outside` 면
`outside`/`pending`, 그 밖은 `unknown`/`pending`. 관리자가 이미 `rejected` 로 둔 행은 `rejected` 유지.

## 3. 지점 좌표·반경 등록

관리 화면: 오비서 → 사업자/지점 화면 "지점 목록" 옆 **출퇴근 위치(좌표·반경) 설정**
(`/static/apps/obys/attendance-admin.html`, 소유자·관리자만).

1. 사장님이 매장 안(입구 근처)에서 관리 화면을 연다.
2. 해당 지점의 **지금 위치로** → 측정 오차가 30m 를 넘으면 경고가 뜬다. 다시 측정한다.
3. 반경(30~50m, 기본 50m)을 확인하고 **저장**. `PATCH /api/v1/workspaces/{business_id}/branches/{branch_id}/geofence`
   가 호출되고 감사로그 `attendance.geofence_config` 가 남는다.
4. 좌표를 모르면 지도 서비스에서 매장 좌표(위도, 경도)를 복사해 입력해도 된다.

성신여대점처럼 주소가 비어 있는 지점은 좌표가 등록될 때까지 모든 출퇴근이 "지점 좌표 미등록" 확인필요로 남는다.

## 4. 직원 설치 안내

주소: `https://<오비서 도메인>/static/apps/obys/clock.html`

- 먼저 오비서(`/static/apps/obys/index.html`)에 직원 계정으로 로그인한다(같은 브라우저).
- **Android(Chrome)**: 출퇴근 화면 아래 **홈 화면에 추가** 버튼, 또는 메뉴 ⋮ → 앱 설치.
- **iPhone(Safari)**: 공유 버튼 → **홈 화면에 추가**. iOS 는 설치 앱과 Safari 의 저장소가 분리될 수
  있어, 설치 후 첫 실행에서 로그인 안내가 나오면 설치 앱 안에서 로그인한다.
- 서비스워커 범위는 `/static/apps/obys/` 다. 캐시는 앱 셸(clock.html·manifest·아이콘)만 담고
  API 응답·근태·토큰은 담지 않는다. 셸을 바꿔 배포하면 `sw.js` 의 `CACHE_VERSION` 을 올린다.
- 오프라인이면 "지금 기록할 수 없음 + 재시도" 만 보인다. 기기에 쌓았다가 나중에 올리는 기능은 없다.

## 5. 위치정보 동의·철회

- 최초 실행 시 동의 화면: 수집 항목(버튼 누른 순간의 위치 1점), 목적(근태 확인), 보관 기간,
  거부 시 결과(출퇴근은 되지만 확인필요)를 보여준다.
- **동의합니다** → `POST .../attendance/consent {"agree": true}` → 감사로그 `attendance.consent`.
  이후 기록 행의 `location_consent_at` 에 동의 시각이 들어간다.
- **동의하지 않음** → 서버에는 동의가 없으므로 위치를 보내더라도 저장하지 않는다. 모두 pending.
- 철회: 화면 아래 **위치 동의 철회** → `{"agree": false}` → 감사로그 `attendance.consent_withdraw`.
  서버는 가장 최근 동의/철회 기록을 기준으로 판단한다(기기를 바꿔도 같다).
- 위치는 `getCurrentPosition` 1회만 읽는다. 연속 추적·백그라운드 수집은 없다.
- 보관: 화면 안내는 "좌표 90일, 출퇴근 시각 3년" 이다(PRD D2 권장안). **자동 파기 작업은 1차 범위에 없다** —
  §7 의 파기 쿼리를 월 1회 운영자가 실행한다. D2 가 확정되면 기간을 바꾸고 자동화한다.

## 6. 확인필요(pending) 처리 흐름

1. 관리 화면 "출퇴근 위치 판정" 에서 판정=반경 밖/판정 불가 로 조회한다
   (`GET .../attendance/locations?result=outside`). 거리·정확도·사유가 보인다. 좌표 원본은 체크박스로만 표시.
2. 사유별 조치
   - 반경 밖: 배달·외근 등 정당한 사유가 확인되면 오비서 근태 화면에서 해당 행을 **수정 → 상태 확정**.
   - GPS 오차 과대: 실내 오차가 원인인 경우가 많다. 직원 확인 후 확정.
   - 지점 좌표 미등록: §3 으로 좌표를 먼저 등록한다(과거 행은 자동 재판정하지 않는다). 개별 확정.
   - 위치 미동의: 출근부·CCTV 등 다른 근거로 확정.
3. 상태 변경은 기존 `PUT .../attendance/records/{id}` 로 하며 감사로그 `attendance.update` 가 남는다.
4. 첫 주 확인필요 비율이 20% 를 넘으면 매장 QR(FR-15) 앞당김을 CEO 에게 올린다(반경 확대는 CEO 결정 사항).

## 7. 확인 쿼리 (오비서 업무 DB, 읽기 전용)

```sql
-- 컬럼 적용 확인
SELECT column_name, is_nullable FROM information_schema.columns
 WHERE table_name IN ('yeoljeong_branches','yeoljeong_attendance_records')
   AND column_name IN ('latitude','longitude','geofence_radius_m','check_in_at','check_out_at',
                       'geofence_result','location_consent_at') ORDER BY table_name, column_name;

-- 지점 좌표 등록 현황
SELECT business_id, id, name, latitude IS NOT NULL AS has_coords, geofence_radius_m
  FROM yeoljeong_branches WHERE deleted_at IS NULL ORDER BY business_id, sort_order;

-- 오늘 PWA 출퇴근 판정 분포
SELECT business_id, geofence_result, status, count(*)
  FROM yeoljeong_attendance_records
 WHERE source = 'pwa' AND deleted_at IS NULL AND work_date = (now() AT TIME ZONE 'Asia/Seoul')::date
 GROUP BY 1, 2, 3 ORDER BY 1, 2, 3;

-- 퇴근 누락(24시간 넘게 열린 출근)
SELECT id, business_id, employee_email_masked, work_date, check_in_at
  FROM yeoljeong_attendance_records
 WHERE source = 'pwa' AND check_out_at IS NULL AND deleted_at IS NULL
   AND check_in_at < now() - interval '24 hours';

-- 동의 없이 좌표가 저장된 행(항상 0 이어야 한다)
SELECT count(*) FROM yeoljeong_attendance_records
 WHERE source = 'pwa' AND location_consent_at IS NULL
   AND (check_in_lat IS NOT NULL OR check_out_lat IS NOT NULL);
```

좌표 파기(보관기간 경과분, 운영자 승인 후 실행 — 거리·판정·시각은 남기고 좌표만 지운다):

```sql
UPDATE yeoljeong_attendance_records
   SET check_in_lat = NULL, check_in_lng = NULL, check_out_lat = NULL, check_out_lng = NULL, updated_at = now()
 WHERE source = 'pwa' AND work_date < (now() AT TIME ZONE 'Asia/Seoul')::date - 90
   AND (check_in_lat IS NOT NULL OR check_out_lat IS NOT NULL);
```

## 8. 되돌리기

1. 코드 되돌림(출퇴근 라우트 제거)을 먼저 배포한다 — 신규 PWA 기록이 멈춘다.
2. `source='pwa'` 행을 내보내 보관한다(`\copy (SELECT ... WHERE source='pwa') TO ...`).
3. 남길 행은 관리자 판단으로 수기 행으로 옮기거나 soft delete 한다(데이터 변경은 CEO 승인 사항).
4. 그 뒤에만 롤백 SQL 을 실행한다 — `pwa` 행이 남아 있으면 롤백 SQL 이 스스로 멈춘다.

## 9. API 목록

| 메서드 | 경로 (`/api/v1/workspaces` 아래) | 권한 |
|---|---|---|
| GET | `/{business_id}/attendance/clock/state` | 직원 본인 (좌표 원본 없음) |
| POST | `/{business_id}/attendance/consent` | 직원 본인 |
| POST | `/{business_id}/attendance/check-in` | 직원 본인 (본문 이메일 무시, 대리 불가) |
| POST | `/{business_id}/attendance/check-out` | 직원 본인 |
| GET | `/{business_id}/attendance/me/{record_id}` | 직원 본인 (타인 기록 403) |
| GET | `/{business_id}/attendance/locations` | 소유자·관리자 (좌표 원본 포함) |
| GET | `/{business_id}/branches/geofence` | 소유자·관리자 |
| PATCH | `/{business_id}/branches/{branch_id}/geofence` | 소유자·관리자 (감사로그) |
