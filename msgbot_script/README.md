# AADS 메신저봇R — 카카오톡 AI 자동 응답

안드로이드 메신저봇R 앱을 통해 카카오톡 메시지에 AI가 자동 응답합니다.

## 1. 메신저봇R 설치

1. Google Play Store에서 **"메신저봇R"** 검색 후 설치
   - 또는 APK 직접 설치: https://github.com/nickyam/MessengerBotR
2. 앱 실행 후 **알림 접근 권한** 허용 (필수)
   - 설정 > 앱 > 메신저봇R > 알림 접근 > 허용
3. **배터리 최적화 제외** 설정 (백그라운드 동작 유지)
   - 설정 > 배터리 > 메신저봇R > 제한 없음

## 2. BOT_TOKEN 발급

토큰 자동 생성 API를 호출하세요:

```bash
curl -X POST "https://aads.newtalk.kr/api/v1/kakao-bot/msgbot/token/generate?user_label=내이름"
# 응답: { "bot_token": "abc123...", "user_label": "내이름" }
```

발급 후 설정을 커스터마이징하려면:

```bash
curl -X POST https://aads.newtalk.kr/api/v1/kakao-bot/msgbot/config \
  -H "Content-Type: application/json" \
  -d '{
    "bot_token": "발급받은_토큰",
    "config": {
      "enabled_rooms": ["*"],
      "blocked_rooms": [],
      "tone": "friendly",
      "model": "haiku",
      "auto_reply": true,
      "reply_delay_sec": 2
    }
  }'
```

## 3. 스크립트 설정

1. 메신저봇R 앱에서 **"봇 추가"** > **JavaScript** 선택
2. `kakaobot.js` 파일의 내용을 전체 복사-붙여넣기
3. 스크립트 상단의 두 값을 수정:
   ```javascript
   var BOT_TOKEN = "발급받은_토큰";  // 2단계에서 받은 토큰
   var BOT_TRIGGER = "@봇";         // 그룹채팅 호출 키워드 (원하는 대로 변경)
   ```
4. **컴파일** 버튼 클릭
5. 봇 **활성화** 토글 ON

## 4. 테스트

1. 카카오톡에서 아무 채팅방에 메시지 전송
2. 1:1 채팅: 자동으로 AI 응답
3. 그룹채팅: `@봇 안녕` 처럼 트리거 키워드 포함 시 응답

## 5. 설정 옵션

| 항목 | 기본값 | 설명 |
|------|--------|------|
| `enabled_rooms` | `["*"]` | 응답할 채팅방 (`["*"]` = 전체) |
| `blocked_rooms` | `[]` | 차단할 채팅방 목록 |
| `tone` | `friendly` | 톤앤매너: friendly, formal, casual, witty |
| `model` | `haiku` | AI 모델: haiku(빠름), sonnet(균형), opus(최고) |
| `auto_reply` | `true` | 자동 응답 ON/OFF |
| `reply_delay_sec` | `2` | 응답 딜레이 (초) — 현재 서버 미사용, 향후 확장 |

## 6. API 레퍼런스

| 메서드 | 경로 | 설명 |
|--------|------|------|
| POST | `/api/v1/kakao-bot/msgbot/webhook` | 메신저봇R 웹훅 (AI 응답) |
| POST | `/api/v1/kakao-bot/msgbot/config` | 설정 저장 |
| GET | `/api/v1/kakao-bot/msgbot/config?bot_token=xxx` | 설정 조회 |
| GET | `/api/v1/kakao-bot/msgbot/logs?bot_token=xxx&limit=50` | 로그 조회 |
| POST | `/api/v1/kakao-bot/msgbot/token/generate?user_label=이름` | 토큰 발급 |

## FAQ

**Q: 카카오톡 버전 제한이 있나요?**
A: 메신저봇R은 카카오톡 알림을 읽는 방식이라 대부분의 버전에서 동작합니다. 알림 미리보기가 켜져 있어야 합니다.

**Q: 배터리 소모가 심한가요?**
A: 메신저봇R은 알림 리스너 방식이라 배터리 소모가 적습니다. 단, 배터리 최적화에서 제외해야 안정적으로 동작합니다.

**Q: 여러 봇을 동시에 사용할 수 있나요?**
A: 네, 메신저봇R에서 여러 봇을 만들 수 있지만, 같은 AADS 서버를 사용하려면 각각 다른 bot_token이 필요합니다.

**Q: 응답이 안 올 때?**
A: (1) 메신저봇R 활성화 확인 (2) 알림 접근 권한 확인 (3) BOT_TOKEN이 올바른지 확인 (4) 서버 상태 확인
