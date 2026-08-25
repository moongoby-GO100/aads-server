# 배민 4개 매장 주문/리뷰/광고 전체 백필 구현 계획

작성: 2026-08-25 12:22 KST  
대상: FOOD/열정국밥 배민셀프서비스 자동수집  
범위: 4개 매장 전체, `https://self.baemin.com/orders/history` 주문건별 상세, 리뷰관리 상세, 광고현황/광고비, 정산 대사 보강, 과거자료 누적 백필

## 1. 결론

현재 배민 자동수집은 최근 주문 일부를 `baemin_order_history.v1`로 저장하는 단계까지 왔지만, 4개 매장 전체 백필 완료 상태는 아니다.

완료 기준은 4개 매장 각각에 대해 주문내역 최신순 백필, 주문 상세/정산 상세/주문 추가 정보, 리뷰 상세, 광고현황을 같은 run/checkpoint로 순차 수집하고, 누락/중복/미확정 정산을 원장 지표로 검증하는 것이다.

## 2. 현재 실측 상태

2026-08-25 12:22 KST 기준 운영 DB 조회 결과다.

| 원장 | 배민 적재 상태 | 판정 |
|---|---:|---|
| `yeoljeong_delivery_sales` | 미아점 185건, 중화점 7건, 성신여대점 4건, 성신여대역점 3건 | 주문 row는 있으나 주문상세 백필은 미아점 중심 |
| `yeoljeong_delivery_sales.payload.schema_version=baemin_order_history.v1` | 180건 | 주문상세 신규 스키마는 미아점만 채움 |
| `yeoljeong_delivery_sales` 주문번호 채움 | 미아점 180건 | 나머지 3개 매장 주문건별 상세 미완 |
| `yeoljeong_delivery_settlements` | 미아점 184건, 중화점 5건, 성신여대점 4건, 성신여대역점 3건 | 주문별 정산 projection은 부분적 |
| `yeoljeong_delivery_reviews` | 미아점 240건, 중화점 316건, 성신여대점 215건, 성신여대역점 184건 | 리뷰 row는 있으나 상세 필드 품질 부족 |
| `yeoljeong_delivery_reviews.review_text` 채움 | 중화점 2건 | 리뷰 상세 collector 필요 |
| `yeoljeong_delivery_ads` | 0건 | 광고현황 collector 미구현 |

## 3. 수집 대상 매장

| business_id | branch | 필수 수집 범위 |
|---|---|---|
| `biz-mia` | `열정국밥_미아점` | 주문상세 보강, 리뷰 상세, 광고현황, 과거 백필 |
| `biz-junghwa` | `중화점` | 주문상세 전체 백필, 리뷰 상세, 광고현황 |
| `biz-sungshin` | `성신여대점` | 주문상세 전체 백필, 리뷰 상세, 광고현황 |
| `biz-eonni-naengmyeon` | `성신여대역점` | 주문상세 전체 백필, 리뷰 상세, 광고현황 |

삭제된 지점명 `언니냉면`은 백필 대상 지점명으로 쓰지 않는다. 해당 사업자의 운영 branch는 `성신여대역점`이다.

## 4. DB 저장 계약

현재 DB는 공통 물리 컬럼 `row_id`, `business_id`, `branch`, `payload`, `created_at`, `updated_at`, `deleted_at` 구조다. 신규 컬럼을 먼저 늘리지 않고 `payload JSONB` 계약을 확정해 저장한다.

### 4.1 주문/매출 원장

테이블: `yeoljeong_delivery_sales`  
스키마 버전: `baemin_order_history.v2`

| payload 필드 | 타입 | 설명 |
|---|---|---|
| `service`, `platform` | string | `baemin` 고정 |
| `record_type` | string | `sales` |
| `business_id`, `branch` | string | 사업자/지점 |
| `order_no`, `order_id`, `source_id` | string | 배민 주문번호. 중복 방지 핵심 키 |
| `ordered_at`, `accepted_at`, `delivered_at`, `occurred_on` | string | 주문/접수/배달 시각, 주문일 |
| `order_status` | string | 배달완료/주문취소 등 |
| `order_channel` | string | 배민배달, 배민클럽, 가게배달 등 |
| `store_no` | string | 배민 가게번호 |
| `menu_summary` | string | 목록 대표 메뉴 |
| `items[]` | array | 메뉴명, 수량, 옵션명, 옵션금액, 메뉴별 금액 |
| `payment_type` | string | 바로결제/만나서결제 |
| `delivery_type` | string | 알뜰배달/한집배달/가게배달 |
| `order_amount` | number | 주문금액 |
| `payment_total_amount` | number | 총 결제금액 |
| `instant_discount_amount` | number | 즉시할인 |
| `partner_coupon_discount_amount` | number | 파트너부담 쿠폰할인 |
| `customer_paid_amount` | number | 고객 결제 추정액. 화면 제공 시 저장 |
| `cancel_amount` | number | 취소/환불 금액. 화면 제공 시 저장 |
| `settlement` | object | 주문별 정산 상세 전체 projection |
| `extra` | object | 주문 추가 정보 모달 전체 |
| `source_url`, `source_collected_at`, `schema_version` | string | 출처/수집시각/스키마 |

`row_id`는 `sha256(business_id|branch|baemin|sales|order_no)`로 생성한다. 같은 주문 재수집은 update/upsert이고 row가 늘어나면 안 된다.

### 4.2 정산 원장

테이블: `yeoljeong_delivery_settlements`  
스키마 버전: `baemin_order_history.v2`

| payload 필드 | 타입 | 설명 |
|---|---|---|
| `order_no`, `order_id`, `occurred_on` | string | 주문 연결 키 |
| `sales_amount` | number | 주문금액 |
| `fee_amount` | number | 수수료 합계 |
| `vat_amount` | number | 부가세 |
| `settlement_amount` | number | 입금예정금액 |
| `settlement_status` | string | `ready`, `pending`, `cancelled`, `unknown` |
| `settlement.order_brokerage_amount` | number | 주문중개 합계 |
| `settlement.brokerage_fee_amount` | number | 중개이용료 |
| `settlement.delivery_amount` | number | 배달 합계 |
| `settlement.delivery_fee_amount` | number | 배달비 |
| `settlement.etc_amount` | number | 그외 합계 |
| `settlement.payment_fee_amount` | number | 결제정산수수료 |
| `settlement.vat_amount` | number | 부가세 |
| `settlement.expected_deposit_amount` | number | 입금예정금액 |
| `settlement.expected_deposit_on` | string | 입금예정일 |
| `settlement.status_message` | string | 미확정 안내 문구 |

### 4.3 리뷰 원장

테이블: `yeoljeong_delivery_reviews`  
스키마 버전: `baemin_review.v1`

| payload 필드 | 타입 | 설명 |
|---|---|---|
| `review_id`, `source_id` | string | 리뷰 고유키. 없으면 주문번호+리뷰일+본문 해시 |
| `order_no` | string | 주문 연결 키. 화면에서 없으면 매칭 후보로 저장 |
| `reviewed_at`, `occurred_on` | string | 리뷰 작성 시각/일자 |
| `rating` | number | 별점 |
| `review_text` | string | 고객 리뷰 본문 |
| `menu_summary` | string | 리뷰 연결 메뉴 |
| `customer_alias` | string | 고객 표시명. 마스킹 유지 |
| `image_count`, `has_image` | number/bool | 리뷰 이미지 여부 |
| `owner_reply_text` | string | 사장님 댓글 |
| `owner_replied_at` | string | 댓글 작성 시각 |
| `reply_status` | string | 답변완료/미답변 |
| `matched_order_no` | string | 주문 원장과 매칭된 주문번호 |
| `match_confidence` | number | 주문번호 직접 연결 1.0, 후보 매칭은 0~1 |
| `source_url`, `source_collected_at`, `schema_version` | string | 출처/수집시각/스키마 |

### 4.4 광고 원장

테이블: `yeoljeong_delivery_ads`  
스키마 버전: `baemin_ads.v1`

| payload 필드 | 타입 | 설명 |
|---|---|---|
| `campaign_id`, `campaign_name` | string | 광고 캠페인 식별 |
| `ad_product` | string | 우리가게클릭, 오픈리스트, 배민1 등 화면 제공 상품명 |
| `status` | string | 운영중/중지/종료 |
| `period_start`, `period_end`, `occurred_on` | string | 광고 기간/집계일 |
| `budget_amount` | number | 예산 |
| `spend_amount` | number | 광고비 |
| `impressions`, `clicks`, `orders` | number | 노출/클릭/주문 수 |
| `ctr`, `conversion_rate` | number | 클릭률/전환율 |
| `order_amount`, `roas` | number | 광고 매출/ROAS |
| `raw_labels` | object | 화면 라벨 원본값. selector 변경 대비 |
| `source_url`, `source_collected_at`, `schema_version` | string | 출처/수집시각/스키마 |

## 5. 구현 아키텍처

### 5.1 신규/보강 모듈

| 파일 | 조치 |
|---|---|
| `app/services/baemin_order_history_collector.py` | v1 파서를 v2로 보강. 주문 행 클릭, 상세 펼침, 추가 정보 모달, 페이지네이션, 날짜 window, checkpoint 지원 |
| `app/services/baemin_review_collector.py` | 신규. 리뷰관리 화면 전용 collector |
| `app/services/baemin_ads_collector.py` | 신규. 광고관리/우리가게클릭 화면 전용 collector |
| `app/services/yeoljeong_finance_service.py` | `sync_delivery`에서 `baemin_backfill` mode 처리, 원장 upsert, status diagnostics 확장 |
| `scripts/trigger_delivery_sync.py` | `--service baemin --mode full_backfill --from --to --max-orders --branch all` 지원 |
| `tests/unit/test_baemin_order_history_collector.py` | v2 fixture 추가 |
| `tests/unit/test_baemin_review_collector.py` | 리뷰 fixture 추가 |
| `tests/unit/test_baemin_ads_collector.py` | 광고 fixture 추가 |

### 5.2 수집 순서

1. PC Agent online 확인 및 배민 work_key 고정 세션 획득
2. 4개 매장을 순차 처리
3. 매장별 최신 7일 window부터 시작
4. `orders/history` 이동, 날짜 필터 적용
5. 주문 목록 최신순 파싱
6. 주문 row별 펼침 상세 파싱
7. `주문 추가 정보 보기` 모달 열기/파싱/닫기
8. 다음 페이지 또는 스크롤 끝까지 이동
9. sales/settlements upsert
10. 리뷰관리 화면 이동 후 리뷰 상세 upsert
11. 광고관리 화면 이동 후 광고현황 upsert
12. window checkpoint 저장 후 과거 7일 window로 이동

## 6. 리소스 보호 정책

| 항목 | 기본값 |
|---|---:|
| 동시성 | 전체 1세션, 매장 순차 |
| 주문 상세 클릭 간격 | 1.0~1.8초 jitter |
| 모달 열기/닫기 간격 | 0.8~1.5초 jitter |
| 페이지 이동 간격 | 2~4초 jitter |
| 1회 run 최대 주문 | 300건 |
| 1회 run 최대 리뷰 | 300건 |
| 1회 run 최대 광고 row | 100건 |
| 1회 run 최대 시간 | 12분 |
| 백필 window | 최근 7일 우선, 이후 7일 단위 과거 이동 |
| 에러 재시도 | 네트워크/DOM 일시 실패 2회 |
| 인증 에러 | 즉시 `action_required` 저장 후 중단 |
| 완료 재개 | `delivery_collection_status.payload.checkpoint` 기준 |

## 7. 상태/체크포인트

테이블: `yeoljeong_delivery_collection_status`

| payload 필드 | 설명 |
|---|---|
| `service` | `baemin` |
| `mode` | `full_backfill` |
| `run_id` | 실행 ID |
| `branch` | 현재 지점 |
| `window_from`, `window_to` | 수집 날짜 범위 |
| `checkpoint.last_order_no` | 마지막 저장 주문번호 |
| `checkpoint.page_index` | 마지막 페이지 |
| `checkpoint.window_complete` | 해당 window 완료 여부 |
| `metrics.orders_seen`, `orders_saved`, `orders_updated` | 주문 수집 지표 |
| `metrics.settlements_saved`, `settlement_pending` | 정산 지표 |
| `metrics.reviews_seen`, `reviews_saved`, `reviews_matched` | 리뷰 지표 |
| `metrics.ads_seen`, `ads_saved` | 광고 지표 |
| `metrics.detail_failed`, `review_detail_failed`, `ads_failed` | 실패 지표 |
| `error_code`, `message` | 실패/보류 사유 |

## 8. 완료 기준

| 범위 | 완료 기준 |
|---|---|
| 주문 | 4개 매장 모두 지정 과거 종료일까지 `order_no` 중복 없이 저장 |
| 주문 상세 | 주문 row의 목록/펼침/추가정보 모달 필드 채움률 95% 이상 |
| 정산 | D+1 이상 주문의 `settlement_status=ready` 채움률 95% 이상 |
| 리뷰 | 리뷰 본문/평점/작성일 채움률 95% 이상 |
| 광고 | 광고 관리 화면에서 제공하는 캠페인/상태/비용/성과 row 저장 |
| 재개 | 중단 후 같은 payload 재실행 시 이미 저장된 주문은 update만 수행 |
| 리소스 | 1회 run 12분, 상세 클릭 jitter, 동시성 1 준수 |

## 9. 구현 준비 체크리스트

- [x] 기존 주문상세 v1 collector 존재 확인
- [x] 운영 DB 공통 JSONB 원장 구조 확인
- [x] 현재 적재 상태와 미완 범위 확인
- [x] 수집 필드 계약 v2 작성
- [x] 리뷰/광고 별도 collector 필요성 분리
- [ ] v2 collector 구현
- [ ] 리뷰 collector 구현
- [ ] 광고 collector 구현
- [ ] `sync_delivery` full_backfill mode 연결
- [ ] parser fixture/unit test 작성
- [ ] PC Agent 배민 로그인 세션 E2E
- [ ] 4개 매장 최근 7일 dry run
- [ ] 과거 window 백필 실행

## 10. Runner 구현 지시서

```
TASK_ID: AADS-FOOD-BAEMIN-FULL-BACKFILL-P0-20260825
TITLE: FOOD 배민 4개 매장 주문상세/리뷰/광고 전체 백필 collector 구현
PRIORITY: P0
SIZE: L
MODEL: default

Active project: AADS
Workdir: /root/aads/aads-server

요구사항:
1. docs/plans/20260825_BAEMIN_FULL_BACKFILL_IMPLEMENTATION_PLAN.md를 수락기준으로 삼아 구현한다.
2. 기존 미커밋 dirty 파일을 되돌리지 말고, 필요한 파일만 선별 수정한다.
3. baemin_order_history_collector.py를 v2로 보강해 orders/history 날짜 window, 페이지네이션/스크롤, row expand, 주문 추가 정보 모달 파싱, checkpoint 진단을 구현한다.
4. baemin_review_collector.py와 baemin_ads_collector.py를 추가해 리뷰관리/광고관리 화면의 상세 row를 payload 계약대로 파싱한다.
5. yeoljeong_finance_service.py의 배민 PC Agent 수집 경로에 mode=full_backfill을 연결하고, sales/settlements/reviews/ads/status 원장에 idempotent upsert한다.
6. scripts/trigger_delivery_sync.py에 baemin full_backfill 실행 옵션을 추가한다.
7. 테스트 fixture를 추가하고 아래 검증을 통과시킨다.

검증:
- python3 -m py_compile app/services/baemin_order_history_collector.py app/services/baemin_review_collector.py app/services/baemin_ads_collector.py app/services/yeoljeong_finance_service.py scripts/trigger_delivery_sync.py
- pytest tests/unit/test_baemin_order_history_collector.py tests/unit/test_baemin_review_collector.py tests/unit/test_baemin_ads_collector.py -q
- git diff --check -- app/services/baemin_order_history_collector.py app/services/baemin_review_collector.py app/services/baemin_ads_collector.py app/services/yeoljeong_finance_service.py scripts/trigger_delivery_sync.py tests/unit/test_baemin_order_history_collector.py tests/unit/test_baemin_review_collector.py tests/unit/test_baemin_ads_collector.py docs/plans/20260825_BAEMIN_FULL_BACKFILL_IMPLEMENTATION_PLAN.md

완료 보고:
- 변경 파일
- 테스트 결과
- DB 필드 계약 반영 여부
- PC Agent E2E 가능/불가 사유
- 남은 수동 인증/세션 리스크
```
