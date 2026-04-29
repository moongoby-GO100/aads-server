# NewTalk V2 전체 페이지 현황 보고서 (보강판)

> 작성일: 2026-04-29 | 서버: 114.207.244.86 | 도메인: https://v2.newtalk.kr

---

## 인프라 현황

| 컨테이너 | 상태 | 포트 | 역할 |
|-----------|------|------|------|
| newtalk-v2-frontend | Up (정상) | 3000 | Next.js 15.5.12 SSR |
| newtalk-v2-app | Up 6주 | 9000 (FPM) | Laravel 12 API |
| newtalk-v2-nginx | Up 6주 | 8080→80 | 리버스 프록시 |
| newtalk-v2-db | Up 6주 (healthy) | 3307→3306 | MySQL 8.0 |
| newtalk-v2-redis | Up 6주 | 6380→6379 | 캐시/세션 |
| newtalk-v2-reverb | Up 45시간 | 6001→8080 | WebSocket (Reverb) |

**Health Check**: `GET /api/health` → `{"status":"ok","services":{"database":"ok","redis":"ok","disk_free_gb":184.49}}`

**SSL**: Cloudflare 종단 → 호스트 nginx(:80) → Docker nginx(:8080) → Laravel/Next.js

---

## 1. 정적 디자인 페이지 (4개)

| 페이지 | URL | 설명 |
|--------|-----|------|
| Discover (상품 탐색 피드) | `/discover.html` | 100개+ 상품 카드 프로토타입 |
| 도매 브랜드 포털 | `/wholesale.html` | MAISON de FLEUR 풀 데모 |
| 기획서 v1.3 (최신) | `/plan-v1.3.html` | 수수료 구조 확정본 |
| 기획서 v1.2 | `/plan-v1.2.html` | 이전 버전 |

---

## 2. Next.js 앱 페이지 (93개)

### 2-1. 공통 / 인증 (3개)

| 페이지 | URL | 파일 |
|--------|-----|------|
| 메인 (→로그인 리다이렉트) | `/` | `app/page.tsx` |
| 로그인 | `/login` | `app/(auth)/login/page.tsx` |
| 회원가입 | `/register` | `app/(auth)/register/page.tsx` |

### 2-2. 소매 Retail (30개)

| 페이지 | URL | 비고 |
|--------|-----|------|
| 피드 (숏컷) | `/feed` | retail 레이아웃 |
| 탐색 (숏컷) | `/explore` | retail 레이아웃 |
| 브랜드 목록 | `/brands` | |
| 브랜드 상세 | `/brand/[slug]` | 동적 |
| 마이페이지 (숏컷) | `/mypage` | |
| 상품 상세 | `/retail/product/[id]` | 별도 레이아웃 |
| 피드 | `/retail/feed` | V1 실시간 상품 연동 |
| 탐색 | `/retail/explore` | |
| 마이페이지 | `/retail/mypage` | |
| 주문 목록 | `/retail/orders` | |
| 주문 상세 | `/retail/orders/[id]` | |
| 신규 주문 | `/retail/order/new` | |
| 장바구니 | `/retail/cart` | |
| 결제 | `/retail/payment` | |
| 결제 성공 | `/retail/payment/success` | |
| 결제 실패 | `/retail/payment/fail` | |
| 반품 목록 | `/retail/returns` | |
| 반품 상세 | `/retail/returns/[id]` | |
| 위탁배송 목록 | `/retail/dropship` | |
| 위탁배송 상세 | `/retail/dropship/[id]` | |
| 메시지 목록 | `/retail/messages` | |
| 메시지 상세 | `/retail/messages/[id]` | |
| 메신저 | `/retail/messenger` | |
| 스토리 | `/retail/stories` | |
| 쇼츠 목록 | `/retail/shorts` | |
| 쇼츠 상세 | `/retail/shorts/[id]` | |
| 거래처 | `/retail/trade` | |
| 거래처 신청 | `/retail/trade/apply` | |
| 트렌드 | `/retail/trends` | |
| 주소 관리 | `/retail/addresses` | |

### 2-3. 도매 Wholesale (31개)

| 페이지 | URL | 비고 |
|--------|-----|------|
| 대시보드 | `/wholesale/dashboard` | 매출·상품·주문 요약 |
| 상품 목록 | `/wholesale/products` | |
| 상품 상세 | `/wholesale/products/[id]` | |
| 상품 채널 매핑 | `/wholesale/products/[id]/channels` | |
| 주문 목록 | `/wholesale/orders` | |
| 주문 상세 | `/wholesale/orders/[id]` | |
| 정산 목록 | `/wholesale/settlements` | |
| 정산 상세 | `/wholesale/settlements/[id]` | |
| 예치금 | `/wholesale/deposit` | |
| 다운로드 | `/wholesale/downloads` | |
| 즐겨찾기 | `/wholesale/favorites` | |
| 마켓 | `/wholesale/markets` | |
| 채널 목록 | `/wholesale/channels` | |
| 채널 상세 | `/wholesale/channels/[id]` | |
| 위탁배송 목록 | `/wholesale/dropship` | |
| ���탁배송 상세 | `/wholesale/dropship/[id]` | |
| 콘텐츠 목록 | `/wholesale/content` | |
| 콘텐츠 작성 | `/wholesale/content/new` | |
| 콘텐츠 수�� | `/wholesale/content/[id]/edit` | |
| 메시지 목록 | `/wholesale/messages` | |
| 메시지 상세 | `/wholesale/messages/[id]` | |
| 메신저 | `/wholesale/messenger` | |
| 촬영 의뢰 | `/wholesale/shooting` | |
| 쇼츠 목록 | `/wholesale/shorts` | |
| 쇼츠 작성 | `/wholesale/shorts/new` | |
| 쇼츠 수정 | `/wholesale/shorts/[id]/edit` | |
| 스토리 목록 | `/wholesale/stories` | |
| 스토리 작성 | `/wholesale/stories/new` | |
| 거래처 | `/wholesale/trade` | |
| 거래 신청 상세 | `/wholesale/trade/applications/[id]` | |
| 거래 파트너 상세 | `/wholesale/trade/partners/[id]` | |

### 2-4. 관리자 Admin (26개)

| 페이지 | URL | 비고 |
|--------|-----|------|
| 대시보드 | `/admin/dashboard` | |
| 상품 관리 | `/admin/products` | |
| 상품 상세 | `/admin/products/[id]` | |
| 주문 관리 | `/admin/orders` | |
| 반품 관리 | `/admin/returns` | |
| 반품 상세 | `/admin/returns/[id]` | |
| 정산 관리 | `/admin/settlements` | |
| 정산 상세 | `/admin/settlements/[id]` | |
| 풀필먼트 | `/admin/fulfillment` | |
| 풀필먼트 상세 | `/admin/fulfillment/[id]` | |
| 채널 관리 | `/admin/channels` | |
| 채널 상세 | `/admin/channels/[id]` | |
| 파이프라인 | `/admin/pipeline` | |
| 파이프라인 상세 | `/admin/pipeline/[id]` | |
| 파이프라인 큐 | `/admin/pipeline/queue` | |
| 매입 관리 | `/admin/purchase` | |
| 매입 상세 | `/admin/purchase/[id]` | |
| 매입 주문 | `/admin/purchase/orders` | |
| 매입 입고 | `/admin/purchase/receiving` | |
| 매입 입고 상세 | `/admin/purchase/receiving/[id]` | |
| 바코드 | `/admin/purchase/barcode` | |
| 거래처 | `/admin/trade` | |
| 회원 관리 | `/admin/users` | |
| 회원 상세 | `/admin/users/[id]` | |
| 메신저 | `/admin/messenger` | |
| 구매 관리 (구형) | `/purchasing` | admin 레이아웃 |

### 2-5. 기타 역할 (3개)

| 페이지 | URL | 역할 |
|--------|-----|------|
| MD 대시보드 | `/md/dashboard` | MD (상품기획) |
| 구매담당 대시보드 | `/purchaser/dashboard` | Purchaser (매입) |
| 외주 대시보드 | `/outsource/dashboard` | Outsource (외주) |

---

## 3. API 라우트 (228개)

### 3-1. 인증 (4개)
`POST /api/auth/login` · `POST /api/auth/logout` · `GET /api/auth/me` · `POST /api/auth/register`

### 3-2. 상품/피드 (12개)
`GET /api/retail/feed` · `GET /api/feed` · `POST /api/feed` · `GET /api/feed/explore` · `GET /api/feed/search` · `GET /api/feed/{id}` · `POST /api/feed/{id}/like` · `GET /api/discover/products` · `GET /api/discover/products/{id}` · `GET /api/recommendations` · `GET /api/trends` · `GET,POST /api/user-interests`

### 3-3. 브랜드 (6개)
`GET /api/brand-pages` · `GET,PUT /api/brand-pages/mine` · `GET /api/brand-pages/{slug}` · `GET /api/brand-pages/{slug}/feed` · `POST /api/brand-pages/{slug}/follow` · `GET /api/brand-pages/{slug}/products`

### 3-4. 주문/결제/장바구니 (10개)
`GET,POST /api/cart` · `DELETE /api/cart` · `PUT,DELETE /api/cart/{id}` · `GET /api/admin/orders` · `GET /api/admin/orders/{id}` · `PATCH /api/admin/orders/{id}/status`

### 3-5. 반품 (6개)
`GET,POST /api/returns` · `GET /api/returns/{id}` · `PUT /api/returns/{id}/approve` · `PUT /api/returns/{id}/reject` · `PUT /api/returns/{id}/status` · `PUT /api/returns/{id}/tracking`

### 3-6. 배송/위탁 (12개)
`GET,POST /api/dropship` · `GET /api/dropship/{id}` · `GET /api/dropship/by-order/{orderId}` · `PUT /api/dropship/{id}` · `PUT /api/dropship/{id}/status` · `PUT /api/dropship/{id}/tracking` · `GET /api/shipments` · `GET /api/shipments/{id}` · `PUT /api/shipments/{id}/status` · `GET /api/shipments/order/{orderId}/logs` · `GET,POST,PUT,DELETE /api/shipping-addresses`

### 3-7. 정산 (5개)
`GET,POST /api/settlements` · `GET /api/settlements/{id}` · `PUT /api/settlements/{id}/confirm` · `GET /api/settlements/{id}/items` · `GET /api/settlements/{id}/logs`

### 3-8. 채널/SNS 연동 (20개+)
카페24: `GET /api/cafe24/callback` · `POST /api/cafe24/connect` · `GET /api/cafe24/products` · `POST /api/cafe24/products/push` · `PUT,DELETE /api/cafe24/products/{id}` · `GET /api/cafe24/status`
채널: `GET,POST /api/channels` · `DELETE /api/channels/{id}` · `GET,POST,DELETE /api/channels/{id}/mappings` · `POST /api/channels/{id}/sync`
SNS: `GET,POST /api/sns` · `GET,POST /api/sns/posts` · `POST /api/sns/posts/bulk` · `GET,DELETE /api/sns/posts/{id}` · `GET /api/sns/posts/{id}/analytics` · `GET /api/sns/posts/{id}/hashtags` · `POST /api/sns/posts/{id}/schedule` · `GET /api/sns/{connectionId}/optimal-time` · `DELETE /api/sns/{id}`

### 3-9. 콘텐츠/쇼츠/스토리 (22개)
콘텐츠: `POST /api/contents` · `GET /api/contents/mine` · `GET,PUT,DELETE /api/contents/{id}`
쇼츠: `GET,POST /api/shorts` · `GET,PUT,DELETE /api/shorts/{id}` · `GET,POST /api/shorts/{id}/comments` · `DELETE /api/shorts/{id}/comments/{cid}` · `POST /api/shorts/{id}/like` · `GET,POST,DELETE /api/shorts/{id}/tags` · `POST /api/shorts/{id}/view` · `GET /api/shorts/{id}/views`
스토리: `GET,POST /api/stories` · `GET /api/stories/mine` · `GET,DELETE /api/stories/{id}` · `POST /api/stories/{id}/view`

### 3-10. 메시지/대화 (7개)
`GET,POST /api/conversations` · `GET,DELETE /api/conversations/{id}` · `GET,POST /api/conversations/{id}/messages` · `PUT /api/conversations/{id}/mute` · `PUT /api/conversations/{id}/pin` · `POST /api/conversations/{id}/read`

### 3-11. 거래/팔로우 (5개)
`POST /api/trade-applications` · `GET /api/trade-applications` · `GET /api/trade-applications/received` · `PUT /api/trade-applications/{id}/respond` · `POST,DELETE /api/follows/{userId}` · `GET /api/follows/{userId}/followers`

### 3-12. 도매 전용 (8개)
`GET /api/wholesale/dashboard` · `GET /api/wholesale/dashboard/summary` · `GET /api/wholesale/deposit/balance` · `GET /api/wholesale/deposit/transactions` · `GET /api/wholesale/downloads` · `GET /api/wholesale/products` · `GET,POST,GET,DELETE /api/wholesale/shooting-requests`

### 3-13. 관리자 대시보드 (9개)
`GET /api/admin/dashboard` · `GET /api/dashboard/overview` · `GET /api/dashboard/stats` · `GET /api/dashboard/purchasing/summary` · `GET /api/dashboard/purchasing/trend` · `GET /api/dashboard/purchasing/suppliers` · `GET /api/dashboard/purchasing/recent-orders` · `GET /api/dashboard/purchasing/recent-inbounds` · `GET /api/dashboard/purchasing/alerts`

### 3-14. 바코드/매입 (5개)
`GET /api/barcodes` · `POST /api/barcodes/generate` · `POST /api/barcodes/print-batch` · `GET /api/barcodes/{id}` · `PUT /api/barcodes/{id}/status`

### 3-15. 위시리스트 (3개)
`GET /api/wishlists` · `POST /api/wishlists/{productId}` · `DELETE /api/wishlists/{productId}`

### 3-16. 기타
`GET /api/health` · `GET /api/retail/dashboard`

---

## 4. 권한(Role) 매핑

| 역할 | 라우트 그룹 | 페이지 수 | 주요 기능 |
|------|-------------|-----------|-----------|
| **admin** | `/admin/*` | 26 | 전체 관리, 상품/주문/정산/회원/파이프라인 |
| **wholesale** | `/wholesale/*` | 31 | 본인 상품 관리, 채널 연동, 촬영 의뢰, 콘텐츠 |
| **retail** | `/retail/*` | 30 | 상품 탐색, 주문, 반품, 위탁배송, 메시지 |
| **md** | `/md/*` | 1 | 상품 기획 대시보드 |
| **purchaser** | `/purchaser/*` | 1 | 매입 대시보드 |
| **outsource** | `/outsource/*` | 1 | 외주 대시보드 |

---

## 5. V1 연동 현황

| 항목 | 상태 | 설명 |
|------|------|------|
| V1 회원 DB | ✅ 연동 | `DB::connection('v1')` → autoda DB, `v1_idx`/`v1_userid` 매핑 |
| V1 상품 피드 | ✅ 연동 | `/api/retail/feed` → V1 goods 테이블 실시간 조회 (77,410건) |
| V1 이미지 | ✅ 연동 | Docker 볼륨 `/v1_img:ro` + HTTPS URL 변환 |
| V1 로그인 호환 | ✅ 지원 | 이메일 또는 V1 userid로 로그인, bcrypt $2y$ 호환 완료 |
| 도매 상품 필터 | ✅ 수정완료 | `goods.GoodsEtc6`(매입처) = V1 `users.username` 매핑 |
| V1 Health Check | ❌ 미구현 | `/api/health`에 V1 DB·이미지 마운트 점검 미포함 |

---

## 6. 금일 수정사항 (2026-04-29)

| 이슈 | 심각도 | 수정 내용 |
|------|--------|-----------|
| Mixed Content (HTTPS→HTTP) | P0 | `NEXT_PUBLIC_API_URL` → `https://v2.newtalk.kr/api`, 프론트 리빌드 |
| $2a$ bcrypt 비호환 | P0 | 3,202건 `$2a$` → `$2y$` 벌크 변환 |
| `GoodPrice` 컬럼 부재 | P1 | RetailFeedController에서 존재하지 않는 컬럼 참조 4곳 제거 |
| `depositBalance` 메서드 중복 | P1 | WholesaleDashboardController 메서드명 충돌 수정 |
| 도매 상품 전체 노출 | P1 | `user_id` 필터 → `GoodsEtc6`(매입처)=`username` 매핑으로 교체 |
| dialog.tsx 미존재 | P2 | shadcn/ui Dialog 컴포넌트 생성 (wholesale/shooting 빌드 실패 해결) |

---

## 총계

| 구분 | 수량 |
|------|------|
| 정적 HTML 페이지 | 4 |
| Next.js 앱 페이지 | 93 |
| **프론트엔드 전체** | **97** |
| API 라우트 | 228 |
| Docker 컨테이너 | 6 |
| 사용자 역할 | 6 (admin, wholesale, retail, md, purchaser, outsource) |
