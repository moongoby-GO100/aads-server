# Claude OAuth 호출 순서 — Gmail 1순위 전환

**일자**: 2026-03-24 (KST)

## 요약

AADS 채팅·백그라운드 LLM에서 Anthropic OAuth를 쓸 때 **Naver(TOKEN_2)가 1순위**로 잡히던 설정을 **Gmail(TOKEN_1 / `ANTHROPIC_API_KEY_FALLBACK`) 우선**으로 맞춤.

## 원인

- `docker-compose.yml`: `ANTHROPIC_API_KEY` ← `ANTHROPIC_AUTH_TOKEN_2`(Naver), `ANTHROPIC_API_KEY_FALLBACK` ← `ANTHROPIC_AUTH_TOKEN`(Gmail).
- `app/core/anthropic_client.py`의 `get_client` / `call_llm_*_fallback`이 `[Naver, Gmail]` 순으로 시도.
- `app/services/model_selector.py`의 `_ANTHROPIC_KEYS` 기본값도 Naver 우선(관리 UI `get_key_order` 표시).

LiteLLM(`litellm-config.yaml`)은 이미 `ANTHROPIC_API_KEY_1`=Gmail 선행이었음.

## 변경 파일

| 파일 | 내용 |
|------|------|
| `app/core/anthropic_client.py` | Gmail→Naver 순 폴백, `get_client` 기본 키 Gmail 우선 |
| `app/services/model_selector.py` | `_ANTHROPIC_KEYS` 기본 `[Gmail, Naver]` |

## 검증

- `/usr/local/bin/python3.11 -m py_compile` 대상 2파일: 통과

## 적용·배포

- 소스: ✅ 반영 (`/root/aads/aads-server`)
- 프로덕션: AADS API 컨테이너/프로세스 **재시작 후** 반영 (Docker 배포 절차에 따름)

## 후속

- [ ] 배포 후 채팅 1회 호출·로그에서 사용 키 prefix(관리자 키 순서 API) Gmail 선두 확인
- [ ] Naver 우선이 필요하면 기존처럼 `set_key_order("naver")` API 사용
