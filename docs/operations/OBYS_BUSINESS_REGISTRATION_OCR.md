# 오비서 사업자등록증 원본 보관 · OCR 제안 · 관리자 확인 저장

TASK: AADS-OBYS-BIZLICENSE-ORIGINAL-OCR-20260930 (2026-09-30)

## 1. 흐름

```
[사업자 페이지] 목록에서 [수정] → 사업자등록증 파일 선택
   │ ① POST /api/v1/yeoljeong-finance/businesses/{id}/registration-document   (원본 보관, 버전 +1)
   │ ② POST .../registration-document/{document_id}/ocr                       (제안만, 저장 안 함)
   ▼
폼 아래 비교표: 항목 | OCR 값(검증·신뢰도) | 현재 값 | [적용]
   │ 관리자가 [적용] 누른 항목만 폼 칸에 채워짐
   ▼
[사업자 저장] (기존 경로 그대로: settings().businesses → PUT /settings)
```

- OCR 은 **제안만** 한다. OCR 엔드포인트는 사업자 레코드를 바꾸지 않는다.
- 업로드·OCR 이 실패해도 폼 입력은 그대로 남는다. 화면에 실패 사유가 나오고, 손으로 입력해 계속 저장하면 된다.
- 등록증은 **기존 사업자**에만 올릴 수 있다(`yeoljeong_businesses` 에 현재 테넌트 행이 있어야 한다). 신규 사업자는 먼저 저장한 뒤 [수정]으로 다시 열어서 올린다.
- 모두 **관리자(멤버십 role owner/admin)만** 할 수 있다. 다른 역할은 403, 다른 테넌트의 사업자는 404 다.

## 2. 엔드포인트

| 메서드 | 경로 (`/api/v1/yeoljeong-finance` 아래) | 설명 |
|---|---|---|
| POST | `/businesses/{business_id}/registration-document` | multipart `file`. `.pdf/.jpg/.jpeg/.png`, 10MB 이하. 형식·크기·서명 위반은 400 |
| GET | `/businesses/{business_id}/registration-document` | `documents`(최신순), `current`(최신 활성 1건), `count` |
| GET | `/businesses/{business_id}/registration-document/{document_id}/download` | 원본. 파일명 `사업자등록증_v{n}_{정리된 원본명}{ext}`, `Cache-Control: no-store` |
| POST | `/businesses/{business_id}/registration-document/{document_id}/ocr` | 제안 전용: `suggested`, `current`, `ocr`, `applied=false` |
| DELETE | `/businesses/{business_id}/registration-document/{document_id}` | soft delete(`deleted_at`, `deleted_by`)만. 파일은 남는다 |

응답의 문서 항목: `id, business_id, version, original_filename, content_type, extension, byte_size, sha256, uploaded_by(created_by), uploaded_at(created_at), is_current`.
업로드 응답의 `status` 는 `stored`(새 버전) 또는 `duplicate`(최신본과 sha256 이 같음 — 새 버전을 만들지 않는다)다.

## 3. 저장 위치

| 무엇 | 어디 | 비고 |
|---|---|---|
| 화면 사업자 정보(상호·등록번호·대표자·개업일·주소·메모) | 화면 `settings().businesses` → `PUT /settings`. 서버는 파일 `settings.json` 의 `ui_settings.businesses` 에 쓰고, DB 풀이 있으면 `yeoljeong_businesses` 열로 upsert 한다(`save_settings_persisted`, 기존 동작). DB 모드의 `GET /settings` 는 `yeoljeong_businesses` 에서 다시 읽는다 | `yeoljeong_settings(scope='ui')` 에는 계좌·직원·연동만 있다 |
| 사업자 레지스트리 | `yeoljeong_businesses` 표 (`tenant-registry` API 의 `update_business` 도 같은 표) | 이번 작업에서 두 경로를 통합하지 않았다 |
| 등록증 버전 메타데이터(원본명·크기·확장자·sha256·업로더·시각) | `yeoljeong_business_registration_documents` 표 (신규) | 사업자별 `version` 1,2,3… 삭제된 번호도 재사용하지 않는다 |
| 등록증 원본 파일 | `$OBYS_UPLOAD_ROOT/<tenant_id>/<sha256(business_id) 앞 32자>/business_registration/<uuid>.<ext>` | 기본 `OBYS_UPLOAD_ROOT=app/data/yeoljeong_finance/uploads/ledgers`. 원장과 같은 루트, 다른 하위 폴더 |
| 화면 → 문서 참조 | 원본은 `yeoljeong_business_registration_documents` (business_id 기준 최신 활성 1건). 수정 화면을 열 때 `GET …/registration-document` 로 현행본을 읽는다. 저장 시 `ui_settings.businesses[].registrationDocument = {id, version, sha256, originalFilename, uploadedAt}` 도 함께 붙지만, 표에 열이 없어 **파일 모드에서만** 남는 편의 사본이다 | 예전의 메모 문자열 `사업자등록증 파일: <파일명>` 은 더 이상 만들지 않는다(기존 메모는 그대로 둠) |
| OCR 실행 기록 | `yeoljeong_audit_logs` (`action=business_registration.ocr_suggest`) | 문서 id·버전·성공 여부·소요 ms·원문 길이·항목별 `found/valid/value_length`. **OCR 원문과 추출값은 남기지 않는다** |
| 업로드·삭제 기록 | `yeoljeong_audit_logs` (`business_registration.upload` / `.delete`) | 버전·sha256·크기·확장자 |

## 4. OCR 항목 → 사업자 필드

| OCR 항목 | 화면 필드 / 표 열 | 정규화 | 검증 | 자동채움 후보 |
|---|---|---|---|---|
| 상호(`name`) | `name` / `name` | 공백만 정리 | 없음(교정 금지) | 예 |
| 사업자등록번호(`registration_no`) | `registrationNo` / `registration_no` | `000-00-00000` (하이픈 없는 10자리도 붙임) | 국세청 검증번호(체크섬). 불일치면 `valid=false` + 사유 | 체크섬 통과 시만 |
| 대표자(`representative`) | `representative` / `representative` | 공백만 정리 | 없음 | 예 |
| 개업일(`opened_at`) | `openedAt` / `opened_at` | `YYYY-MM-DD` ("2025년 4월 1일", `2025.04.01` 도 처리) | 존재하는 날짜, 미래 날짜면 `valid=false` | 유효할 때만 |
| 사업장 주소(`address`) | `address` / `address` | 공백만 정리 | 없음 | 예 |
| 법인등록번호(`corporate_registration_no`) | — | `000000-0000000` | 형식 | 아니오(참고) |
| 과세유형(`tax_type`) | — | 일반과세/간이과세/면세 | 표시 문구 | 아니오(참고) |

읽지 못한 항목은 `value=null` + `reason` 이다. 값을 추측해 만들지 않는다. 등록번호는 "등록번호" 표시를 못 찾았을 때만, 원문 전체에서 `000-00-00000` 모양이 **정확히 한 종류**일 때 그것을 쓰고 신뢰도를 낮춘다(0.6배).

OCR 호출은 기존 브리지 `app/core/local_ocr_bridge.py:ocr_extract` (CEO PC Agent 경유)다. PC Agent 가 없으면 `ocr.ok=false` 와 사유를 돌려주고, 원본은 이미 보관된 상태로 남는다. PDF 를 PC Agent OCR 이 읽는지는 에이전트 쪽 구현에 달려 있다 — 읽지 못하면 이미지(jpg/png)로 다시 올린다.

## 5. 코드 기본값이 등록값을 덮지 않는다

- `_canonicalize_ui_settings` 는 이제 **저장된 상호를 그대로** 쓴다. canonical 상호(`CANONICAL_BUSINESSES`)는 저장값이 비어 있을 때만 쓴다. 지점명(`CANONICAL_BRANCHES`)도 같다. id 와 별칭 맵, 지점→사업자 매핑은 바꾸지 않았다.
- `"기초등록 필요"` / `"미등록"` / `"-"` 는 값이 아니라 상태다. API 로 나갈 때 빈 문자열로 바꾸고, 대신 다음을 붙인다.
  - settings: `needs_registration_info`, `missing_registration_fields` (`registrationNo/representative/openedAt/address`)
  - `GET /tenant-registry/businesses`: 같은 두 필드 (`registration_no/representative/opened_at/address`)
- 화면은 이 플래그로 "기초등록 필요" 배지를 보여준다. 등록번호 칸에 자리표시자를 넣지 않는다.
- seed 의 성신여대점·언니냉면 자리표시자는 빈 값으로 바꿨다. 이 값을 읽는 곳을 확인했다: 계약서 기본값(`employer_registration_no`)은 빈 값이면 채우지 않고 `_missing_contract_value` 가 전과 같이 누락으로 판정한다. 은행 범위(`entityType`)는 해당 없음.
- **영향:** 다음에 관리자가 [사업자 저장]을 누르면 `save_settings_persisted` 가 `name` 에 canonical 상호 대신 저장된 상호를 쓴다. 등록 4항목은 DB 값이 비어 있을 때만 채운다(10절). 이 작업은 운영 DB 를 직접 고치지 않는다.

## 6. 마이그레이션 (오비서 업무 DB 전용, 수동)

`migrations/20260930_obys_business_registration_documents.sql` — 표 1개와 인덱스 2개 추가만 한다. 멱등하다.
`scripts/migrations_auto_apply_baseline.txt` 에 HOLD 로 올라 있어 AADS 배포가 자동 적용하지 않는다.

```bash
psql "$OBYS_DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/20260930_obys_business_registration_documents.sql
psql "$OBYS_DATABASE_URL" -c "\d yeoljeong_business_registration_documents"
```

롤백: `migrations/rollback/20260930_obys_business_registration_documents.down.sql` (표를 먼저 덤프한다. 원본 파일은 디스크에 남는다).
마이그레이션 전에 이 코드가 배포되면 등록증 엔드포인트만 500 이 나고, 기존 사업자·원장 화면은 영향이 없다.

## 7. 보존

- 원본과 모든 이전 버전을 보관한다. 자동 삭제는 없다. DELETE 는 soft delete 라 파일과 행이 남는다.
- 보존 기간 기준: 국세기본법의 장부·증빙서류 보존기간(법정신고기한부터 5년)을 따른다. 폐업한 사업자도 마지막 신고기한부터 5년이 지나기 전에는 파기하지 않는다. 파기는 별도 승인을 받아 수동으로 하고, 파기한 `sha256` 목록을 남긴다.
- 원본대조: 파일 `sha256sum` 과 표의 `sha256` 이 같아야 한다.

```bash
sha256sum "$OBYS_UPLOAD_ROOT/<tenant>/<hash>/business_registration/<uuid>.pdf"
```

## 8. 사업자번호 미비 사업자 조회

레지스트리 표:

```sql
SELECT tenant_id, id, name, registration_no, representative, opened_at, address
  FROM yeoljeong_businesses
 WHERE deleted_at IS NULL
   AND (COALESCE(btrim(registration_no), '') IN ('', '-', '미등록', '기초등록 필요')
        OR COALESCE(btrim(representative), '') IN ('', '-', '미등록', '기초등록 필요')
        OR COALESCE(btrim(opened_at), '') IN ('', '-')
        OR COALESCE(btrim(address), '') IN ('', '-'))
 ORDER BY tenant_id, sort_order, id;
```

화면 settings 파일 모드(`$YEOLJEONG_FINANCE_DATA_DIR/settings.json`):

```bash
jq -r '.ui_settings.businesses[] | select((.registrationNo // "" | gsub("^\\s+|\\s+$";"")) as $n | ($n == "" or $n == "-" or $n == "미등록" or $n == "기초등록 필요")) | [.id, .name] | @tsv' settings.json
```

API 로는 `GET /api/v1/yeoljeong-finance/settings` 의 `businesses[].needs_registration_info=true`, 또는 `GET /api/v1/yeoljeong-finance/tenant-registry/businesses` 의 `needs_registration_info=true` 행이다.

등록증이 한 번도 안 올라온 사업자:

```sql
SELECT b.tenant_id, b.id, b.name
  FROM yeoljeong_businesses b
 WHERE b.deleted_at IS NULL
   AND NOT EXISTS (SELECT 1 FROM yeoljeong_business_registration_documents d
                    WHERE d.tenant_id = b.tenant_id AND d.business_id = b.id AND d.deleted_at IS NULL);
```

## 9. 화면 검증

1. 관리자로 로그인 → 오비서 `/apps/obys/` → 사업자 페이지(사업자 신규등록/수정).
2. 목록에서 "언니냉면" 행 → 등록번호 칸이 "기초등록 필요" **배지**로 나오는지 본다(글자값이 아님). 보완 % 가 자리표시자를 채운 값으로 세지 않는지 본다.
3. [수정] → 사업자등록증 파일에 jpg/png/pdf 를 고른다 → "원본 보관 v1 · 파일명 · bytes · sha256…" 줄이 나오는지 본다.
4. PC Agent 가 붙어 있으면 비교표가 나온다. 체크섬이 틀린 번호는 빨간 사유 배지와 "적용 불가"로 나온다. [적용]을 누른 칸만 바뀌는지, 누르기 전에는 폼이 그대로인지 본다.
5. PC Agent 가 없으면 "OCR 실패 … 손입력으로 계속" 이 나오고 폼 입력이 그대로인지 본다.
6. [사업자 저장] → 상세현황에 "등록증 원본 v1 …" 이 나오고 메모에 파일명 문자열이 붙지 않는지 본다.
7. 같은 파일을 다시 고르면 "최신본과 동일 파일"이 나오고, 다른 파일이면 v2 가 된다.
8. 비관리자 계정에서는 파일 선택 시 "관리자만" 안내가 나오고 업로드하지 않는지 본다.

## 10. 사업자 기초정보 우선순위와 설정 동기화 (AADS-OBYS-BIZ-DEFAULTS-NO-OVERWRITE-20260930)

우선순위: **관리자 직접 입력 > DB 실값 > 파일 원장 > 코드 기본값(`CANONICAL_BUSINESSES`)**.

- 관리자 직접 입력 = `obys_upload_service.update_business`. 보낸 항목은 그대로 덮어쓴다.
- 설정 동기화 = `yeoljeong_finance_service.save_settings_persisted`. `registration_no`·`representative`·`opened_at`·`address` 네 컬럼은 **DB 값이 비어 있을 때만** 채운다. 나머지(`name`·`entity_type`·`tax_type`·`memo`·`sort_order`)는 예전처럼 덮어쓴다.
- 자리표시자(`BUSINESS_PLACEHOLDER_VALUES` = `기초등록 필요`, `미등록`)는 빈 값과 같다 — 들어오는 값이 자리표시자면 빈 값으로 바꿔 보내고, DB 에 이미 자리표시자가 들어 있으면 "비어 있음"으로 보아 실값이 채운다.
- SQL 은 `BUSINESS_SETTINGS_UPSERT_SQL`(`_build_business_settings_upsert_sql`)에서 만든다. 스키마 변경 없음.
- 한계: 설정 화면에서 이미 값이 있는 네 항목을 바꿔 저장해도 반영되지 않는다. 바꾸려면 사업자 수정(`update_business`)을 쓴다.

| DB 기존 값 | 들어온 값 | 결과 |
|---|---|---|
| 실값 | 실값 / 빈값 / 자리표시자 | 기존 실값 유지 |
| 빈값·공백 | 실값 | 들어온 실값으로 채움 |
| 빈값·공백 | 빈값 / 자리표시자 | 빈값 |
| 자리표시자 | 실값 | 들어온 실값으로 채움 |
| 자리표시자 | 빈값 / 자리표시자 | 빈값으로 정리됨(기존 자리표시자는 빈 것으로 간주돼 빈 EXCLUDED 로 교체) |

확인 쿼리(등록 4항목이 비었거나 자리표시자인 사업자):

```sql
SELECT id, name, registration_no, representative, opened_at, address, updated_by, updated_at
  FROM yeoljeong_businesses
 WHERE deleted_at IS NULL
   AND (COALESCE(TRIM(registration_no), '') IN ('', '기초등록 필요', '미등록')
     OR COALESCE(TRIM(representative), '') IN ('', '기초등록 필요', '미등록')
     OR COALESCE(TRIM(opened_at), '') = ''
     OR COALESCE(TRIM(address), '') = '');
```
