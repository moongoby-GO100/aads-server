# KAKAOBOT 기술 상세 문서

**문서 ID**: KAKAOBOT-TECHNICAL-001
**작성일**: 2026-03-28 (KST)
**인수인계서 참조**: KAKAOBOT-HANDOVER-001

---

## 1. kakao_bot.py 전체 구조 (1,619줄)

### 1.1 임포트 및 라우터 초기화
```python
# L1-19
from app.auth import get_current_user  # JWT 인증 의존성
router = APIRouter(prefix="/kakao-bot")
logger = logging.getLogger(__name__)
```

### 1.2 Pydantic 모델 (14개)

| 모델 | 줄 | 필드 |
|------|-----|------|
| `KakaoBotRequest` | L36 | sender, message, room (Optional) |
| `KakaoBotResponse` | L45 | reply, should_reply |
| `AgentRegisterRequest` | L249 | agent_id, hostname, version, os_info |
| `MsgbotWebhookRequest` | - | sender, message, room, isGroupChat, bot_token |
| `MsgbotWebhookResponse` | - | reply, should_reply |
| `MsgbotConfigPayload` | - | enabled, trigger, tone, max_length |
| `MsgbotConfigRequest` | - | config: MsgbotConfigPayload |
| `AligoSmsRequest` | - | receiver, msg, sender, title, rdate, rtime, testmode_yn |
| `AligoAlimtalkRequest` | - | receiver, template_code, message, subject, button, failover |
| `AligoCancelRequest` | - | mid |
| `AligoAiReplyRequest` | - | receiver, occasion, recipient_name, relationship, tone |
| `ContactCreate/Update` | - | name, phone, group_name, relationship, memo, metadata |
| `AnniversaryCreate/Update` | - | contact_id, title, anniversary_date, is_lunar, recurrence, remind_days_before, auto_send, template_id, custom_message |
| `AiGenerateRequest` | - | occasion, recipient_name, relationship, tone, extra_context, count |
| `AiImproveRequest` | - | original, instruction, tone |
| `TemplateCreate/Update` | - | category, title, content, tone, tags |
| `ScheduledCreate` | - | contact_id, anniversary_id, template_id, message, scheduled_at |

### 1.3 내부 헬퍼 함수

| 함수 | 역할 |
|------|------|
| `_pool()` | DB 커넥션 풀 반환 (`get_pool()`) |
| `_build_agent_zip()` | PC Agent 소스 ZIP 빌드 |
| `_ensure_msgbot_tables()` | 메신저봇R 테이블 런타임 DDL |
| `_get_msgbot_config(conn, user_id)` | 봇 설정 JSON 조회 |
| `_save_msgbot_log(conn, ...)` | 웹훅 로그 저장 |
| `_ensure_aligo_tables()` | 알리고 로그 테이블 런타임 DDL |
| `_log_aligo_send(conn, ...)` | 알리고 발송 로그 저장 |
| `_ensure_saas_tables()` | SaaS 4테이블 존재 확인 (마이그레이션 대체) |

### 1.4 전체 함수 목록 (번호순)

```
L36   class KakaoBotRequest
L45   class KakaoBotResponse
L52   async def kakao_bot_respond         — POST /respond
L106  async def agent_version             — GET /agent/version
L149  def _build_agent_zip
L177  async def agent_download            — GET /agent/download
L216  async def agent_download_exe        — GET /agent/download-exe
L249  class AgentRegisterRequest
L257  async def agent_register            — POST /agent/register
L333  async def _ensure_msgbot_tables
L372  async def msgbot_webhook            — POST /msgbot/webhook
L430  async def msgbot_config_save        — POST /msgbot/config
L460  async def msgbot_config_get         — GET /msgbot/config
L485  async def msgbot_logs               — GET /msgbot/logs
L510  async def msgbot_token_generate     — POST /msgbot/token/generate
L550  async def _ensure_aligo_tables
L585  async def aligo_send_sms            — POST /aligo/send-sms
L640  async def aligo_send_alimtalk       — POST /aligo/send-alimtalk
L700  async def aligo_remain              — GET /aligo/remain
L720  async def aligo_history             — GET /aligo/history
L750  async def aligo_cancel              — POST /aligo/cancel
L780  async def aligo_ai_reply_sms        — POST /aligo/ai-reply-sms
L850  async def _ensure_saas_tables
L900  async def create_contact            — POST /contacts
L950  async def list_contacts             — GET /contacts
L990  async def update_contact            — PUT /contacts/{id}
L1040 async def delete_contact            — DELETE /contacts/{id}
L1070 async def create_anniversary        — POST /anniversaries
L1120 async def list_anniversaries        — GET /anniversaries
L1170 async def upcoming_anniversaries    — GET /anniversaries/upcoming
L1220 async def update_anniversary        — PUT /anniversaries/{id}
L1270 async def delete_anniversary        — DELETE /anniversaries/{id}
L1310 async def ai_generate              — POST /ai/generate
L1340 async def ai_improve               — POST /ai/improve
L1370 async def create_template           — POST /templates
L1401 async def list_templates            — GET /templates
L1440 async def update_template           — PUT /templates/{id}
L1480 async def delete_template           — DELETE /templates/{id}
L1500 async def seed_templates            — POST /templates/seed
L1550 async def create_scheduled          — POST /scheduled
L1572 async def list_scheduled            — GET /scheduled
L1604 async def cancel_scheduled          — DELETE /scheduled/{id}
```

---

## 2. kakaobot_ai.py 전체 코드 분석 (150줄)

### 핵심 로직

```python
async def generate_messages(occasion, recipient_name, relationship="",
                           tone="friendly", extra_context="", count=3):
    prompt = f"""다음 상황에 맞는 카카오톡 메시지를 {count}개 작성해주세요.
    상황: {occasion}
    수신자: {recipient_name}
    관계: {relationship or '일반'}
    톤: {tone}
    {f'추가 맥락: {extra_context}' if extra_context else ''}
    
    각 메시지를 번호를 붙여서 작성해주세요. (1. 2. 3. ...)
    200자 이내로 작성하세요."""
    
    result = await call_llm_with_fallback(
        messages=[{"role": "user", "content": prompt}],
        model="claude-haiku",  # 비용 효율
        max_tokens=800,
    )
    return _parse_numbered_messages(result, count)
```

### 파싱 로직
- `1. `, `1) `, `01. `, `01) ` 패턴 매칭
- `#` 헤더 라인 제거
- expected 수보다 적으면 그대로 반환, 많으면 slice

---

## 3. kakaobot_scheduler.py 실행 흐름 (244줄)

### 서버 시작 → 스케줄러 루프

```
main.py lifespan:
  └─ start_scheduler_tasks()
       ├─ asyncio.create_task(_scheduler_loop_send)      # 매 60초
       └─ asyncio.create_task(_scheduler_loop_anniversary)  # 매일 02:00 KST
```

### check_and_send_scheduled() 상세

```python
async def check_and_send_scheduled() -> int:
    pool = get_pool()
    sent_count = 0
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT s.*, c.phone, c.name
            FROM kakaobot_scheduled s
            JOIN kakaobot_contacts c ON s.contact_id = c.id
            WHERE s.status = 'pending' AND s.scheduled_at <= NOW()
            ORDER BY s.scheduled_at ASC LIMIT 20
        """)
        for row in rows:
            if not aligo_client.is_available():
                break  # 알리고 미설정 시 전량 스킵
            result = await aligo_client.send_sms(
                receiver=row["phone"],
                msg=row["message"],
            )
            status = "sent" if result.get("result_code") == 1 else "failed"
            await conn.execute("""
                UPDATE kakaobot_scheduled
                SET status=$1, sent_at=NOW(), send_result=$2
                WHERE id=$3
            """, status, json.dumps(result), row["id"])
            sent_count += 1
    return sent_count
```

### generate_anniversary_schedules() 상세

```python
# 1) auto_send=TRUE 기념일 조회
# 2) 오늘 발송 대상 계산
#    - yearly: 올해 날짜 - remind_days_before == 오늘
#    - monthly: 이번 달 날짜 - remind_days_before == 오늘
#    - once: 정확히 일치
# 3) 중복 체크: 같은 anniversary_id + 오늘 날짜 이미 존재하면 스킵
# 4) 메시지 결정 우선순위:
#    custom_message > template.content > AI생성 > "기념일 축하 메시지입니다"
# 5) 09:00 KST 예약 INSERT
```

---

## 4. aligo_client.py 상세 (238줄)

### SMS 발송 로직
```python
async def send_sms(receiver, msg, sender=None, title="", 
                   rdate="", rtime="", testmode="N"):
    if not is_available():
        return {"result_code": -1, "message": "알리고 미설정"}
    
    # EUC-KR 90바이트 초과 시 LMS 자동 전환
    try:
        byte_len = len(msg.encode("euc-kr"))
    except UnicodeEncodeError:
        byte_len = len(msg) * 2
    msg_type = "LMS" if byte_len > 90 else "SMS"
    
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(f"{_SMS_BASE}/send/", data={
            "key": _API_KEY,
            "user_id": _USER_ID,
            "sender": sender or _SENDER,
            "receiver": receiver,
            "msg": msg,
            "msg_type": msg_type,
            "title": title or ("안내" if msg_type == "LMS" else ""),
            "rdate": rdate,
            "rtime": rtime,
            "testmode_yn": testmode,
        })
        return resp.json()
```

### 알림톡 발송 로직
```python
async def send_alimtalk(receiver, template_code, message, 
                        subject="", button="", failover="N",
                        fsubject="", fmessage=""):
    # ALIGO_SENDER_KEY 필수
    # failover="Y" 시 SMS 자동 폴백
    data = {
        "key": _API_KEY,
        "user_id": _USER_ID,
        "senderkey": _SENDER_KEY,
        "sender": _SENDER,
        "receiver_1": receiver,
        "tpl_code": template_code,
        "message_1": message,
        "subject_1": subject,
        "button_1": button or "{}",
        "failover": failover,
        "fsubject_1": fsubject,
        "fmessage_1": fmessage,
    }
```

---

## 5. 프론트엔드 상세

### 5.1 인증 흐름 (전 페이지 공통)
```typescript
function getAuthHeaders(): Record<string, string> {
  const token = localStorage.getItem("aads_token")
    || document.cookie.split("; ")
        .find(r => r.startsWith("aads_token="))?.split("=")[1]
    || null;
  return token ? { Authorization: `Bearer ${token}` } : {};
}
```

### 5.2 layout.tsx (9탭 네비게이션)
```typescript
const NAV_ITEMS = [
  { href: "/kakaobot", label: "대시보드", icon: "🏠" },
  { href: "/kakaobot/contacts", label: "연락처", icon: "👥" },
  { href: "/kakaobot/anniversaries", label: "기념일", icon: "📅" },
  { href: "/kakaobot/templates", label: "템플릿", icon: "📝" },
  { href: "/kakaobot/ai-writer", label: "AI 생성", icon: "✨" },
  { href: "/kakaobot/scheduled", label: "예약발송", icon: "⏰" },
  { href: "/kakaobot/history", label: "이력", icon: "📋" },
  { href: "/kakaobot/agent", label: "PC 에이전트", icon: "🤖" },
  { href: "/kakaobot/settings", label: "설정", icon: "⚙️" },
];
// 브랜드 컬러: #FFE812 (카카오 노란색)
```

### 5.3 대시보드 (page.tsx)
- `GET /api/v1/kakao-bot/stats` 호출 → 5개 통계 카드
- 8개 기능 카드 그리드 (각 페이지 링크)
- 3단계 빠른 시작 가이드

### 5.4 AI 생성기 (ai-writer/page.tsx, 216줄)
- 상황 선택: SITUATIONS 배열 (생일, 결혼, 졸업, 설날, 추석 등)
- 관계 선택: RELATIONSHIPS 배열 (친구, 가족, 직장동료, 고객 등)
- 톤 선택: friendly, formal, casual, humorous
- `POST /api/v1/kakao-bot/ai/generate` 호출
- 결과를 카드로 표시, 복사 버튼

### 5.5 도메인 분기 (layout.tsx — root)
```typescript
const isKakaobot = host.includes("kakaobot");
// → title, description, manifest, icons 모두 분기
```

---

## 6. PC Agent 상세

### 6.1 빌드 및 배포
```bash
# PyInstaller 빌드 (Windows에서)
pyinstaller kakaobot-setup.spec

# 서버에서 다운로드 API
GET /api/v1/kakao-bot/agent/download      → ZIP
GET /api/v1/kakao-bot/agent/download-exe  → EXE
```

### 6.2 에이전트 등록/하트비트
```python
POST /api/v1/kakao-bot/agent/register
{
    "agent_id": "PC-xxxx",
    "hostname": "DESKTOP-ABC",
    "version": "1.0.0",
    "os_info": "Windows 10"
}
```

### 6.3 자동 응답 설정 (kakao_auto.py)
```python
# 기본 설정
delay_min = 1.0        # 최소 응답 딜레이 (초)
delay_max = 5.0        # 최대 응답 딜레이 (초)
tone = "friendly"      # AI 톤
max_length = 200       # 최대 메시지 길이
rate_limit_per_min = 10  # 분당 최대 응답 수

# 감시 대상 윈도우 클래스
_KAKAO_CHAT_CLASSES = ("EVA_Window_Dblclk", "EVA_Window")
```

---

## 7. 트러블슈팅 가이드

### 7.1 "AI 문구가 생성되지 않음"
1. `call_llm_with_fallback` 에러 로그 확인: `docker exec aads-server grep "kakaobot_ai" /var/log/aads-server/*.log`
2. `ANTHROPIC_AUTH_TOKEN` 유효성 확인
3. LiteLLM 프록시 상태: `curl http://litellm:4000/health`

### 7.2 "예약 발송이 실행되지 않음"
1. 스케줄러 실행 확인: `docker exec aads-server grep "kakaobot_scheduler" /var/log/aads-server/*.log`
2. 알리고 설정 확인: `is_available()` → `ALIGO_API_KEY`, `ALIGO_USER_ID` 확인
3. pending 건 확인: `SELECT * FROM kakaobot_scheduled WHERE status='pending' ORDER BY scheduled_at`

### 7.3 "PC Agent 연결 안됨"
1. Agent 등록 확인: `GET /api/v1/kakao-bot/agent/version`
2. 방화벽 포트 확인 (8100)
3. Windows 카카오톡 실행 여부 + PyAutoGUI 권한 (관리자 실행)

### 7.4 "메신저봇R 응답 안됨"
1. `BOT_TOKEN` 설정 여부 확인 (현재 미설정)
2. `POST /api/v1/kakao-bot/msgbot/token/generate`로 토큰 생성
3. Android 앱에서 스크립트 컴파일 & 활성화 확인

### 7.5 "프론트엔드 로그인 안됨"
1. `kakaobot.newtalk.kr` DNS → 서버68 IP 확인
2. CORS 설정 확인: `allow_origins`에 `https://kakaobot.newtalk.kr` 포함 여부
3. `aads_token` 쿠키/localStorage 확인

---

## 8. 향후 로드맵 (제안)

| 우선순위 | 작업 | 설명 |
|----------|------|------|
| P1 | 보안 패치 | cancel_scheduled user_id 검증 추가 |
| P1 | 음력 지원 완성 | korean-lunar-calendar 패키지 설치 |
| P2 | 프론트엔드 검증 | API 필드 전수 매칭 테스트 |
| P2 | 대량 발송 | 그룹 대상 일괄 발송 기능 |
| P3 | 통계 대시보드 | 일별/월별 발송 현황 시각화 |
| P3 | 알림톡 연동 | 카카오 비즈니스 채널 정식 연동 |
| P4 | 멀티테넌트 | 사용자별 요금제/한도 관리 |

---

*끝. 인수인계서(KAKAOBOT-HANDOVER-001.md)와 함께 참조하세요.*
