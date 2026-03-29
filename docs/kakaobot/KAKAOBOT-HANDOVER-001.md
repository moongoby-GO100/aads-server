# KAKAOBOT 프로젝트 인수인계서

**문서 ID**: KAKAOBOT-HANDOVER-001
**작성일**: 2026-03-28 (KST)
**작성자**: AADS PM/CTO AI
**인계 대상**: 카카오봇 프로젝트 총괄관리자
**문서 버전**: v1.0

---

## 1. 프로젝트 개요

### 1.1 서비스 정의
- **서비스명**: 카카오봇 — AI 메시지 서비스
- **도메인**: https://kakaobot.newtalk.kr
- **백엔드 API**: https://aads.newtalk.kr/api/v1/kakao-bot/*
- **목적**: AI 기반 카카오톡 메시지 자동화 SaaS 플랫폼
  - 축하/안부/마케팅/알림 등 메시지 자동 생성
  - 기념일 자동 발송 (음력 포함)
  - 예약 발송
  - PC Agent 기반 카카오톡 GUI 자동화

### 1.2 발송 채널 (3가지)

| 채널 | 방식 | 상태 | 비고 |
|------|------|------|------|
| **A안: 알리고 SMS/알림톡** | 알리고 REST API 경유 | ✅ 구현 완료 | `ALIGO_API_KEY` 등 환경변수 설정 필요 |
| **B안: 메신저봇R** | Android 메신저봇R 앱 → 웹훅 | ⚠️ BOT_TOKEN 미설정 | `msgbot_script/kakaobot.js` 스크립트 제공 |
| **C안: PC Agent** | Windows PyAutoGUI 기반 카카오톡 GUI 조작 | ✅ 구현 완료 | EXE 빌드/다운로드 API 제공 |

### 1.3 기술 스택
- **백엔드**: FastAPI 0.115, Python 3.11, asyncpg (PostgreSQL 15)
- **프론트엔드**: Next.js 16, TypeScript, Tailwind CSS (App Router)
- **AI**: Claude Haiku (call_llm_with_fallback 경유, R-AUTH 준수)
- **SMS/알림톡**: 알리고(Aligo) REST API
- **인프라**: Docker Compose, 서버68 (68.183.183.11)
- **PWA**: manifest-kakaobot.json, 아이콘 192/512px

---

## 2. 아키텍처

### 2.1 시스템 구성도

```
[사용자 브라우저] → kakaobot.newtalk.kr (Next.js)
       ↓ API 호출
[aads.newtalk.kr/api/v1/kakao-bot/*] (FastAPI)
       ↓
   ┌───┼───┬───────────┐
   ↓   ↓   ↓           ↓
[PostgreSQL]  [Claude AI]  [알리고 API]  [PC Agent (Windows)]
  (7 테이블)  (문구 생성)   (SMS/알림톡)   (카카오톡 GUI)
                                          ↕
                                    [메신저봇R (Android)]
                                    (웹훅 → AI 응답)
```

### 2.2 라우터 등록 (main.py)
```python
# app/main.py
from app.api.kakao_bot import router as kakao_bot_router
app.include_router(kakao_bot_router, prefix="/api/v1", tags=["kakao-bot"])

# CORS 허용
allow_origins = ["https://aads.newtalk.kr", "https://kakaobot.newtalk.kr"]

# 스케줄러 시작 (lifespan)
from app.services.kakaobot_scheduler import start_scheduler_tasks
```

### 2.3 도메인 분리
- `kakaobot.newtalk.kr` 접속 시 → `layout.tsx`에서 호스트명 감지 → 카카오봇 전용 UI/매니페스트/아이콘 적용
- `aads.newtalk.kr` 접속 시 → AADS 대시보드 UI
- 로그인 페이지도 호스트명 기반 분기 (`login/page.tsx:91`)

---

## 3. 파일 구조

### 3.1 백엔드 (aads-server)

| 파일 | 줄수 | 역할 |
|------|------|------|
| `app/api/kakao_bot.py` | 1,619 | **핵심 라우터** — 35개 API 엔드포인트, 14개 Pydantic 모델, 내부 헬퍼 |
| `app/services/kakaobot_ai.py` | 150 | AI 문구 생성 엔진 (Claude Haiku) |
| `app/services/kakaobot_scheduler.py` | 244 | 예약 발송 + 기념일 자동 스케줄 생성 |
| `app/services/aligo_client.py` | 238 | 알리고 SMS/알림톡 REST API 클라이언트 |
| `app/services/kakao_search_service.py` | 116 | Kakao Developers 웹검색 API (카카오봇과 무관, AADS 검색용) |
| `app/_tmp_kakaobot_prompt.json` | 3 | 총괄관리자 시스템 프롬프트 (임시 파일) |
| `msgbot_script/kakaobot.js` | 73 | 메신저봇R 스크립트 (B안) |
| `pc_agent/commands/kakao.py` | 196 | PC Agent 카카오톡 GUI 조작 (C안) |
| `pc_agent/commands/kakao_auto.py` | 469 | PC Agent 자동 응답 모듈 (C안) |
| `pc_agent/kakaobot-setup.spec` | 70 | PyInstaller EXE 빌드 스펙 |
| `migrations/038_kakaobot_saas.sql` | 79 | DB 마이그레이션 (4 테이블) |

### 3.2 프론트엔드 (aads-dashboard)

| 파일 | 역할 |
|------|------|
| `src/app/kakaobot/layout.tsx` | 9탭 네비게이션 레이아웃 (브랜드 컬러 #FFE812) |
| `src/app/kakaobot/page.tsx` | 메인 대시보드 (통계 카드 + 기능 그리드 + 빠른 시작) |
| `src/app/kakaobot/contacts/page.tsx` | 연락처 CRUD |
| `src/app/kakaobot/anniversaries/page.tsx` | 기념일 관리 |
| `src/app/kakaobot/templates/page.tsx` | 템플릿 관리 |
| `src/app/kakaobot/ai-writer/page.tsx` | AI 문구 생성기 (216줄) |
| `src/app/kakaobot/scheduled/page.tsx` | 예약 발송 관리 |
| `src/app/kakaobot/history/page.tsx` | 발송 이력 조회 |
| `src/app/kakaobot/settings/page.tsx` | 설정 (알리고/메신저봇R 연동) |
| `src/app/kakaobot/agent/page.tsx` | PC Agent 관리 |
| `src/components/KakaoBotHeader.tsx` | 공통 헤더 (로그아웃 포함) |
| `public/manifest-kakaobot.json` | PWA 매니페스트 |
| `public/icon-kakaobot-192.png` | PWA 아이콘 192x192 |
| `public/icon-kakaobot-512.png` | PWA 아이콘 512x512 |

---

## 4. API 엔드포인트 (35개)

### 4.1 AI 자동 응답 (C안)
| Method | 경로 | 함수 | 설명 |
|--------|------|------|------|
| POST | `/kakao-bot/respond` | `kakao_bot_respond` | 카카오톡 메시지 → AI 응답 생성 |

### 4.2 PC Agent (C안)
| Method | 경로 | 함수 | 설명 |
|--------|------|------|------|
| GET | `/kakao-bot/agent/version` | `agent_version` | Agent 최신 버전 조회 |
| GET | `/kakao-bot/agent/download` | `agent_download` | Agent ZIP 다운로드 |
| GET | `/kakao-bot/agent/download-exe` | `agent_download_exe` | Agent EXE 다운로드 |
| POST | `/kakao-bot/agent/register` | `agent_register` | Agent 등록/하트비트 |

### 4.3 메신저봇R (B안)
| Method | 경로 | 함수 | 설명 |
|--------|------|------|------|
| POST | `/kakao-bot/msgbot/webhook` | `msgbot_webhook` | 메신저봇R 웹훅 수신 |
| POST | `/kakao-bot/msgbot/config` | `msgbot_config_save` | 봇 설정 저장 |
| GET | `/kakao-bot/msgbot/config` | `msgbot_config_get` | 봇 설정 조회 |
| GET | `/kakao-bot/msgbot/logs` | `msgbot_logs` | 봇 로그 조회 |
| POST | `/kakao-bot/msgbot/token/generate` | `msgbot_token_generate` | 봇 토큰 생성 |

### 4.4 알리고 SMS/알림톡 (A안)
| Method | 경로 | 함수 | 설명 |
|--------|------|------|------|
| POST | `/kakao-bot/aligo/send-sms` | `aligo_send_sms` | SMS/LMS 발송 |
| POST | `/kakao-bot/aligo/send-alimtalk` | `aligo_send_alimtalk` | 카카오 알림톡 발송 |
| GET | `/kakao-bot/aligo/remain` | `aligo_remain` | 잔여 건수 조회 |
| GET | `/kakao-bot/aligo/history` | `aligo_history` | 발송 이력 조회 |
| POST | `/kakao-bot/aligo/cancel` | `aligo_cancel` | 예약 발송 취소 |
| POST | `/kakao-bot/aligo/ai-reply-sms` | `aligo_ai_reply_sms` | AI 문구 생성 후 SMS 즉시 발송 |

### 4.5 연락처 CRUD
| Method | 경로 | 함수 | 설명 |
|--------|------|------|------|
| POST | `/kakao-bot/contacts` | `create_contact` | 연락처 생성 |
| GET | `/kakao-bot/contacts` | `list_contacts` | 연락처 목록 |
| PUT | `/kakao-bot/contacts/{id}` | `update_contact` | 연락처 수정 |
| DELETE | `/kakao-bot/contacts/{id}` | `delete_contact` | 연락처 삭제 |

### 4.6 기념일 관리
| Method | 경로 | 함수 | 설명 |
|--------|------|------|------|
| POST | `/kakao-bot/anniversaries` | `create_anniversary` | 기념일 생성 |
| GET | `/kakao-bot/anniversaries` | `list_anniversaries` | 기념일 목록 |
| GET | `/kakao-bot/anniversaries/upcoming` | `upcoming_anniversaries` | 다가오는 기념일 |
| PUT | `/kakao-bot/anniversaries/{id}` | `update_anniversary` | 기념일 수정 |
| DELETE | `/kakao-bot/anniversaries/{id}` | `delete_anniversary` | 기념일 삭제 |

### 4.7 AI 문구 생성
| Method | 경로 | 함수 | 설명 |
|--------|------|------|------|
| POST | `/kakao-bot/ai/generate` | `ai_generate` | AI 문구 다중 생성 |
| POST | `/kakao-bot/ai/improve` | `ai_improve` | 기존 문구 개선 |

### 4.8 템플릿 관리
| Method | 경로 | 함수 | 설명 |
|--------|------|------|------|
| POST | `/kakao-bot/templates` | `create_template` | 템플릿 생성 |
| GET | `/kakao-bot/templates` | `list_templates` | 템플릿 목록 |
| PUT | `/kakao-bot/templates/{id}` | `update_template` | 템플릿 수정 |
| DELETE | `/kakao-bot/templates/{id}` | `delete_template` | 템플릿 삭제 |
| POST | `/kakao-bot/templates/seed` | `seed_templates` | 시드 템플릿 30개 삽입 |

### 4.9 예약 발송
| Method | 경로 | 함수 | 설명 |
|--------|------|------|------|
| POST | `/kakao-bot/scheduled` | `create_scheduled` | 예약 발송 생성 |
| GET | `/kakao-bot/scheduled` | `list_scheduled` | 예약 발송 목록 |
| DELETE | `/kakao-bot/scheduled/{id}` | `cancel_scheduled` | 예약 발송 취소 |

---

## 5. 데이터베이스 (7 테이블)

### 5.1 마이그레이션 파일로 생성되는 4개 테이블 (`038_kakaobot_saas.sql`)

#### kakaobot_contacts
```sql
CREATE TABLE IF NOT EXISTS kakaobot_contacts (
    id SERIAL PRIMARY KEY,
    user_id VARCHAR(100) NOT NULL DEFAULT 'default',
    name VARCHAR(100) NOT NULL,
    phone VARCHAR(20) NOT NULL,
    group_name VARCHAR(100) DEFAULT '',
    relationship VARCHAR(50) DEFAULT '',
    memo TEXT DEFAULT '',
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
-- 인덱스: (user_id), (phone)
```

#### kakaobot_anniversaries
```sql
CREATE TABLE IF NOT EXISTS kakaobot_anniversaries (
    id SERIAL PRIMARY KEY,
    user_id VARCHAR(100) NOT NULL DEFAULT 'default',
    contact_id INT REFERENCES kakaobot_contacts(id) ON DELETE CASCADE,
    title VARCHAR(200) NOT NULL,
    anniversary_date DATE NOT NULL,
    is_lunar BOOLEAN DEFAULT FALSE,
    recurrence VARCHAR(20) DEFAULT 'yearly',  -- yearly/monthly/once
    remind_days_before INT DEFAULT 0,
    auto_send BOOLEAN DEFAULT FALSE,
    template_id INT,
    custom_message TEXT DEFAULT '',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
-- 인덱스: (user_id), (anniversary_date), (contact_id)
```

#### kakaobot_templates
```sql
CREATE TABLE IF NOT EXISTS kakaobot_templates (
    id SERIAL PRIMARY KEY,
    user_id VARCHAR(100) NOT NULL DEFAULT 'default',
    category VARCHAR(50) NOT NULL,
    title VARCHAR(200) NOT NULL,
    content TEXT NOT NULL,
    tone VARCHAR(30) DEFAULT 'friendly',
    tags JSONB DEFAULT '[]',
    use_count INT DEFAULT 0,
    is_system BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
-- 인덱스: (user_id), (category)
```

#### kakaobot_scheduled
```sql
CREATE TABLE IF NOT EXISTS kakaobot_scheduled (
    id SERIAL PRIMARY KEY,
    user_id VARCHAR(100) NOT NULL DEFAULT 'default',
    contact_id INT REFERENCES kakaobot_contacts(id) ON DELETE CASCADE,
    anniversary_id INT REFERENCES kakaobot_anniversaries(id) ON DELETE SET NULL,
    template_id INT REFERENCES kakaobot_templates(id) ON DELETE SET NULL,
    message TEXT NOT NULL,
    scheduled_at TIMESTAMPTZ NOT NULL,
    status VARCHAR(20) DEFAULT 'pending',  -- pending/sent/failed/cancelled
    sent_at TIMESTAMPTZ,
    send_result JSONB DEFAULT '{}',
    retry_count INT DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
-- 인덱스: (status, scheduled_at), (user_id)
```

### 5.2 런타임 자동 생성 3개 테이블

⚠️ **중요**: 아래 3개 테이블은 마이그레이션 SQL이 아니라 `kakao_bot.py` 내 `_ensure_*` 함수로 런타임에 자동 생성됩니다.

| 테이블 | 생성 함수 | 용도 |
|--------|----------|------|
| `kakao_msgbot_config` | `_ensure_msgbot_tables()` | 메신저봇R 설정 (user_id, config JSONB) |
| `kakao_msgbot_logs` | `_ensure_msgbot_tables()` | 메신저봇R 웹훅 로그 |
| `kakaobot_aligo_logs` | `_ensure_aligo_tables()` | 알리고 발송 로그 |

---

## 6. 핵심 서비스 상세

### 6.1 AI 문구 생성 (kakaobot_ai.py)

```python
# 2개 공개 함수
async def generate_messages(occasion, recipient_name, relationship="", tone="friendly",
                           extra_context="", count=3) -> List[str]:
    """Claude Haiku, max_tokens=800. 번호 리스트 파싱 후 count개 반환. 에러 시 빈 리스트."""

async def improve_message(original, instruction="", tone="friendly") -> str:
    """Claude Haiku, max_tokens=400. 에러 시 원본 반환."""

# 내부 파서
def _parse_numbered_messages(text, expected) -> List[str]:
    """'1. ', '1) ', '01. ', '01) ' 패턴 지원, # 헤더 제거."""
```

**R-AUTH 준수**: 두 함수 모두 `app/core/anthropic_client.call_llm_with_fallback()` 경유. 직접 Anthropic SDK 호출 없음.

### 6.2 예약 발송 스케줄러 (kakaobot_scheduler.py)

2개의 asyncio 루프가 서버 시작 시 `main.py` lifespan에서 실행됨:

```
┌─ _scheduler_loop_send() ──────────────────────────────────┐
│  매 60초마다 check_and_send_scheduled() 호출              │
│  → pending 상태 + scheduled_at ≤ NOW() 건 최대 20건 조회  │
│  → aligo_client.send_sms() 발송                          │
│  → result_code == 1 → 'sent' / 아니면 → 'failed'        │
│  → 예외 시 retry_count + 1                               │
└───────────────────────────────────────────────────────────┘

┌─ _scheduler_loop_anniversary() ───────────────────────────┐
│  매일 새벽 02:00 KST에 generate_anniversary_schedules()   │
│  → auto_send=TRUE 기념일 조회                             │
│  → 오늘 발송 대상 계산 (remind_days_before 차감)          │
│  → 중복 체크 후 09:00 예약 INSERT                         │
│  → 메시지: custom_message > template > AI 생성 > 기본문구 │
│  → 음력 변환: korean_lunar_calendar (미설치 시 양력 폴백) │
└───────────────────────────────────────────────────────────┘
```

### 6.3 알리고 클라이언트 (aligo_client.py)

| 함수 | 설명 | API URL |
|------|------|---------|
| `is_available()` | API_KEY + USER_ID 설정 확인 | - |
| `send_sms(receiver, msg, ...)` | SMS/LMS 발송 (EUC-KR 90바이트 초과 시 LMS 자동 전환) | `https://apis.aligo.in/send/` |
| `send_alimtalk(receiver, template_code, message, ...)` | 카카오 알림톡 (failover=Y 시 SMS 폴백) | `https://kakaoapi.aligo.in/akv10/alimtalk/send/` |
| `get_remain()` | 잔여 건수 조회 | `https://apis.aligo.in/remain/` |
| `get_send_list(page, page_size, start_date, limit_day)` | 발송 이력 | `https://apis.aligo.in/list/` |
| `cancel_reservation(mid)` | 예약 취소 | `https://apis.aligo.in/cancel/` |

⚠️ **모든 함수**: `is_available()` 선 검사 → 미설정 시 `{result_code: -1}` 반환 (예외 미발생)

### 6.4 메신저봇R 스크립트 (kakaobot.js)

```javascript
SERVER_URL = "https://aads.newtalk.kr/api/v1/kakao-bot/msgbot/webhook"
BOT_TOKEN = "여기에_토큰_입력"  // ⚠️ 미설정 상태
BOT_TRIGGER = "@봇"
TIMEOUT_MS = 10000
```

- 그룹채팅: `BOT_TRIGGER` 포함 시만 응답
- 1:1 채팅: 무조건 응답
- `org.jsoup`으로 서버 POST → `should_reply: true`이면 `replier.reply(reply)`

### 6.5 PC Agent (C안)

#### kakao.py — 수동 전송
```python
async def kakao_send(params: Dict) -> Dict:
    """4단계: 카카오톡 찾기 → 채팅방 검색 → 메시지 입력 → 전송"""

async def kakao_read(params: Dict) -> Dict:
    """Ctrl+A → Ctrl+C → 최근 20줄 파싱"""
```

#### kakao_auto.py — 자동 응답
```python
class KakaoAutoResponder:
    """싱글톤. 2초 간격 감시 루프.
    delay_min=1.0, delay_max=5.0, tone="friendly", max_length=200, rate_limit=10/min
    히스토리: ~/.aads_kakao_auto/history.jsonl"""
```

6개 핸들러: `kakao_auto_start`, `kakao_auto_stop`, `kakao_auto_status`, `kakao_auto_config`, `kakao_auto_rooms`, `kakao_auto_history`

---

## 7. 환경변수

| 변수 | 용도 | 필수 | 현재 상태 |
|------|------|------|-----------|
| `ALIGO_API_KEY` | 알리고 SMS 인증 키 | A안 사용 시 필수 | .env 확인 필요 |
| `ALIGO_USER_ID` | 알리고 사용자 ID | A안 사용 시 필수 | .env 확인 필요 |
| `ALIGO_SENDER` | 발신번호 (010XXXXXXXX) | A안 사용 시 필수 | .env 확인 필요 |
| `ALIGO_SENDER_KEY` | 카카오 알림톡 발신 프로필 키 | 알림톡 사용 시 필수 | .env 확인 필요 |
| `KAKAO_REST_API_KEY` | Kakao Developers 웹검색 | 검색 기능 시 | .env 확인 필요 |
| `DATABASE_URL` | PostgreSQL 접속 | 필수 | AADS 공통 |
| `ANTHROPIC_AUTH_TOKEN` | Claude AI (R-AUTH) | AI 생성 시 필수 | AADS 공통 |

⚠️ **docker-compose.prod.yml에 알리고 환경변수 미등록** — `.env` 파일에서 직접 로드됨. `is_available()`이 `False`이면 스케줄러 발송이 전량 스킵됩니다.

---

## 8. 시드 데이터 (템플릿 30개)

`POST /kakao-bot/templates/seed` 호출 시 삽입:

| 카테고리 | 수량 | 예시 |
|----------|------|------|
| birthday | 5 | 생일 축하 (공식/친근/가족) |
| wedding | 5 | 결혼 축하 |
| new_year | 5 | 새해 인사 |
| chuseok | 5 | 추석 인사 |
| marketing | 5 | 신규오픈/할인/재방문/신제품/시즌 프로모션 |
| greeting | 5 | 계절인사/안부/건강/감사/응원 |

임계치: 기존 템플릿 ≥ 30개이면 시드 스킵.

---

## 9. 알려진 이슈 (14건)

### P1 (긴급)

| # | 이슈 | 위치 | 설명 |
|---|------|------|------|
| 1 | AI 생성기 에러 처리 부족 | `kakaobot_ai.py` | 빈 리스트 반환 시 프론트엔드에서 에러 표시 없음 |
| 2 | 기념일 음력 변환 미완성 | `kakaobot_scheduler.py:_resolve_anniversary_date` | `korean_lunar_calendar` 미설치 시 양력 폴백 — 음력 기능 사실상 미동작 |
| 3 | cancel_scheduled 권한 검증 누락 | `kakao_bot.py` L1604-1615 | `user_id` 검증 없이 타 사용자 예약 취소 가능 (보안 버그) |

### P2 (중요)

| # | 이슈 | 위치 | 설명 |
|---|------|------|------|
| 4 | 메신저봇R BOT_TOKEN 미설정 | `msgbot_script/kakaobot.js:12` | 플레이스홀더 상태 — B안 미동작 |
| 5 | 알리고 환경변수 docker-compose 미등록 | `docker-compose.prod.yml` | `.env` 직접 설정 필요, 누락 시 발송 전량 스킵 |
| 6 | 마이그레이션/런타임 DDL 분리 | `038_kakaobot_saas.sql` vs `_ensure_*` | 4개 테이블은 SQL 파일, 3개는 런타임 생성 — 일관성 부족 |
| 7 | 프론트엔드-백엔드 필드명 불일치 가능성 | 각 page.tsx ↔ kakao_bot.py | 전수 검증 필요 |

### P3 (개선)

| # | 이슈 | 설명 |
|---|------|------|
| 8 | 페이징 미구현 | 연락처/템플릿 목록에 offset/cursor 페이징 없음 |
| 9 | 검색 기능 부재 | 연락처/템플릿 검색 UI/API 없음 |
| 10 | 대량 발송 | 그룹 대상 일괄 발송 미구현 |
| 11 | 발송 통계 대시보드 | 일별/월별 발송 통계 시각화 없음 |
| 12 | 사용자별 발송 한도 | rate limiting 미구현 |
| 13 | 알림톡 템플릿 관리 | 카카오 비즈니스 채널 연동 미구현 |
| 14 | 다국어 지원 | 한국어 전용, 영어 등 미지원 |

---

## 10. 코딩 규칙 및 보안

### 10.1 필수 준수 사항
- **R-AUTH**: `ANTHROPIC_API_KEY` 직접 사용 금지. `call_llm_with_fallback()` 경유 필수.
- **R-KEY**: API 키 소스코드 하드코딩 절대 금지. `.env`만 허용.
- **R-COMMIT**: `--no-verify` 금지. pre-commit hook 5단계 통과 필수.
- **R-DOCKER**: `docker compose up -d` 전체 실행 금지. 단일 서비스만 재시작.
- **async/await**: 모든 I/O 작업 async.
- **Pydantic v2**: BaseModel + field_validator.
- **한국어 주석**: docstring 한국어.

### 10.2 인증 체계
- 프론트엔드: `localStorage.aads_token` 또는 쿠키 `aads_token`
- API: `Authorization: Bearer {token}` 헤더
- 의존성: `get_current_user` (FastAPI Depends)

---

## 11. 배포 방법

### 백엔드 재시작 (코드 수정 반영)
```bash
# 컨테이너 재생성 없이 프로세스만 재시작
docker exec aads-server supervisorctl restart aads-api
```

### 프론트엔드 빌드/배포
```bash
docker compose -f /root/aads/aads-dashboard/docker-compose.yml build aads-dashboard
docker compose -f /root/aads/aads-dashboard/docker-compose.yml up -d aads-dashboard
```

### DB 마이그레이션
```bash
docker exec aads-postgres psql -U aads -d aads -f /migrations/038_kakaobot_saas.sql
```

### 헬스체크
```bash
curl -s https://aads.newtalk.kr/api/v1/ops/health-check | python3 -m json.tool
```

---

## 12. Git 이력

| 날짜 | 커밋 | 설명 |
|------|------|------|
| 2026-03-26 | `59d64fb` | SaaS 회원가입 + CORS kakaobot.newtalk.kr |
| 2026-03-16 | `df3ef89` | web_search Google→Naver→Kakao 수정 |
| 2026-03-09 | `5d4ee8f` | Naver + Kakao 웹검색 폴백 통합 |

### Phase 완료 기록
- **Phase 1**: C안 (PC Agent AI 자동 응답) + B안 (메신저봇R 웹훅) + A안 (알리고 SMS)
- **Phase 2**: 프론트엔드 9페이지 + SaaS 회원가입 + 시드 템플릿 30개 + 기념일 자동 발송
- RESULT 문서: `/root/aads/RESULT_kakaobot_phase2.md`, `/root/aads/RESULT-kakaobot-phase2.md`

---

## 13. 총괄관리자 시스템 프롬프트 위치

`/root/aads/aads-server/app/_tmp_kakaobot_prompt.json`에 총괄관리자 역할/임무/핵심책임/관할 파일/로드맵/보안규칙이 정의되어 있습니다. 이 파일의 `system_prompt` 내용을 세션 초기화 시 주입하면 됩니다.

---

## 14. 즉시 조치 권장 사항

1. **P1-3 보안 패치**: `cancel_scheduled`에 `user_id` 검증 추가
2. **P1-2 음력 패키지 설치**: `pip install korean-lunar-calendar` + Dockerfile 반영
3. **P2-4 BOT_TOKEN 설정**: 메신저봇R 사용 시 토큰 생성 후 스크립트 배포
4. **P2-5 환경변수 정비**: 알리고 키 `.env` 설정 확인 + 발송 테스트
5. **P2-6 DDL 통합**: 런타임 `_ensure_*` 함수의 DDL을 마이그레이션 SQL로 이전

---

*끝. 추가 질문은 AADS PM/CTO AI에게 문의하세요.*
