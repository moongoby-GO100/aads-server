# 배민 4개 매장 전체 백필 구현/보강 계획

작성: 2026-08-25 12:21 KST  
대상 프로젝트: FOOD / 열정국밥 매장비서  
대상 포털: 배민셀프서비스 `https://self.baemin.com/`  
목표: 배민 4개 등록 매장의 주문건별 매출/정산, 리뷰, 광고현황을 누락 없이 누적 백필하고 재수집 가능한 collector 체계로 고정한다.

## 1. 요약

배민 계정은 4개 매장 모두 등록되어 있다. 현재 원장은 주문/정산/리뷰 일부가 적재되어 있지만, 배민 광고현황은 0건이고 리뷰 본문/평점 품질도 대부분 비어 있다. 따라서 "전체 백필 완료"가 아니라 "주문내역 P0 파서 일부 구현 후, 리뷰/광고/전기간 백필 보강 필요" 상태다.

이번 구현 준비의 결론은 아래와 같다.

1. 주문건별 백필은 기존 `baemin_order_history_collector.py`를 확장해 4개 매장을 순차 처리한다.
2. 리뷰는 주문내역 페이지가 아니라 배민 리뷰관리 화면 전용 collector로 분리한다.
3. 광고현황은 배민 광고/광고관리 화면 전용 collector를 새로 추가한다.
4. 백필은 기존 `delivery_collection_status`의 stale queued/running 7건을 정리한 뒤 checkpoint 기반으로 실행한다.

## 2. 실측 현황

### 2.1 배민 등록 계정 4건

2026-08-25 12:21 KST 기준 `yeoljeong_platform_accounts`와 파일 원장 `app/data/yeoljeong_finance/platform_accounts.json`에서 확인한 배민 계정은 아래 4건이다.

| 사업자 | 지점 | 배민 계정 상태 | 포털 상태 | 백필 우선순위 | 출처 |
|---|---|---:|---:|---:|---|
| `biz-junghwa` | 중화점 | `credential_registered` | `succeeded` | P0 | [DB 조회] |
| `biz-sungshin` | 성신여대점 | `credential_registered` | `succeeded` | P0 | [DB 조회] |
| `biz-eonni-naengmyeon` | 성신여대역점 | `credential_registered` | `action_required` | P0 인증복구 선행 | [DB 조회] |
| `biz-mia` | 열정국밥_미아점 | `credential_registered` | `succeeded` | P0 | [DB 조회] |

### 2.2 원장 적재량

| 원장 | 배민 row | 전체 row | 판정 | 출처 |
|---|---:|---:|---|---|
| `yeoljeong_delivery_sales` | 199 | 777 | 일부 적재, 주문상세 스키마 혼재 | [DB 조회] |
| `yeoljeong_delivery_settlements` | 196 | 962 | 일부 적재, 주문별 정산 재확인 필요 | [DB 조회] |
| `yeoljeong_delivery_reviews` | 955 | 1,900 | row는 있으나 본문/평점 품질 낮음 | [DB 조회] |
| `yeoljeong_delivery_ads` | 0 | 85 | 배민 광고현황 미수집 | [DB 조회] |

### 2.3 지점별 파일 원장 품질

| 지점 | sales | settlements | reviews | ads | 주문상세 v1 | 판정 | 출처 |
|---|---:|---:|---:|---:|---:|---|---|
| 성신여대역점 | 3 | 3 | 184 | 0 | 0 | 인증복구 후 전체 재백필 필요 | [파일 원장] |
| 중화점 | 7 | 5 | 316 | 0 | 0 | 주문/정산 거의 미완 | [파일 원장] |
| 열정국밥_미아점 | 184 | 184 | 240 | 0 | 180 | 주문 P0 파서 일부 반영, 리뷰/광고 미완 | [파일 원장] |
| 성신여대점 | 4 | 4 | 215 | 0 | 0 | 주문/정산 거의 미완 | [파일 원장] |

리뷰 품질은 `review_text`, `rating`, `occurred_on` 기준으로 다시 봐야 한다. 파일 원장 기준 배민 리뷰 955건 중 본문이 채워진 row는 중화점 2건뿐이고 평점은 0건이다. 따라서 기존 리뷰 row는 "리뷰 존재 흔적"으로만 보고, 리뷰관리 화면 백필로 보정한다.

### 2.4 실행 충돌 상태

| 항목 | 결과 | 영향 | 출처 |
|---|---:|---|---|
| AADS 활성 Pipeline Runner | 0건 | 직접 문서 작업 가능 | [DB 조회] |
| 배민 `delivery_collection_status` queued/running | 7건 | 실제 백필 실행 전 stale 정리 필요 | [DB 조회] |

queued/running 7건은 2026-08-24~2026-08-25에 멈춘 상태로 보인다. 새 백필 runner는 시작 시 15분 초과 queued/running을 `failed/BACKFILL_STALE_RUN_CLEANED`로 정리하거나, 같은 scope 재실행 시 기존 run을 checkpoint로 전환해야 한다.

## 3. 수집 범위

### 3.1 주문건별 주문/매출

대상 화면: `orders/history`

| 구분 | 필드 |
|---|---|
| 식별 | `order_no`, `order_id`, `store_no`, `business_id`, `branch`, `service` |
| 시간 | `ordered_at`, `accepted_at`, `delivered_at`, `occurred_on`, `source_collected_at` |
| 주문 | `order_status`, `order_channel`, `payment_type`, `delivery_type`, `menu_summary` |
| 메뉴 | `items[].name`, `items[].quantity`, `items[].options[].name`, `items[].options[].amount` |
| 금액 | `order_amount`, `payment_total_amount`, `gross_amount`, `instant_discount_amount`, `partner_coupon_discount_amount` |
| 요청사항 | `extra.store_request`, `extra.delivery_request`, `extra.processing_history` |
| 결제수단 | `extra.primary_payment_method`, `extra.sub_payment_method` |
| 출처 | `source_url`, `schema_version=baemin_order_history.v1`, `raw_text_hash` |

### 3.2 주문건별 정산

대상 화면: `orders/history` 펼침 정산정보, 필요 시 정산관리 화면 보조.

| 구분 | 필드 |
|---|---|
| 연결키 | `order_no`, `order_id`, `occurred_on` |
| 상태 | `settlement.status`, `settlement_status`, `settlement.status_message` |
| 금액 | `settlement.order_brokerage_amount`, `settlement.order_amount`, `settlement.brokerage_fee_amount`, `settlement.delivery_amount`, `settlement.delivery_fee_amount`, `settlement.etc_amount`, `settlement.payment_fee_amount`, `settlement.vat_amount`, `settlement.expected_deposit_amount` |
| 입금 | `settlement.expected_deposit_on`, `settlement_amount` |
| 재조회 | 당일/D+0 미확정은 `pending` 저장 후 D+1 재조회 |

### 3.3 리뷰

대상 화면: 배민셀프서비스 리뷰관리.

| 구분 | 필드 |
|---|---|
| 식별 | `review_id`, `order_no`, `review_source_id`, `business_id`, `branch` |
| 리뷰 | `rating`, `review_text`, `reviewed_at`, `occurred_on`, `image_count`, `review_keywords` |
| 주문 연결 | `menu_summary`, `order_no`, `order_amount`, `match_confidence` |
| 답변 | `owner_reply_text`, `reply_status`, `reply_at`, `reply_required` |
| CS | `is_low_rating`, `needs_owner_action`, `sentiment_hint` |
| 출처 | `schema_version=baemin_review.v1`, `source_url`, `source_collected_at` |

주문번호가 화면에 없으면 `reviewed_at + menu_summary + branch`로 후보 연결하고 `match_confidence`를 남긴다. 자동 답글 작성/게시까지는 이번 백필 범위가 아니며, 조회와 원장 보강만 한다.

### 3.4 광고현황

대상 화면: 배민셀프서비스 광고/광고관리/우리가게클릭 등 광고 현황 화면.

| 구분 | 필드 |
|---|---|
| 식별 | `ad_id`, `campaign_id`, `product_name`, `store_no`, `business_id`, `branch` |
| 상태 | `ad_status`, `campaign_status`, `started_on`, `ended_on`, `budget_status` |
| 비용 | `daily_budget_amount`, `spent_amount`, `charged_amount`, `coupon_amount`, `vat_amount` |
| 성과 | `impressions`, `clicks`, `orders`, `sales_amount`, `ctr`, `conversion_rate`, `roas` |
| 과금/정산 | `billing_cycle`, `invoice_no`, `settlement_on`, `payment_method` |
| 출처 | `schema_version=baemin_ads.v1`, `source_url`, `source_collected_at` |

배민 광고 row는 현재 0건이다. 광고관리 화면이 지점별로 분리되어 있으면 store selector를 반드시 지점별로 확인한다.

## 4. 구현 설계

### 4.1 신규/보강 파일

| 파일 | 작업 | 이유 |
|---|---|---|
| `app/services/baemin_order_history_collector.py` | 주문 상세 DOM/API 수집, 기간 필터, 페이지네이션, 상세 모달 클릭, checkpoint 보강 | 주문/정산 핵심 |
| `app/services/baemin_review_collector.py` | 신규 | 리뷰관리 전용 수집 |
| `app/services/baemin_ads_collector.py` | 신규 | 광고현황 전용 수집 |
| `app/services/yeoljeong_finance_service.py` | `baemin_full_backfill` 모드와 status diagnostics 연결 | 4개 매장 순차 실행 |
| `tests/unit/test_baemin_order_history_collector.py` | fixture 확대 | 주문/정산 회귀 |
| `tests/unit/test_baemin_review_collector.py` | 신규 | 리뷰 파서 회귀 |
| `tests/unit/test_baemin_ads_collector.py` | 신규 | 광고 파서 회귀 |
| `docs/HANDOVER.md` | 작업 기록 | 운영 인수인계 |

### 4.2 실행 모드

기존 `/api/v1/yeoljeong-finance/sync-delivery` payload에 아래 모드를 추가한다.

```json
{
  "services": ["baemin"],
  "all_businesses": true,
  "collection_mode": "baemin_full_backfill",
  "date_from": "2026-07-01",
  "date_to": "2026-08-25",
  "include_orders": true,
  "include_reviews": true,
  "include_ads": true,
  "max_orders": 300,
  "window_days": 7,
  "prefer_pc_agent": true,
  "close_portal_browser_on_complete": false
}
```

처음 운영 실행은 전기간을 한 번에 밀지 않는다. 최근 7일 smoke run으로 4개 매장 모두 주문/리뷰/광고 selector를 확정한 뒤, 7일 window 단위로 과거 백필을 진행한다.

### 4.3 백필 상태/checkpoint

`yeoljeong_delivery_collection_status.payload`에 아래 키를 추가한다.

| 키 | 의미 |
|---|---|
| `backfill_mode` | `baemin_full_backfill` |
| `checkpoint.current_window_from` | 현재 수집 window 시작일 |
| `checkpoint.current_window_to` | 현재 수집 window 종료일 |
| `checkpoint.last_order_no` | 마지막 성공 주문번호 |
| `checkpoint.review_page_cursor` | 리뷰 목록 페이지/스크롤 cursor |
| `checkpoint.ads_page_cursor` | 광고 목록 cursor |
| `diagnostics.orders_seen/saved` | 주문 수집 품질 |
| `diagnostics.reviews_seen/saved/text_filled/rating_filled` | 리뷰 품질 |
| `diagnostics.ads_seen/saved` | 광고 품질 |
| `diagnostics.detail_failed` | 상세 클릭/파싱 실패 수 |
| `diagnostics.auth_required` | 로그인/OTP/CAPTCHA 등 중단 이유 |

## 5. 구현 단계

| 우선순위 | 단계 | 작업 | 완료 기준 |
|---|---|---|---|
| P0 | stale run 정리 | 15분 초과 queued/running 배민 status를 failed로 정규화 | active stale 0건 |
| P0 | 4개 매장 scope 고정 | 배민 계정 4건을 `all_businesses=true`에서 모두 순회 | summary에 4개 scope 표시 |
| P0 | 주문 상세 보강 | 기간 필터/다음페이지/상세 모달/미확정 정산 처리 | fixture + 최근 7일 smoke run |
| P0 | 리뷰 collector | 리뷰 목록/상세/답변 상태 파싱 | `review_text`, `rating`, `occurred_on` 채움 |
| P0 | 광고 collector | 광고 캠페인/비용/성과/상태 파싱 | 배민 ads row 0건 탈출 |
| P1 | D+1 정산 재조회 | `settlement.status=pending` 주문 재조회 | pending 감소 추적 |
| P1 | 전기간 백필 runner | 7일 window checkpoint 반복 | 중복 row 0건, 재개 가능 |
| P1 | UI 품질 매트릭스 | 매장별 주문/정산/리뷰/광고 완료율 표시 | CEO 화면에서 누락 확인 |
| P2 | 은행 입금 대사 연결 | 배민 expected deposit와 은행 입금 후보 매칭 | 정산/입금 차이 리포트 |

## 6. 안정성 정책

| 항목 | 정책 |
|---|---|
| 동시성 | 배민 계정 1개씩 순차 실행, 포털 세션 중복 금지 |
| 클릭 간격 | 상세/모달 클릭 0.8~1.5초 jitter |
| 페이지 이동 | 2~4초 jitter |
| 1회 최대 주문 | 기본 300건 |
| 1회 최대 시간 | 기본 12분 |
| window | 기본 7일 |
| 재시도 | 네트워크/DOM 일시 실패 2회, 인증 실패는 action_required |
| 원본 저장 | 원본 HTML 저장 금지, 안전 텍스트 hash와 파싱 payload만 저장 |
| 시크릿 | 계정 비밀번호/OTP/CAPTCHA 값 원장 저장 금지 |

## 7. 검증 계획

| 검증 | 명령/방법 | 성공 기준 |
|---|---|---|
| 파서 컴파일 | `python3 -m py_compile app/services/baemin_order_history_collector.py app/services/baemin_review_collector.py app/services/baemin_ads_collector.py app/services/yeoljeong_finance_service.py` | 오류 0 |
| 주문 fixture | `pytest tests/unit/test_baemin_order_history_collector.py -q` | 전체 통과 |
| 리뷰 fixture | `pytest tests/unit/test_baemin_review_collector.py -q` | 전체 통과 |
| 광고 fixture | `pytest tests/unit/test_baemin_ads_collector.py -q` | 전체 통과 |
| 4개 매장 smoke | 최근 7일, `all_businesses=true`, `services=["baemin"]` | summary 4개, fatal 0 |
| 품질 SQL | 원장 count + payload 필드 채움률 조회 | 주문/리뷰/광고 지점별 증가 |
| idempotency | 같은 window 2회 실행 | row count 중복 증가 0 |
| 인증 중단 | 성신여대역점 action_required | run 실패가 아니라 재개 가능한 status |

## 8. 완료 기준

| 영역 | 완료 기준 |
|---|---|
| 주문/정산 | 4개 매장 모두 지정 기간의 주문번호 기준 sales/settlements upsert, `order_no` 누락 0건 |
| 리뷰 | 4개 매장 모두 `review_text`, `rating`, `occurred_on`이 채워진 리뷰 row 생성 |
| 광고 | 4개 매장 모두 광고 캠페인/비용/상태 row 생성, 광고가 없으면 `no_ads_active` 상태 row 기록 |
| 재실행 | 같은 기간 재실행 시 중복 row 증가 0건 |
| 운영 | `delivery_collection_status`에 지점별 orders/reviews/ads diagnostics 저장 |
| 보고 | HANDOVER와 최종 백필 리포트에 수집량/누락/인증 필요 지점 기록 |

## 9. 즉시 구현 준비 패킷

다음 작업은 Pipeline Runner 또는 직접 코드 작업으로 바로 착수 가능하다. 범위가 다중 파일이므로 실제 코드 구현은 Runner 투입이 안전하다.

작업명: `AADS-FOOD-BAEMIN-4STORE-FULL-BACKFILL-P0`  
대상 파일:

- `app/services/baemin_order_history_collector.py`
- `app/services/baemin_review_collector.py`
- `app/services/baemin_ads_collector.py`
- `app/services/yeoljeong_finance_service.py`
- `tests/unit/test_baemin_order_history_collector.py`
- `tests/unit/test_baemin_review_collector.py`
- `tests/unit/test_baemin_ads_collector.py`
- `docs/HANDOVER.md`

Runner 지시 초안:

```text
FOOD/열정국밥 매장비서 배민 4개 등록 계정 전체 백필 P0를 구현한다.
기준 문서: docs/plans/20260825_BAEMIN_ALL_STORES_FULL_BACKFILL_IMPLEMENTATION_PLAN.md

필수 구현:
1. stale delivery_collection_status queued/running 정규화 로직을 배민 백필 시작 전에 추가한다.
2. baemin_order_history_collector.py에 기간 필터, 페이지네이션/스크롤, 주문 상세 펼침, 주문 추가 정보 모달, D+0 pending settlement 처리를 보강한다.
3. baemin_review_collector.py를 추가해 리뷰관리 화면의 review_id/order_no/rating/review_text/reviewed_at/menu_summary/owner_reply_text/reply_status/image_count를 schema_version=baemin_review.v1로 파싱한다.
4. baemin_ads_collector.py를 추가해 광고관리 화면의 campaign/product/status/budget/spent/clicks/orders/sales/roas를 schema_version=baemin_ads.v1로 파싱한다. 광고가 없으면 no_ads_active diagnostics를 남긴다.
5. yeoljeong_finance_service.py의 배민 PC Agent 수집 경로에서 collection_mode=baemin_full_backfill 또는 include_orders/include_reviews/include_ads payload를 받아 4개 매장 scope를 순차 실행하고 sales/settlements/reviews/ads 원장에 idempotent upsert한다.
6. 원본 HTML과 시크릿은 저장하지 말고 안전 텍스트 hash, source_url, source_collected_at, diagnostics만 저장한다.
7. tests/unit에 주문/리뷰/광고 fixture 테스트와 idempotency 테스트를 추가한다.

검증:
- python3 -m py_compile 대상 파일
- pytest tests/unit/test_baemin_order_history_collector.py tests/unit/test_baemin_review_collector.py tests/unit/test_baemin_ads_collector.py -q
- git diff --check

배포/재시작/푸시는 하지 말고 커밋 전 검증 결과와 남은 E2E 필요 사항을 보고한다.
```

## 10. 남은 리스크

| 리스크 | 영향 | 대응 |
|---|---|---|
| 성신여대역점 배민 포털 `action_required` | 4개 매장 전체 smoke run 차단 가능 | PC Agent 세션 로그인/인증 복구 후 재개 |
| 배민 화면 selector 변경 | DOM 파서 실패 | 네트워크 JSON 우선, DOM selector, text fallback 3단계 |
| 리뷰 주문번호 미노출 | 주문-리뷰 직접 연결 불가 | 날짜/메뉴/금액 후보 매칭과 confidence 저장 |
| 광고가 실제 미운영 | ads row 0건이 정상인지 누락인지 불명확 | `no_ads_active` status row와 화면 증거 diagnostics 저장 |
| 오래된 queued/running 7건 | 중복 실행/상태 오판 | 백필 시작 전 stale cleanup 필수 |

## 11. 2026-08-25 12:54 KST 재점검 및 구현 준비 상태

DB 시간 `2026-08-25 12:54:37 KST` 기준으로 구현 착수 전 상태를 재확인했다.

| 항목 | 재점검 결과 | 판정 | 출처 |
|---|---:|---|---|
| 배민 platform_accounts raw row | 15건 | 삭제/중복 이력 포함, 실행 scope는 삭제되지 않은 대표 4건으로 제한 | [DB 조회] |
| 삭제되지 않은 대표 배민 계정 | 4건 | 중화점/성신여대점/성신여대역점/열정국밥_미아점 | [DB 조회] |
| 배민 sales row | 199건 | 주문상세 v1은 미아점 중심 180건, 나머지 3개 지점은 거의 미완 | [DB 조회] |
| 배민 settlements row | 196건 | 주문번호 기준 재정산 확인 필요 | [DB 조회] |
| 배민 reviews row | 955건 | `rating=0` placeholder가 대부분이며 본문/일자 품질 미달 | [DB 조회] |
| 배민 ads row | 0건 | 광고 collector 신규 구현 필요 | [DB 조회] |
| 배민 queued/running status | 7건 | 15분 초과 stale 후보, 과거 `언니냉면` scope 1건 포함 | [DB 조회] |
| AADS Pipeline Runner 승인대기 | 2건 | 실제 코드 구현 작업은 제출되어 있으나 CEO 승인 전 | [DB 조회] |

승인대기 Runner:

| job_id | 상태 | 크기 | 생성시각(KST) | 요약 |
|---|---|---|---|---|
| `runner-f6427e3e` | `awaiting_approval` | L | 2026-08-25 12:26:47 | 배민 4개 매장 주문상세/리뷰/광고 전체 백필 collector 구현 재제출 |
| `runner-ef69aaed` | `awaiting_approval` | L | 2026-08-25 12:25:25 | 배민 4개 매장 주문상세/리뷰/광고 전체 백필 collector 구현 |

구현 준비 판정:

1. 문서/필드/검증 기준은 이 파일에 저장 완료.
2. 주문 파서 P0 골격은 `app/services/baemin_order_history_collector.py`와 `tests/unit/test_baemin_order_history_collector.py`에 이미 존재한다.
3. 리뷰/광고 collector 파일은 아직 없으므로 Runner 승인 후 신규 생성이 필요하다.
4. Runner가 2건 중복 승인대기이므로 실제 구현 착수 시 `runner-f6427e3e`만 승인하고 `runner-ef69aaed`는 중복 작업으로 거부하는 것이 안전하다.
5. 코드 구현 전 stale status 7건을 cleanup 대상으로 고정하고, 대표 배민 계정 4건만 순회하도록 scope filter를 먼저 넣어야 한다.
