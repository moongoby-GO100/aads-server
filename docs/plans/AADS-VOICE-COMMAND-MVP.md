# AADS Voice Command MVP Plan

작성 시각: 2026-06-18 09:16 KST
갱신 시각: 2026-06-18 10:01 KST

## 결론

AADS에 음성으로 지시하고 음성으로 답변을 듣는 기능은 구현 가능하다.
다만 현재 본체에는 `audio` 모델 라우팅 후보만 있고, 실제 STT/TTS API, 대시보드 녹음 UI, 음성 재생 UI는 구현되어 있지 않다.

권장 방향은 실시간 음성 대화가 아니라 장애 범위가 작은 MVP부터 적용하는 것이다.

1. 사용자가 마이크 버튼으로 녹음한다.
2. AADS가 음성을 STT로 텍스트 변환한다.
3. 변환된 텍스트를 기존 채팅 전송 경로로 보낸다.
4. 기존 채팅 파이프라인이 도구 호출, 모델 라우팅, 응답 저장을 그대로 수행한다.
5. 사용자가 원하면 답변 텍스트를 TTS로 변환해 재생한다.

## 현재 확인 상태

| 영역 | 현재 상태 | 근거 |
|---|---|---|
| `audio` 모델 라우팅 | 후보 시드 있음 | `app/api/llm_models.py`, `migrations/095_local_multimodal_model_bridge.sql` |
| STT API | 백엔드 MVP 구현 | `app/api/voice.py`, `app/services/voice_service.py` |
| TTS API | 백엔드 MVP 구현 | `app/api/voice.py`, `app/services/voice_service.py` |
| 음성 헬스체크 | 백엔드 MVP 구현 | `/api/v1/voice/health`, provider 설정 여부 masking |
| 대시보드 마이크 UI | 미구현 | 음성 관련 컴포넌트 파일 없음 |
| 기존 채팅 도구 연동 | 가능 | 음성을 텍스트로 변환 후 기존 chat send 경로 사용 |
| 구현 러너 `runner-9dae6d37` | 실패 | root 환경에서 `--dangerously-skip-permissions` 차단, diff 0건 |

## 필요한 구성요소

### Backend

신규 라우터 `app/api/voice.py`를 추가한다.

- `POST /api/v1/voice/transcribe`
  - multipart audio 업로드를 받는다.
  - mime type, 파일 크기, 길이를 제한한다.
  - 사용 가능한 STT provider가 없으면 503을 반환한다.
  - 성공 시 `{ "text": "...", "provider": "...", "model": "..." }` 형식으로 반환한다.
- `POST /api/v1/voice/speech`
  - 텍스트를 입력받아 TTS 오디오를 생성한다.
  - base64 audio 또는 short-lived URL 방식 중 하나로 반환한다.
  - 텍스트 길이 제한과 timeout을 둔다.
- `GET /api/v1/voice/health`
  - STT/TTS provider 사용 가능 여부를 반환한다.
  - API 키 값은 절대 노출하지 않고 boolean/status만 반환한다.

### Provider Routing

신규 서비스 `app/services/voice_service.py`를 둔다.

- 1순위: 사용 가능한 OpenAI STT/TTS 계정
- 2순위: DB `audio` 라우팅 후보 또는 model registry
- 3순위: local/PC Agent Whisper/Piper 계열 준비 상태
- 미설정 시: 503 `voice_provider_not_configured`

음성 API가 직접 도구를 실행하지 않고, 변환된 텍스트를 기존 채팅 파이프라인에 넘기는 구조를 유지한다. 이렇게 해야 기존 권한, 테넌트, 도구 실행 정책, 자동 라우팅과 충돌하지 않는다.

### Dashboard UI

AADS 대시보드 채팅 입력창에 음성 컨트롤을 추가한다.

- 마이크 아이콘: 녹음 시작/중지
- 녹음 중 상태: 버튼 상태와 타이머 표시
- STT 업로드 중 상태: 입력 잠금 또는 진행 표시
- 변환 결과: 입력창에 채우기 또는 즉시 전송 옵션
- 답변 스피커 아이콘: TTS 생성 후 재생
- 실패 상태: 권한 거부, mime 미지원, provider 미설정, 네트워크 실패 메시지

모바일/데스크톱 모두 아이콘 중심으로 구현하고, 버튼 텍스트 overflow가 생기지 않게 한다.

## 보안 및 운영 기준

| 항목 | 기준 |
|---|---|
| 인증 | 기존 AADS JWT/tenant 인증 재사용 |
| 공개 API 여부 | 공개 금지, 내부 로그인 사용자 전용 |
| 파일 제한 | mime allowlist, max size, timeout 필수 |
| 민감값 | API key/env 값 로그/응답/git diff 노출 금지 |
| 사용량 제한 | 무제한 운영이더라도 음성 파일 크기와 요청 시간은 hard-limit 필요 |
| 도구 실행 | 음성 API 직접 실행 금지, 기존 chat send에 위임 |
| 감사 로그 | transcript, provider, duration, error_code 저장. 원본 음성 저장은 기본 비활성 권장 |

## 구현 단계

| 단계 | 작업 | 완료 기준 |
|---|---|---|
| P0 | voice router/service 추가 | 503, health, validation 테스트 통과 |
| P0 | dashboard 마이크/STT UI | 녹음 후 텍스트 입력창 반영 |
| P1 | 답변 TTS 재생 | 답변별 스피커 버튼으로 재생 |
| P1 | provider routing | 사용 가능 계정 우선 선택, 미설정 503 |
| P1 | 로그/비용 추적 | 요청 수, duration, provider 기록 |
| P2 | streaming/realtime voice | MVP 안정화 후 별도 검토 |

## 검증 계획

Backend:

```bash
python3 -m py_compile app/api/voice.py app/services/voice_service.py
pytest -q tests/unit/test_voice_api.py
```

Dashboard:

```bash
npm run lint
npx tsc --noEmit --pretty false
```

API smoke:

```bash
curl -f http://127.0.0.1:8100/api/v1/voice/health
```

E2E:

1. AADS 대시보드 로그인
2. 채팅 입력창에서 마이크 클릭
3. 2~3초 녹음
4. STT 결과가 입력창에 들어오는지 확인
5. 전송 후 기존 채팅 응답 수신 확인
6. 답변 스피커 클릭 후 TTS 재생 확인

## 러너 재투입 지시안

`runner-9dae6d37`는 root 환경에서 Claude Code 권한 플래그가 차단되어 실패했다. 재투입 시 특정 Claude worker model을 강제하지 말고, Runner 기본 모델 설정을 사용하거나 runner 실행 스크립트의 root 권한 플래그 문제를 먼저 해결해야 한다.

```text
TASK_ID: AADS-207
TITLE: AADS Voice Command MVP - STT/TTS/채팅 UI 통합
PRIORITY: P1
SIZE: M
MODE: code_modify

목표:
- /api/v1/voice/transcribe, /api/v1/voice/speech, /api/v1/voice/health 추가
- 기존 AADS JWT/tenant 인증 재사용
- provider 미설정 시 503과 health masking 구현
- dashboard chat 입력창 마이크 버튼과 답변 스피커 버튼 추가
- 음성 텍스트는 기존 chat send 경로로 전송해 도구 실행 정책 유지

절대 금지:
- .env/secret 출력 또는 커밋 금지
- 기존 unrelated dirty 파일 revert 금지
- 기존 채팅 API 계약 변경 금지
- 배포/재시작은 승인 전 금지

검증:
- python3 -m py_compile app/api/voice.py app/services/voice_service.py
- pytest -q tests/unit/test_voice_api.py
- dashboard lint/typecheck 가능한 범위
- git diff 민감값 없음 확인
```

## 2026-06-18 10:01 KST 갱신 판정

백엔드 MVP는 `app/api/voice.py`, `app/services/voice_service.py`, `tests/unit/test_voice_service.py`로 구현되어 있고
`app/main.py`에 `/api/v1/voice/*` 라우터가 연결됐다.

남은 항목:

- 대시보드 마이크/STT 입력 UI는 아직 미구현이다.
- 답변별 TTS 재생 버튼은 아직 미구현이다.
- 운영 배포와 브라우저 E2E는 별도 승인/배포 후 확인해야 한다.
