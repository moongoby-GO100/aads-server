# Anthropic OAuth — ANTHROPIC_AUTH_TOKEN 기준 정리

**일자**: 2026-03-24 (KST)

## 요지

- 앱·채팅 경로는 **`ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_AUTH_TOKEN_2`** 만 사용 (docker-compose에서 `ANTHROPIC_API_KEY` 별칭 주입 제거, `.env` + `env_file`).
- **Pipeline C 원격 `claude`**: 셸에서 **`ANTHROPIC_AUTH_TOKEN` 우선** 설정 후, 레거시 CLI/SDK가 읽는 **`ANTHROPIC_API_KEY`에 동일 OAuth 값만 복사** (이름은 레거시, 값은 sk-ant-oat01).

## 변경 파일

- `app/services/pipeline_c.py` — 위 셸 export 순서
- `app/core/anthropic_client.py` — `get_client` doc/구현 명시
- `app/api/health.py` — LiteLLM `.env.litellm`에서 `ANTHROPIC_API_KEY_1` 인식
- `app/core/prompts/system_prompt_v2.py` — R-AUTH 문구
- `CLAUDE.md`, `.env.example`, `.github/workflows/ci.yml`

## 검증

- `python3.11 -m py_compile` (수정 파이썬 파일)

## 배포

- `docker compose up -d --no-deps aads-server` (이미 `.env`에 토큰 있으면 env_file로 반영)
