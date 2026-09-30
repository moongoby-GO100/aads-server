# 오비서 사업자 서류 보관 (AADS-OBYS-BUSINESS-DOCUMENTS-20260930)

설정 > 사업자 화면의 **[사업자 서류]** 패널에서 사업자(법인/개인) 단위 서류를 실제 파일로
보관하고 조회·수정·삭제·다운로드한다. OCR·홈택스 진위확인은 이 기능의 범위가 아니다.

## 1. 적용 (오비서 업무 DB 전용, 수동)

`yeoljeong_business_documents` 표는 **오비서 업무 DB(OBYS_DATABASE_URL, 진아서버)** 에만 만든다.
AADS DB 에 돌리지 않는다. `scripts/migrations_auto_apply_baseline.txt` 에 올려 자동 적용에서
HOLD 되어 있다.

```bash
psql "$OBYS_DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/20260930_obys_business_documents.sql
psql "$OBYS_DATABASE_URL" -c "\d yeoljeong_business_documents"
```

- 마이그레이션은 `yeoljeong_businesses`·`yeoljeong_business_tenant_mapping` 이 없으면 멈춘다(오적용 방지).
- 멱등하다(`IF NOT EXISTS`). 기존 행을 쓰지 않는다.
- 표가 없는 상태에서 DB 모드로 돌면 목록·등록이 **503** 을 낸다. 성공으로 보이지 않는다.
- 롤백: `migrations/rollback/20260930_obys_business_documents.down.sql`
  (메타데이터가 사라진다 — 먼저 `pg_dump -t yeoljeong_business_documents` 로 덤프한다. 원본 파일은 남는다.)

## 2. 보관 경로

| 항목 | 위치 |
|---|---|
| 원본 파일 | `$OBYS_UPLOAD_ROOT/<tenant_id>/business_documents/<문서 uuid><확장자>` (권한 0600) |
| 메타데이터 | `yeoljeong_business_documents` — 경로(상대)·sha256·크기·발급일·만료일·상태 |
| 파일 모드(개발) | `YEOLJEONG_FINANCE_DATA_DIR/business_documents.json` |

- 저장 루트는 서명 계약서 PDF 와 같은 `OBYS_UPLOAD_ROOT`(`_contract_pdf_root()`)다. 새 루트를 만들지 않았다.
- 원본 파일명은 경로에 쓰지 않는다. 다운로드할 때 `original_filename` 으로 돌려준다.
- 다운로드 때 sha256 을 다시 계산해 등록 당시와 다르면 **409** 로 거절한다(변조·손상 탐지).
- 파일은 DB BLOB 으로 넣지 않는다.

## 3. 서류 종류 (서버 `BUSINESS_DOCUMENT_TYPES` 가 원본)

| 코드 | 화면 라벨 | 구분 |
|---|---|---|
| `business_registration` | 사업자등록증 | **필수** |
| `business_permit` | 영업신고증 | 선택 |
| `bankbook` | 통장사본 | 선택 |
| `lease_contract` | 임대차계약서 | 선택 |
| `hygiene_training` | 위생교육수료증 | 선택 |
| `fire_insurance` | 화재보험증서 | 선택 |
| `corporate_registry` | 법인등기부등본 | 법인 조건부 |
| `seal_certificate` | 인감증명서 | 선택 |
| `representative_id` | 대표자신분증 | 선택 |
| `tax_agent_delegation` | 세무대리인위임장 | 선택 |
| `mail_order_report` | 통신판매업신고증 | 선택 |
| `other` | 기타 | 선택 |

화면은 목록 응답의 `document_types` 를 받아 선택지와 라벨을 그린다. 종류를 추가할 때는 서버 목록만 고친다.

## 4. 등록·교체 절차

1. 설정 > 사업자에서 사업자를 고른다(목록의 [수정] 또는 상세현황의 선택 사업자).
2. [사업자 서류] 패널에서 서류 종류·파일·발급일·만료일·메모를 넣고 **[서류 등록]**.
   - 사업자 저장과 **별개 동작**이다. 사업자만 먼저 저장하고 서류는 나중에 올려도 된다.
   - 업로드가 실패하면 성공 토스트를 띄우지 않고 서버가 준 실패 사유를 그대로 보여준다.
3. **교체**: 같은 종류를 다시 올린다. 이전 파일은 지우지 않고 `이전본(superseded)` 으로 남고,
   새 파일이 `최신(current)` 이 된다. 이력은 세무·노무 분쟁의 근거다.
4. **수정**: 목록의 [수정] → 종류·발급일·만료일·메모만 바꾼다. 파일(경로·sha256·크기)은 바뀌지 않는다.
   파일을 바꾸려면 재등록한다.

상한: 파일 10MB(초과 시 413). 허용 확장자: pdf, jpg, jpeg, png, webp, heic, tif, tiff, hwp, doc, docx.

## 5. 만료 관리

- `expires_at` 이 오늘부터 **30일 이내**면 `만료 임박`, 지났으면 `만료` 배지가 붙는다(KST 기준).
- 사업자 목록·상세현황에 `만료 N건`·`만료 임박 N건` 배지가, 현행 사업자등록증이 없으면 `서류 필요` 배지가 뜬다.
- 갱신본을 받으면 같은 종류로 재등록한다(4-3). 이전본은 이력으로 남는다.
- 사업자등록증을 설정 폼의 **등록증 OCR 칸**(`/businesses/{id}/registration-document`, 별도 표
  `yeoljeong_business_registration_documents`)으로 보관한 경우에도 `서류 필요` 배지는 뜨지 않는다.
  다만 그 파일은 [사업자 서류] 목록에는 나오지 않는다 — 발급일·만료일 관리가 필요하면 이 패널에도 등록한다.

## 6. 권한

- 조회·등록·수정·삭제·다운로드 모두 **관리자(owner/admin, 대표·운영 관리자)** 만. 그 외 403.
- 테넌트: 활성 멤버십의 테넌트만. 다른 테넌트 문서는 403.
- 사업자: 요청한 `business_id` 가 테넌트 소속인지 코드에서 확인하고(`yeoljeong_business_tenant_mapping`),
  모든 SQL 의 WHERE 에도 `tenant_id`·`business_id` 를 넣는다(이중 차단). 소속이 아니면 403.
- 업로드는 권한·소속을 먼저 확인하고 나서야 파일 본문을 읽는다.

## 7. 삭제 정책

- **soft delete 만** 한다: `deleted_at`·`status='deleted'` 를 기록하고 목록에서 뺀다. 행과 원본 파일은 남는다.
- 현행본을 삭제하면 같은 종류의 바로 앞 리비전이 다시 현행이 된다.
- 물리 삭제 API 는 없다. 보존기한이 지난 파일의 파기가 필요하면 별도 승인 후 DBA 가 수동으로 한다.

## 8. API

| 메서드 | 경로 (`/api/v1/yeoljeong-finance`) | 설명 |
|---|---|---|
| GET | `/business-documents?business_id=` | 목록(최신순, 상태·만료 플래그, `document_types`, 사업자별 `summary`). `business_id` 생략 시 테넌트 전체 |
| POST | `/business-documents` | multipart: `business_id`·`document_type`·`file` 필수, `issue_date`·`expires_at`·`memo` |
| GET | `/business-documents/{id}/download` | 원본 파일명·content_type 그대로 |
| PATCH | `/business-documents/{id}` | `document_type`·`issue_date`·`expires_at`·`memo` |
| DELETE | `/business-documents/{id}` | soft delete |

## 9. 점검

```bash
# 표와 인덱스
psql "$OBYS_DATABASE_URL" -c "\d yeoljeong_business_documents"
# 사업자·종류마다 현행본이 1건인지 (부분 유일 인덱스가 보장한다)
psql "$OBYS_DATABASE_URL" -c "SELECT business_id, document_type, count(*) FROM yeoljeong_business_documents WHERE status='current' AND deleted_at IS NULL GROUP BY 1,2 HAVING count(*) > 1"
# 단위 테스트
bash scripts/run_unit_tests.sh tests/unit/test_obys_business_documents.py
```
