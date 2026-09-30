# 오비서 계약서 — 서명요청 알림 · 서명본 PDF 보관/교부 운영 문서

TASK: AADS-OBYS-CONTRACT-NOTIFY-PDF-20260930 · 대상: 오비서(열정국밥) 진아서버 인스턴스

## 1. 무엇이 바뀌었나

| 시점 | 동작 | 실패 시 |
|---|---|---|
| `POST /api/v1/yeoljeong-finance/contracts/{id}/request-signature` | sign_token 발급 후 직원에게 알림 발송, 채널별 이력 기록 | 서명요청은 200 유지, 응답 `notify.status=failed`, 이력 `failed` |
| `POST /api/v1/yeoljeong-finance/contracts/signing` | 서명 봉인 후 `signed_snapshot` 으로 PDF 생성·보관, `contract_signed` 알림 | 서명은 유지, 계약서 `signed_pdf_error` 에 사유, 응답 `signed_pdf.status=failed` |
| `POST /api/v1/yeoljeong-finance/contracts/{id}/resend-signature-notice` (신규) | 관리자만, `status=requested` 만, 최근 이력 기준 5분 제한 | 403 / 409 / 429, 이력 저장소를 못 읽으면 503 |
| `GET /api/v1/yeoljeong-finance/contracts/{id}/signed-pdf` (신규) | 관리자·계약 당사자 본인만. 당사자 다운로드 시 교부 기록 | 다른 테넌트·제3자·없는 ID 모두 403, 미서명 409 |
| `POST /api/v1/yeoljeong-finance/contracts/{id}/signed-pdf/regenerate` (신규) | 관리자만. 봉인 스냅샷으로 다시 만들기 | 미서명 409 |

응답 호환: 기존 `{"contract": ...}` 키는 그대로이고 `notify`, `signed_pdf` 키가 추가됐다.

## 2. 알림 채널 활성화 (환경변수, `.env` 에만 둔다 — R-KEY)

| 변수 | 기본값 | 의미 |
|---|---|---|
| `OBYS_CONTRACT_NOTIFY_CHANNELS` | `inapp` | 쉼표 구분. `inapp`, `sms`, `alimtalk`. **기본값에서는 문자·알림톡이 절대 나가지 않는다** |
| `OBYS_CONTRACT_ALIMTALK_TEMPLATE_SIGN_REQUEST` | (없음) | 서명요청 알림톡 템플릿 코드(카카오 사전심사 완료분). 없으면 알림톡 skip |
| `OBYS_CONTRACT_ALIMTALK_TEMPLATE_SIGNED` | (없음) | 서명완료 알림톡 템플릿 코드 |
| `OBYS_CONTRACT_SIGN_BASE_URL` | (없음) | 문자 본문에 넣을 서명 화면 주소. 예: `https://<오비서 도메인>/apps/obys/` → `?yf_contract_token=...` 가 붙는다. 없으면 링크 없이 "앱에서 확인" 문구만 |
| `ALIGO_API_KEY` / `ALIGO_USER_ID` / `ALIGO_SENDER` / `ALIGO_SENDER_KEY` | 기존 | 알리고 인증·발신번호. 미설정이면 sms/alimtalk 은 `skipped (aligo_unavailable)` |

- 채널 순서는 inapp → sms → alimtalk 이고, **켜진 채널은 모두 시도한다**(sms 와 alimtalk 을 둘 다 켜면 둘 다 간다).
- 알림톡은 `failover_sms=False` 로 보낸다(대체 문자 이중 발송 방지).
- 외부 채널 본문에는 급여 등 계약 조건을 넣지 않는다. 사내 알림(inapp) 본문에는 sign_token 을 넣지 않는다.
- **실직원 대상 시험 금지.** 켜기 전에는 테스트 직원 계정(본인 번호)으로만 확인한다.

활성화 예 (진아서버, 승인 후):

```bash
# .env
OBYS_CONTRACT_NOTIFY_CHANNELS=inapp,sms
OBYS_CONTRACT_SIGN_BASE_URL=https://<오비서 도메인>/apps/obys/
```

## 3. PDF 보관 경로

- 경로: `${OBYS_UPLOAD_ROOT}/<tenant_id>/contracts/<sha256(contract_id)[:32]>.signed.pdf` (권한 0600)
  - `OBYS_UPLOAD_ROOT` 미설정 시 `${YEOLJEONG_FINANCE_DATA_DIR}/uploads/ledgers` (obys_upload_service 기본값과 동일)
  - 웹 정적 경로가 아니다. 다운로드는 위 API 로만 한다.
- DB 에는 **상대 경로**와 파일 sha256·바이트 수를 남긴다: `contract_payload` 의 `signed_pdf_path`/`signed_pdf_sha256`/`signed_pdf_bytes`/`signed_pdf_generated_at`/`signed_pdf_error` (원본), 마이그레이션 적용 후에는 같은 값이 `yeoljeong_contracts` 컬럼에도 복사된다.
- `yeoljeong_uploads` 는 재사용하지 않았다 — `category CHECK IN ('sales','purchase','transaction')` 라 계약서를 넣으려면 기존 제약을 바꿔야 한다.
- PDF 원본은 `signed_snapshot` 하나다. 생성 전에 스냅샷 해시를 다시 계산해 `signed_snapshot_sha256` 과 다르면 만들지 않는다. 자필서명 PNG 도 봉인된 `signature_sha256` 과 대조한다.
- 폰트: `app/assets/fonts/NanumGothic-Regular.ttf` (SIL OFL, 저장소 동봉). `OBYS_CONTRACT_PDF_FONT_PATH` 로 바꿀 수 있다. 폰트가 없거나 한글 글리프가 없으면 **생성 실패로 기록**한다(빈칸 PDF 를 만들지 않는다).
- 라이브러리: `reportlab==4.4.10` (순수 파이썬, `pyproject.toml`·`requirements.runtime.lock` 에 고정). 진아서버 venv 에는 `pip install -r requirements.runtime.lock` 로 들어간다 — OS 패키지 불필요.
- 렌더링은 결정적(invariant)이다. 같은 스냅샷이면 같은 바이트·같은 해시가 나온다.

## 4. 재생성 절차

1. 실패 확인: 아래 5-③ 쿼리로 `signed_pdf_error` 가 있는 서명 계약서를 찾는다.
2. 원인 조치(폰트 경로, `OBYS_UPLOAD_ROOT` 쓰기 권한, reportlab 설치 여부).
3. 관리자 계정으로 `POST /api/v1/yeoljeong-finance/contracts/{id}/signed-pdf/regenerate` → 응답 `signed_pdf.status=stored`, `sha256`, `bytes` 확인.
   - 다운로드 API 도 파일이 없거나 해시가 다르면 한 번 자동 재생성을 시도한다(실패 시 503).
4. 서명 완료 계약서의 수정·삭제 금지는 그대로다. 재생성은 PDF 메타만 갱신하고 스냅샷·서명은 건드리지 않는다.

## 5. 마이그레이션과 확인 쿼리

마이그레이션: `migrations/20260930_obys_contract_notify_signed_pdf.sql` (롤백 `migrations/rollback/20260930_obys_contract_notify_signed_pdf.down.sql`).
**오비서 업무 DB 전용**이고 스키마 추가만 한다(기존 계약서 행 값 불변). AADS 자동 적용에서는 baseline 으로 HOLD 된다.

```bash
psql "$OBYS_DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/20260930_obys_contract_notify_signed_pdf.sql
```

마이그레이션 전에도 코드는 동작한다: 이력 INSERT 는 테이블이 없으면 기록하지 않고(`notify.logged=false`), 재발송은 이력을 못 읽으므로 503 으로 막힌다. 컬럼 복사는 컬럼이 다 있을 때만 한다.

① 계약서별 발송 이력

```sql
SELECT created_at, event, channel, status, target_masked, error_detail
  FROM yeoljeong_contract_notifications
 WHERE tenant_id = '<tenant uuid>' AND contract_id = '<contract id>'
 ORDER BY created_at DESC;
```

② 교부 기록 (최초 교부 시각 + 모든 다운로드)

```sql
SELECT c.id, c.employee_name, c.signed_at, c.delivered_at, c.delivery_channel,
       c.signed_pdf_sha256, c.signed_pdf_bytes
  FROM yeoljeong_contracts c
 WHERE c.tenant_id = '<tenant uuid>' AND c.status = 'signed' AND c.deleted_at IS NULL
 ORDER BY c.signed_at DESC;

SELECT contract_id, created_at, target_masked
  FROM yeoljeong_contract_notifications
 WHERE tenant_id = '<tenant uuid>' AND event = 'delivered'
 ORDER BY created_at DESC;
```

③ PDF 미생성·실패 서명 계약서

```sql
SELECT id, employee_name, signed_at, contract_payload->>'signed_pdf_error' AS pdf_error
  FROM yeoljeong_contracts
 WHERE status = 'signed' AND deleted_at IS NULL
   AND COALESCE(contract_payload->>'signed_pdf_sha256', '') = '';
```

- `delivered_at` 은 **계약 당사자 본인이 처음 내려받은 시각**이고 덮어쓰지 않는다. 관리자 다운로드는 교부로 치지 않는다. 매 다운로드는 이력(event=`delivered`, channel=`download`)에 남는다.
- 수신번호·이메일 원문은 이력에 없다(마스킹 `010-****-5678`, `me****@example.com`).

## 6. 알려진 한계

- 재발송 5분 제한은 이력 조회 후 발송하는 방식이라, 같은 순간 두 번 누르면 둘 다 통과할 수 있다(잠금 없음).
- 파일 모드(업무 DB DSN 미설정, 개발용)에서는 이력이 `${YEOLJEONG_FINANCE_DATA_DIR}/contract_notifications.json` 에 쌓인다.
