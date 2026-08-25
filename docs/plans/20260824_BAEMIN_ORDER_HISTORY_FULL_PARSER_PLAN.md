# 배민 주문내역 전체 파싱 및 과거 누적수집 계획

작성: 2026-08-24 19:17 KST  
대상 URL: `https://self.baemin.com/orders/history`  
범위: FOOD/열정국밥 배민 주문건별 매출, 할인, 배달, 정산, 추가정보, 리뷰 원본 수준 누적수집

## 1. 결론

현재 AADS 매장비서 판매채널 원장은 존재하지만, 배민 `orders/history` 페이지의 주문건별 모든 정보를 수집하는 상태는 아니다.

현재 구현은 배민 홈 대시보드 요약과 현재 DOM의 일반 표를 1회 파싱하는 수준이다. 첨부 화면처럼 주문 행을 펼치고 `주문정보`, `정산정보`, `주문 추가 정보` 모달까지 순회하는 전용 collector, 과거 페이지네이션/날짜 백필, 주문 상세 원장 스키마가 추가되어야 매출/정산자료 자동수집 완료로 볼 수 있다.

## 2. 확인 근거

| 구분 | 확인 결과 | 근거 |
|------|-----------|------|
| 현재 배민 수집 코드 | 홈 요약, 현재 페이지 종류 판정, 현재 DOM 표 파싱 중심 | `app/services/yeoljeong_finance_service.py:5614`, `app/services/yeoljeong_finance_service.py:6710` |
| 공통 파서 | HTML table 또는 delimited text만 표준 row로 변환 | `app/services/yeoljeong_delivery_collectors.py:531` |
| 현재 원장 구조 | `payload JSONB` 중심으로 원본 확장 저장 가능 | `migrations/116_yeoljeong_finance_delivery_ledgers.sql:14` |
| DB 적재 상태 | sales 594건, settlements 779건, reviews 1,858건, status 1,994건 | DB 조회, 2026-08-24 19:19 KST |
| 배민 sales 품질 | 배민 sales 16건 중 주문번호 채움 2건 | DB 조회, 2026-08-24 19:19 KST |
| 배민 reviews 품질 | 배민 reviews 913건이나 review_text/rating/occurred_on 채움 0건 | DB 조회, 2026-08-24 19:19 KST |

## 3. 첨부 화면 기준 파싱 대상

### 3.1 주문 목록 행

첨부 이미지 1, 2에서 확인한 목록 행은 각 주문의 기본 식별자와 금액 요약이다.

| 필드 | 예시 | 저장 키 |
|------|------|---------|
| 펼침 상태 | 접기/펼치기 화살표 | `ui_expanded` |
| 주문 상태 | 배달완료 | `order_status` |
| 주문번호 | `T2F00001YXZA`, `T2FP00000XZV` | `order_no` |
| 주문일시 | `2026. 08. 24. (월) 오전 12:04:48` | `ordered_at` |
| 주문 채널 | 배민배달(배민클럽), 가게배달 | `order_channel` |
| 가게번호 또는 매장 식별번호 | `12583925`, `12574388` | `store_no` |
| 메뉴 요약 | `(단골주문1위) 열정충천해장국2+숙주2+당면2` | `menu_summary` |
| 결제 방식 | 바로결제, 만나서결제 | `payment_type` |
| 배달 방식 | 알뜰배달, 한집배달, 배달 | `delivery_type` |
| 주문금액 | `12,000원`, `28,000원` | `order_amount` |

### 3.2 펼침 주문정보

주문 행을 펼치면 좌측 `주문정보` 영역과 우측 금액 상세가 나온다.

| 필드 | 예시 | 저장 키 |
|------|------|---------|
| 메뉴명 | `(아틀아틀) 돼지국밥정식` | `items[].name` |
| 수량 | `1개` | `items[].quantity` |
| 메뉴/옵션 금액 | `기본(12,000원)`, `1+1(28,000원)` | `items[].options[]` |
| 옵션명 | `다음에 참여할게요`, `보통` | `items[].options[].name` |
| 옵션금액 | `0원` | `items[].options[].amount` |
| 총 결제금액 | `12,000원`, `28,000원` | `payment_total_amount` |
| 즉시할인 | `2,700원`, `1,900원` | `instant_discount_amount` |
| 파트너부담 쿠폰할인 | `14,000원` | `partner_coupon_discount_amount` |
| 추가정보 링크 | `주문 추가 정보 보기` | `has_order_extra_info` |

### 3.3 펼침 정산정보

정산정보는 주문 단위 정산 대사의 핵심이다. 입금예정금액이 아직 공개되지 않은 최신 주문은 `settlement_pending`으로 남긴다.

| 필드 | 예시 | 저장 키 |
|------|------|---------|
| 주문중개 합계 | `(A)주문중개 11,064원` | `settlement.order_brokerage_amount` |
| 주문금액 | `12,000원` | `settlement.order_amount` |
| 중개이용료 | `-936원` | `settlement.brokerage_fee_amount` |
| 배달 합계 | `(B)배달 -3,400원` | `settlement.delivery_amount` |
| 배달비 | `-3,400원` | `settlement.delivery_fee_amount` |
| 그외 합계 | `(C)그외 -288원` | `settlement.etc_amount` |
| 결제정산수수료 | `-288원` | `settlement.payment_fee_amount` |
| 부가세 | `(D)부가세 -463원` | `settlement.vat_amount` |
| 입금예정금액 | `6,913원` | `settlement.expected_deposit_amount` |
| 입금예정일 | `2026. 08. 26. (수)` | `settlement.expected_deposit_on` |
| 미확정 안내 | `입금예정금액은 거래일자 다음날부터 확인...` | `settlement.status_message` |

### 3.4 주문 추가 정보 모달

첨부 이미지 3의 모달은 목록 row만으로는 얻을 수 없는 운영/CS/정산 검증 필드다.

| 필드 | 예시 | 저장 키 |
|------|------|---------|
| 주결제방법 | `배민페이(카드결제)` | `extra.primary_payment_method` |
| 보조결제방법 | `할인쿠폰` | `extra.sub_payment_method` |
| 가게 요청사항 | `(수저포크 X)` | `extra.store_request` |
| 배달 요청사항 | `문 앞에 두고 초인종 눌러주세요.` | `extra.delivery_request` |
| 처리내역 | `배달(픽업)이 완료되었습니다.` | `extra.processing_history` |
| 주문시각 | `2026. 08. 24. (월) 오전 12:04:48` | `extra.ordered_at` |
| 접수시각 | `2026. 08. 24. (월) 오전 12:04:50` | `extra.accepted_at` |
| 배달시각 | `2026. 08. 24. (월) 오전 12:17:13` | `extra.delivered_at` |

## 4. 목표 데이터 모델

기존 `yeoljeong_delivery_sales.payload`에는 아래 구조를 그대로 저장한다. 정산 row는 동일 `order_no`로 `yeoljeong_delivery_settlements.payload`에도 얇은 projection을 저장해 은행 입금 대사와 빠르게 연결한다.

```json
{
  "service": "baemin",
  "record_type": "sales",
  "business_id": "biz-mia",
  "branch": "열정국밥_미아점",
  "order_no": "T2FP00000XZV",
  "ordered_at": "2026-08-24T00:04:48+09:00",
  "accepted_at": "2026-08-24T00:04:50+09:00",
  "delivered_at": "2026-08-24T00:17:13+09:00",
  "order_status": "배달완료",
  "order_channel": "배민배달(배민클럽)",
  "store_no": "12583925",
  "menu_summary": "(단골주문1위) 열정충천해장국2+숙주2+당면2",
  "payment_type": "바로결제",
  "delivery_type": "한집배달",
  "order_amount": 28000,
  "payment_total_amount": 28000,
  "instant_discount_amount": 1900,
  "partner_coupon_discount_amount": 14000,
  "items": [
    {
      "name": "(단골주문1위) 열정충천해장국2+숙주2+당면2",
      "quantity": 1,
      "options": [{"name": "1+1", "amount": 28000}]
    }
  ],
  "settlement": {
    "status": "pending",
    "expected_deposit_amount": null,
    "status_message": "입금예정금액은 거래일자 다음날부터 확인할 수 있어요."
  },
  "extra": {
    "primary_payment_method": "배민페이(카드결제)",
    "sub_payment_method": "할인쿠폰",
    "store_request": "(수저포크 X)",
    "delivery_request": "문 앞에 두고 초인종 눌러주세요.",
    "processing_history": "배달(픽업)이 완료되었습니다."
  },
  "source_url": "https://self.baemin.com/orders/history",
  "source_collected_at": "2026-08-24T19:17:56+09:00",
  "schema_version": "baemin_order_history.v1"
}
```

### 4.1 배민 정보수집용 DB 필드 리스트

현재 DB 물리 컬럼은 공통 원장 구조를 유지한다. 신규 마이그레이션 없이 `payload JSONB` 내부에 아래 필드 계약을 저장한다.

| DB 테이블 | 물리 컬럼 | payload 수집 필드 | 용도 |
|-----------|-----------|-------------------|------|
| `yeoljeong_delivery_sales` | `row_id` | `id` | `business_id|branch|baemin|sales|order_no` 해시, 중복 upsert 키 |
| `yeoljeong_delivery_sales` | `business_id` | `business_id` | 사업자 scope |
| `yeoljeong_delivery_sales` | `branch` | `branch` | 지점 scope |
| `yeoljeong_delivery_sales` | `payload` | `service`, `platform`, `record_type` | 채널/원장 타입 |
| `yeoljeong_delivery_sales` | `payload` | `source_id`, `order_id`, `order_no` | 주문 식별자 |
| `yeoljeong_delivery_sales` | `payload` | `occurred_on`, `ordered_at`, `accepted_at`, `delivered_at` | 주문/접수/배달 시각 |
| `yeoljeong_delivery_sales` | `payload` | `order_status`, `order_channel`, `store_no` | 주문 상태와 배민 매장 식별 |
| `yeoljeong_delivery_sales` | `payload` | `menu_summary`, `items[].name`, `items[].quantity`, `items[].options[]` | 주문 메뉴/옵션 상세 |
| `yeoljeong_delivery_sales` | `payload` | `payment_type`, `delivery_type` | 결제/배달 방식 |
| `yeoljeong_delivery_sales` | `payload` | `gross_amount`, `order_amount`, `payment_total_amount` | 매출/결제 금액 |
| `yeoljeong_delivery_sales` | `payload` | `instant_discount_amount`, `partner_coupon_discount_amount` | 할인/쿠폰 금액 |
| `yeoljeong_delivery_sales` | `payload` | `settlement.*` | 주문 단위 정산 상세 원본 projection |
| `yeoljeong_delivery_sales` | `payload` | `extra.primary_payment_method`, `extra.sub_payment_method` | 주문 추가 정보 결제수단 |
| `yeoljeong_delivery_sales` | `payload` | `extra.store_request`, `extra.delivery_request` | 가게/배달 요청사항 |
| `yeoljeong_delivery_sales` | `payload` | `extra.processing_history` | 주문 처리내역 |
| `yeoljeong_delivery_sales` | `payload` | `source_url`, `source_collected_at`, `schema_version` | 출처/수집시각/스키마 버전 |
| `yeoljeong_delivery_settlements` | `row_id` | `id` | `business_id|branch|baemin|settlements|order_no` 해시 |
| `yeoljeong_delivery_settlements` | `business_id` | `business_id` | 사업자 scope |
| `yeoljeong_delivery_settlements` | `branch` | `branch` | 지점 scope |
| `yeoljeong_delivery_settlements` | `payload` | `order_no`, `order_id`, `occurred_on` | 주문 연결 키 |
| `yeoljeong_delivery_settlements` | `payload` | `sales_amount`, `fee_amount`, `vat_amount`, `settlement_amount` | 정산 대사 핵심 금액 |
| `yeoljeong_delivery_settlements` | `payload` | `settlement_status`, `settlement.status` | 정산 확정/미확정 상태 |
| `yeoljeong_delivery_settlements` | `payload` | `settlement.order_brokerage_amount`, `settlement.brokerage_fee_amount` | 주문중개 금액/수수료 |
| `yeoljeong_delivery_settlements` | `payload` | `settlement.delivery_amount`, `settlement.delivery_fee_amount` | 배달 금액/배달비 |
| `yeoljeong_delivery_settlements` | `payload` | `settlement.etc_amount`, `settlement.payment_fee_amount`, `settlement.vat_amount` | 기타/결제수수료/부가세 |
| `yeoljeong_delivery_settlements` | `payload` | `settlement.expected_deposit_amount`, `settlement.expected_deposit_on`, `settlement.status_message` | 입금예정 금액/일자/미확정 안내 |
| `yeoljeong_delivery_collection_status` | `payload` | `diagnostics.order_history_orders_seen`, `order_history_orders_saved`, `order_history_detail_failed`, `order_history_settlement_pending` | run별 품질 지표 |

리뷰는 `orders/history`가 아니라 배민 리뷰관리 화면에서 별도 수집한다. 리뷰관리 collector의 목표 payload 필드는 `review_id`, `order_no`, `rating`, `review_text`, `reviewed_at`, `menu_summary`, `owner_reply_text`, `reply_status`, `image_count`, `match_confidence`, `schema_version=baemin_review.v1`이다.

## 5. collector 설계

### 5.1 신규 모듈

새 파일 `app/services/baemin_order_history_collector.py`를 추가한다.

| 함수 | 책임 |
|------|------|
| `collect_baemin_order_history(page, account, date_from, date_to, options)` | 로그인된 PC Agent page에서 기간 단위 주문 수집 |
| `navigate_order_history(page)` | `https://self.baemin.com/orders/history` 이동 및 로그인 상태 확인 |
| `set_order_history_period(page, date_from, date_to)` | 날짜 필터 적용 |
| `extract_order_rows(page)` | 현재 페이지 주문 행 목록, 펼침 버튼, row handle 추출 |
| `expand_order_row(row)` | 주문정보/정산정보 패널 열기 |
| `parse_order_summary(row_text)` | 목록 행 필드 파싱 |
| `parse_order_detail(expanded_text)` | 메뉴, 할인, 정산정보 파싱 |
| `open_and_parse_extra_info(row)` | `주문 추가 정보 보기` 클릭 후 모달 파싱 |
| `go_next_page_or_scroll(page)` | 페이지네이션/무한스크롤/날짜 이전 이동 |
| `upsert_order_history_records(records)` | 기존 원장에 idempotent 저장 |

### 5.2 DOM/API 접근 우선순위

1. 브라우저 네트워크 응답에서 주문내역 JSON API가 보이면 API payload를 우선 사용한다.
2. API를 못 잡으면 DOM locator 기반으로 목록 행을 추출한다.
3. DOM selector가 깨지면 row text fallback parser를 사용한다.
4. 상세 모달은 항상 버튼 클릭 후 현재 표시 text와 HTML을 같이 읽고, 원본 HTML은 저장하지 않는다.

### 5.3 페이지 순회

| 단계 | 정책 |
|------|------|
| 최신순 시작 | 기본 `date_to=오늘`, `date_from=오늘 또는 최근 N일` |
| 과거 백필 | 일 단위 또는 7일 단위 window를 뒤로 이동 |
| 중복 차단 | `business_id|branch|baemin|order_no` 해시를 primary id로 사용 |
| 중단/재개 | 마지막 완료 window, 마지막 주문번호, 페이지 index를 `delivery_collection_status.payload.checkpoint`에 저장 |
| 정산 미확정 | 당일 주문은 `settlement.status=pending`으로 저장하고 D+1 이후 정산 재조회 |
| 완료 판정 | 날짜 window의 목록 끝, 다음 페이지 없음, 신규 order_no 0건이 동시에 충족되어야 완료 |

## 6. 리소스 보호 정책

| 항목 | 제한값 |
|------|--------|
| 동시성 | 배민 계정당 1세션, 지점별 순차 |
| 주문 상세 클릭 간격 | 800-1,500ms jitter |
| 페이지 이동 간격 | 2-4초 jitter |
| 1회 run 최대 주문 | 기본 300건 |
| 1회 run 최대 시간 | 기본 12분 |
| 백필 window | 최근 7일 우선, 이후 7일 단위 과거 이동 |
| 자동 재시도 | 네트워크/DOM 일시 실패 2회, 인증 실패는 즉시 action_required |
| 브라우저 세션 | 배민 work key 고정, 완료 시 keep-open 또는 close 정책을 payload로 제어 |

## 7. 리뷰 수집 연결

주문내역만으로 리뷰 전체는 완성되지 않는다. 리뷰는 배민 `리뷰관리` 화면에서 별도 수집하되 주문번호, 주문일, 메뉴명, 리뷰일, 평점, 리뷰내용, 사장님 댓글, 사진 여부, 답글 상태를 같은 canonical key로 연결한다.

| 구분 | 구현 방향 |
|------|-----------|
| 리뷰 목록 | `self.baemin.com` 리뷰관리 메뉴 전용 collector 추가 |
| 리뷰 상세 | 카드/목록 row별 상세 펼침 또는 모달 파싱 |
| 주문 연결 | 주문번호가 있으면 직접 연결, 없으면 주문일+메뉴+금액 후보로 `match_confidence` 저장 |
| 현재 문제 | 배민 reviews 913건은 있으나 텍스트/평점/일자가 0건이라 기존 row를 신뢰 데이터로 볼 수 없음 |

## 8. 구현 단계

| 우선순위 | 작업 | 산출물 | 완료 기준 |
|----------|------|--------|-----------|
| P0 | 배민 주문내역 전용 collector 추가 | `app/services/baemin_order_history_collector.py` | 구현 완료, parser fixture 3건 통과 |
| P0 | finance service 연결 | `_collect_baemin_from_browser_bridge_session_async()`가 orders/history collector 호출 | 구현 완료, 운영 PC Agent E2E 필요 |
| P0 | 원장 품질 상태 추가 | status payload diagnostics에 `order_history_orders_seen/order_history_orders_saved/order_history_detail_failed/order_history_settlement_pending` | 구현 완료, 운영 run 검증 필요 |
| P0 | 최근 7일 자동수집 | CLI/API payload에 `mode=baemin_order_history` | 최근순 주문 상세 100% 수집 |
| P1 | 과거 백필 | checkpoint 기반 7일 window 반복 | 과거 종료일까지 누적, 중복 0건 |
| P1 | D+1 정산 재조회 | pending settlement 재수집 job | 당일 주문 정산 미확정 자동 보강 |
| P1 | 리뷰관리 상세 collector | 리뷰 원장 품질 보정 | `review_text/rating/occurred_on` 채움률 95% 이상 |
| P2 | UI 품질 리포트 | 매장비서 수집 현황 화면 | 지점별 누락 주문/리뷰/정산 확인 가능 |

## 9. 테스트 계획

| 테스트 | 내용 |
|--------|------|
| fixture parser | 첨부 화면 text/HTML을 fixture로 만들어 목록, 펼침 상세, 추가정보 모달 파싱 |
| idempotency | 같은 주문 2회 수집 시 sales/settlements row count 불변 |
| pagination | 다음 페이지, 무한스크롤, 빈 페이지 조건별 종료 |
| pending settlement | 당일 주문의 입금예정금액 미표시를 실패가 아니라 pending으로 저장 |
| parser fallback | selector 실패 시 row text fallback으로 핵심 필드 유지 |
| rate limit | 상세 클릭 간격과 max orders/time 제한 적용 |
| PC Agent resilience | 연결 끊김 시 checkpoint 저장 후 재연결 run에서 재개 |

## 10. 운영 실행 계획

1. CEO PC Agent 배민 세션을 `self.baemin.com/orders/history`에 고정한다.
2. 최근 7일을 먼저 수집해 selector와 금액 파싱 정확도를 검증한다.
3. 주문번호 기준으로 기존 16건 배민 sales를 신규 상세 row와 병합한다.
4. D+1 정산 미확정 주문은 다음날 자동 재조회 큐로 넘긴다.
5. 과거 백필은 7일 window, 300건, 12분 제한으로 순차 실행한다.
6. 리뷰관리는 주문내역과 분리해 전용 collector로 수집하되 같은 원장 품질 리포트에 표시한다.

## 11. 현재 막힌 부분

| 막힘 | 영향 | 조치 |
|------|------|------|
| 주문내역 전용 collector 없음 | 주문건별 모든 정보 수집 불가 | P0 신규 collector 구현 |
| 펼침 상세/모달 순회 없음 | 할인, 요청사항, 접수/배달시각 누락 | row별 expand/modal parser 구현 |
| 과거 백필 checkpoint 없음 | 최근 1회성 수집에서 중단 | status payload checkpoint 설계 |
| 리뷰 원장 품질 불량 | 리뷰 분석/CS 대응 불가 | 리뷰관리 전용 collector 구현 |
| 정산 미확정 상태 모델 없음 | 당일 주문 정산을 실패로 오판 가능 | `settlement.status=pending` 도입 |

## 12. 수용 기준

배민 자동수집 완료 판정은 아래 조건을 모두 만족해야 한다.

1. 최근 7일 주문 목록의 모든 주문번호가 `yeoljeong_delivery_sales.payload.order_no`에 저장된다.
2. 각 주문마다 목록, 주문정보, 정산정보, 주문 추가 정보 모달 필드가 `schema_version=baemin_order_history.v1`로 저장된다.
3. 당일 주문 정산 미확정은 `pending`으로 저장되고 D+1 재조회 후 금액이 채워진다.
4. 같은 기간을 2회 수집해도 중복 row가 생기지 않는다.
5. 수집 중 PC Agent 연결이 끊기면 마지막 window/order checkpoint부터 재개된다.
6. 리뷰관리 상세 수집 후 배민 리뷰 row의 `review_text`, `rating`, `occurred_on` 채움률이 95% 이상이다.
7. 수집 결과는 매장비서 화면과 DB count가 일치한다.
