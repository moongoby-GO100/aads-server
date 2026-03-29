# OpenClaw ↔ 68서버(AADS) 채팅 연동 가능성 — 연구 보고  
**일시:** 2026-03-29 KST

## 1. 용어 정리

| 용어 | 본 보고에서의 의미 |
|------|-------------------|
| **OpenClaw** | 오픈소스 에이전트/게이트웨이 스택. 모델은 `provider/model` 형식, Anthropic·OpenAI·커스텀 프록시 등 연동. 공식: [docs.openclaw.ai](https://docs.openclaw.ai/concepts/model-providers) |
| **CP** | 문맥상 **Cursor/로컬 개발 PC**에 OpenClaw CLI·게이트웨이가 설치된 환경으로 해석 (별도 확인 필요) |
| **68서버 채팅창** | AADS 대시보드·API의 **CEO Chat** (`app/api/ceo_chat.py`): FastAPI → `AsyncAnthropic` + OAuth 토큰, 의도 분류·에이전트 루프 |

## 2. 현재 68서버(AADS) 채팅 아키텍처 (요약)

- **백엔드:** `ceo_chat` 라우터가 **Anthropic 공식 SDK**로 `messages` 스트리밍 호출 (`create_anthropic_client` → `api.anthropic.com`, OAuth `sk-ant-oat…` 등).
- **보조 경로:** `auth_provider`에 **LiteLLM** URL/키 설정이 있으나, CEO Chat 핵심은 **직접 Anthropic** 위주.
- **동일 서버 인프라:** Docker **`aads-litellm`** (포트 4000) — OpenAI 호환 `/v1/chat/completions` 및 Anthropic 경유 프록시 역할.

→ 즉, **웹 채팅 UI는 OpenClaw와 직접 연결되어 있지 않음.**

## 3. OpenClaw 측 연동 포인트 (문서 기준)

1. **Anthropic**  
   - `ANTHROPIC_API_KEY` 또는 setup-token / CLI 로그인 등.  
   - 모델 예: `anthropic/claude-opus-4-6`.

2. **로컬/프록시 (LiteLLM 명시)**  
   - 문서에 **「Local proxies (LM Studio, vLLM, LiteLLM, etc.)」** 절이 있음.  
   - `models.providers`에 `baseUrl`, `api: "openai-completions"`, API 키로 **OpenAI 호환 엔드포인트**를 붙이는 패턴.

3. **게이트웨이**  
   - OpenClaw는 자체 **gateway**·에이전트 루프를 가짐. 68의 AADS 채팅과는 **별 애플리케이션**.

결론: OpenClaw는 **“같은 API 키/같은 LiteLLM 프록시를 공유할 수는 있으나”, AADS 웹 채팅과는 기본적으로 다른 진입점**이다.

## 4. “연결해서 사용” 시나리오별 판단

### 4-1. CP의 OpenClaw만 68 **LiteLLM**에 붙이기 (가장 현실적)

- **가능:** CP에서 OpenClaw 설정의 `baseUrl`을 `http://<68서버 공인IP 또는 VPN>:4000/v1` (또는 내부망에서 `aads-litellm:4000`에 도달 가능한 주소)로 두고, Bearer는 LiteLLM 마스터 키와 동일하게 맞춤.  
- **효과:** 트래픽이 **68의 LiteLLM**으로 모여 **라우팅·키 정책 일원화** 가능.  
- **한계:** **AADS 웹 채팅 UI**와는 여전히 별개. “채팅창 하나로 통합”은 아님.

### 4-2. AADS **웹 채팅**이 OpenClaw **세션/에이전트**를 쓰게 하기

- **현 상태로는 불가에 가깝다.**  
  - `ceo_chat`은 OpenClaw HTTP API를 호출하지 않음.  
- **가능하게 하려면 (개발 필요):**  
  - (A) OpenClaw **gateway**를 68에 설치하고, 공개 API(예: OpenAI 호환 또는 전용)를 정한 뒤 `ceo_chat`의 LLM 호출부를 해당 엔드포인트로 **위임**하거나,  
  - (B) OpenClaw가 노출하는 **MCP/툴**을 AADS가 **도구 호출**로 붙이는 방식 (기존 intent/tool-use 루프 확장).  
- **난이도:** 중~상 (인증, 스트리밍, 세션 ID, 에러·폴백 정책 정합).

### 4-3. “같은 Claude 구독/OAuth를 쓰면 자동 연동?”

- **부분만 해당.**  
  - OpenClaw와 AADS 모두 **Anthropic OAuth/API 키**를 쓸 수 있으나, **계정·키 공유 ≠ UI·세션 통합**.  
  - 한도·429는 **동일 키를 쓰는 모든 클라이언트**에 합산될 수 있음.

## 5. 보안·운영 주의

- 68의 **4000 포트**를 외부에 열면 프록시 남용 위험 → **방화벽, IP 허용, 또는 Tailscale/WireGuard** 권장.  
- LiteLLM 마스터 키·Anthropic 키는 **회전·최소 권한** 원칙.

## 6. 종합 답변

| 질문 | 답 |
|------|-----|
| CP OpenClaw를 68 **채팅창(AADS 웹 UI)과 그대로 붙여 쓸 수 있나? | **아니오.** 코드상 직접 연결 없음. 별도 개발 필요. |
| CP OpenClaw를 68 **LiteLLM**에 붙여 같은 게이트웨이로 쓸 수 있나? | **예 (설정으로 가능).** `models.providers` + `baseUrl`을 68:4000/v1 등으로 지정. |
| 장기적으로 웹 채팅과 OpenClaw를 통합하려면? | OpenClaw gateway를 68에 두고 **CEO Chat 백엔드를 프록시/어댑터로 교체·병행**하는 설계 검토. |

## 7. 권장 다음 단계 (선택)

1. CP에서 OpenClaw에 **68 LiteLLM URL만** 연결해 스모크 테스트 (네트워크·키만 맞으면 됨).  
2. 웹 채팅 통합이 목표면 **요구사항 정의** (스트리밍, 세션, 에이전트 도구 여부) 후 API 설계.  
3. 외부 노출 시 **접근 제어** 설계 필수.

---

**참고 링크**

- OpenClaw 모델 프로바이더: https://docs.openclaw.ai/concepts/model-providers  
- Anthropic 제공자: https://docs.openclaw.ai/providers/anthropic (또는 Provider Directory)  
- AADS CEO Chat 구현: `aads-server/app/api/ceo_chat.py`  
- 인증·LiteLLM 설정: `aads-server/app/core/auth_provider.py`
